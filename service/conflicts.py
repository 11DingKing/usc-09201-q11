"""冲突检测：从当前有效事件即时推导，结果可回溯到具体事件。

覆盖纸质记录时代月底才发现的几类问题：
- 跨基地/跨任务工时重叠（同一村民同一时段出现在两处）；
- 同一照片等证据摘要被多条事件重复使用；
- 重复签名（同一人同一任务同类事件相隔过近）；
- 临时换人未登记（事件发生时不在名册内，工时仍计入，仅提示补登记）；
- 证据位置网格与地块网格不一致（山区定位漂移常见，仅作提示）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .derive import build_timeline, group_events
from .models import Task, WorkEvent
from .timeutil import slice_by_day

DUPLICATE_WINDOW_MINUTES = 10

KIND_OVERLAP = "overlap_hours"
KIND_EVIDENCE = "evidence_reused"
KIND_DUPLICATE = "possible_duplicate"
KIND_UNASSIGNED = "unassigned_work"
KIND_LOCATION = "location_mismatch"


def _conflict_id(kind: str, *parts: object) -> str:
    """确定性冲突编号：同一批事件多次检测结果一致，便于对照与引用。"""

    raw = "|".join([kind, *(str(part) for part in parts)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class OverlapPair:
    """同一村民跨任务的一对重叠片段。"""

    worker_id: str
    task_a: str
    task_b: str
    holder_task: str  # 后到片段所在任务，其重叠部分暂扣
    conflict_id: str
    event_ids: list[str]
    day_minutes: list[tuple[str, int]]


def find_overlap_pairs(events: list[WorkEvent]) -> list[OverlapPair]:
    """找出同一村民在不同任务上的时间重叠，重叠部分归后到片段承担。"""

    grouped = group_events(events)
    segments_by_worker: dict[str, list[tuple[str, object]]] = {}
    for (task_id, worker_id), grouped_events in grouped.items():
        timeline = build_timeline(task_id, worker_id, grouped_events)
        for segment in timeline.segments:
            if segment.end_at is not None and segment.end_at > segment.start_at:
                segments_by_worker.setdefault(worker_id, []).append((task_id, segment))

    pairs: list[OverlapPair] = []
    for worker_id, items in sorted(segments_by_worker.items()):
        items.sort(key=lambda item: (item[1].start_at, item[1].start_event_id))
        for index in range(len(items)):
            for other in range(index + 1, len(items)):
                task_a, seg_a = items[index]
                task_b, seg_b = items[other]
                if task_a == task_b:
                    continue
                start = max(seg_a.start_at, seg_b.start_at)
                end = min(seg_a.end_at, seg_b.end_at)
                if end <= start:
                    continue
                later_is_b = (seg_b.start_at, seg_b.start_event_id) > (
                    seg_a.start_at,
                    seg_a.start_event_id,
                )
                holder = task_b if later_is_b else task_a
                conflict_id = _conflict_id(
                    KIND_OVERLAP,
                    worker_id,
                    *sorted([seg_a.start_event_id, seg_b.start_event_id]),
                )
                pairs.append(
                    OverlapPair(
                        worker_id=worker_id,
                        task_a=task_a,
                        task_b=task_b,
                        holder_task=holder,
                        conflict_id=conflict_id,
                        event_ids=sorted([seg_a.start_event_id, seg_b.start_event_id]),
                        day_minutes=slice_by_day(start, end),
                    )
                )
    return pairs


def detect(events: list[WorkEvent], tasks: dict[str, Task]) -> list[dict]:
    """对一批事件做全量冲突检测，返回稳定的冲突列表。"""

    active = [event for event in events if event.status == "active"]
    conflicts: list[dict] = []

    def base_ids_of(task_ids: list[str]) -> list[str]:
        return sorted({tasks[tid].base_id for tid in task_ids if tid in tasks})

    # 同一证据摘要被多条事件重复使用
    by_digest: dict[str, list[WorkEvent]] = {}
    for event in active:
        if event.evidence and event.evidence.digest:
            by_digest.setdefault(event.evidence.digest, []).append(event)
    for digest, group in sorted(by_digest.items()):
        if len(group) < 2:
            continue
        task_ids = sorted({event.task_id for event in group})
        conflicts.append(
            {
                "conflict_id": _conflict_id(KIND_EVIDENCE, digest),
                "kind": KIND_EVIDENCE,
                "severity": "critical",
                "detail": f"同一证据摘要被 {len(group)} 条事件重复使用，疑似同一照片重复登记",
                "refs": {
                    "event_ids": sorted(event.event_id for event in group),
                    "worker_ids": sorted({event.worker_id for event in group}),
                    "task_ids": task_ids,
                    "base_ids": base_ids_of(task_ids),
                },
                "evidence_digest": digest,
            }
        )

    # 重复签名：同一村民同一任务同类事件相隔过近
    by_signature: dict[tuple[str, str, str], list[WorkEvent]] = {}
    for event in active:
        if event.kind == "review":
            continue
        key = (event.task_id, event.worker_id, event.kind)
        by_signature.setdefault(key, []).append(event)
    for (task_id, worker_id, kind), group in sorted(by_signature.items()):
        group.sort(key=lambda event: (event.occurred_at, event.seq))
        for first, second in zip(group, group[1:]):
            gap = (second.occurred_at - first.occurred_at).total_seconds() / 60
            if gap > DUPLICATE_WINDOW_MINUTES:
                continue
            conflicts.append(
                {
                    "conflict_id": _conflict_id(
                        KIND_DUPLICATE, first.event_id, second.event_id
                    ),
                    "kind": KIND_DUPLICATE,
                    "severity": "warning",
                    "detail": (
                        f"村民 {worker_id} 在任务 {task_id} 的同类记录相隔 "
                        f"{int(gap)} 分钟，疑似重复签名"
                    ),
                    "refs": {
                        "event_ids": sorted([first.event_id, second.event_id]),
                        "worker_ids": [worker_id],
                        "task_ids": [task_id],
                        "base_ids": base_ids_of([task_id]),
                    },
                }
            )

    # 临时换人未登记 / 证据位置与地块网格不一致
    for event in active:
        if event.kind == "review":
            continue
        task = tasks.get(event.task_id)
        if task is None:
            continue
        if event.worker_id not in task.roster_at(event.occurred_at):
            conflicts.append(
                {
                    "conflict_id": _conflict_id(KIND_UNASSIGNED, event.event_id),
                    "kind": KIND_UNASSIGNED,
                    "severity": "warning",
                    "detail": (
                        f"村民 {event.worker_id} 在任务 {event.task_id} 发生时不在名册内，"
                        "工时照常计入，请补办临时换人登记"
                    ),
                    "refs": {
                        "event_ids": [event.event_id],
                        "worker_ids": [event.worker_id],
                        "task_ids": [event.task_id],
                        "base_ids": base_ids_of([event.task_id]),
                    },
                }
            )
        if (
            event.evidence
            and event.evidence.location_cell
            and task.plot_cell
            and event.evidence.location_cell != task.plot_cell
        ):
            conflicts.append(
                {
                    "conflict_id": _conflict_id(KIND_LOCATION, event.event_id),
                    "kind": KIND_LOCATION,
                    "severity": "info",
                    "detail": (
                        f"事件 {event.event_id} 的证据位置网格 "
                        f"{event.evidence.location_cell} 与地块网格 {task.plot_cell} 不一致"
                    ),
                    "refs": {
                        "event_ids": [event.event_id],
                        "worker_ids": [event.worker_id],
                        "task_ids": [event.task_id],
                        "base_ids": base_ids_of([event.task_id]),
                    },
                }
            )

    # 跨任务工时重叠
    for pair in find_overlap_pairs(active):
        task_ids = sorted({pair.task_a, pair.task_b})
        base_ids = base_ids_of(task_ids)
        total = sum(minutes for _, minutes in pair.day_minutes)
        conflicts.append(
            {
                "conflict_id": pair.conflict_id,
                "kind": KIND_OVERLAP,
                "severity": "critical" if len(base_ids) > 1 else "warning",
                "detail": (
                    f"村民 {pair.worker_id} 在任务 {pair.task_a} 与 {pair.task_b} "
                    f"的工时重叠 {total} 分钟，重叠部分暂由后到任务 {pair.holder_task} 承担"
                ),
                "refs": {
                    "event_ids": pair.event_ids,
                    "worker_ids": [pair.worker_id],
                    "task_ids": task_ids,
                    "base_ids": base_ids,
                },
                "holder_task_id": pair.holder_task,
                "overlaps": [
                    {"day": day, "minutes": minutes} for day, minutes in pair.day_minutes
                ],
            }
        )

    conflicts.sort(key=lambda item: (item["kind"], item["conflict_id"]))
    return conflicts
