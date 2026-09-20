"""由事件流推导工时时间线。

推导是纯函数：同一组事件无论到达顺序如何，都按（发生时间, 到达序号）排序后
重放状态机，因此离线补传、倒序到达都能得到一致结果。无法解释的异常不丢弃，
记入 anomalies 一并展示，保证“不漏记真实劳动，也不静默改数”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import EVENT_KIND_LABELS, WorkEvent


@dataclass
class Segment:
    """一段闭合或未闭合的计工时间。"""

    start_event_id: str
    start_at: datetime
    end_event_id: str | None = None
    end_at: datetime | None = None
    anomalies: list[str] = field(default_factory=list)

    @property
    def minutes(self) -> int | None:
        if self.end_at is None:
            return None
        return int((self.end_at - self.start_at).total_seconds() // 60)


@dataclass
class Timeline:
    """某村民在某任务上的完整作业时间线。"""

    task_id: str
    worker_id: str
    segments: list[Segment]
    anomalies: list[dict]
    review: WorkEvent | None
    completed: bool

    @property
    def total_minutes(self) -> int:
        return sum(seg.minutes or 0 for seg in self.segments)


def build_timeline(task_id: str, worker_id: str, events: list[WorkEvent]) -> Timeline:
    """把同一（任务, 村民）的事件重放为时间线。

    状态机：idle -> working -> paused -> working -> done；
    完工后再次开工视为新班次并记录提示；暂停中收到开工按复工处理；
    未暂停先收到复工按开工处理（避免漏记），同时记录异常。
    """

    active = [event for event in events if event.status == "active"]
    active.sort(key=lambda e: (e.occurred_at, e.seq))

    segments: list[Segment] = []
    anomalies: list[dict] = []
    review: WorkEvent | None = None
    state = "idle"
    current: Segment | None = None

    def note(code: str, event: WorkEvent, detail: str) -> None:
        anomalies.append({"code": code, "event_id": event.event_id, "detail": detail})

    for event in active:
        kind = event.kind
        if kind == "review":
            review = event  # 排序后后者覆盖，最新复核生效
            continue
        if state == "done" and kind in ("start", "resume"):
            note("extra_shift", event, "完工后再次开工，按新班次另计")
            state = "idle"
        if kind == "start":
            if state == "working":
                note("duplicate_start", event, "未暂停再次开工，按首次开工计算")
            elif state == "paused":
                note("start_while_paused", event, "暂停中收到开工，按复工处理")
                current = Segment(event.event_id, event.occurred_at, anomalies=["start_while_paused"])
                state = "working"
            else:
                current = Segment(event.event_id, event.occurred_at)
                state = "working"
        elif kind == "pause":
            if state == "working" and current is not None:
                current.end_event_id = event.event_id
                current.end_at = event.occurred_at
                segments.append(current)
                current = None
                state = "paused"
            else:
                note("pause_without_start", event, "未登记开工就收到暂停，该事件不计工时")
        elif kind == "resume":
            if state == "paused":
                current = Segment(event.event_id, event.occurred_at)
                state = "working"
            elif state == "working":
                note("resume_while_working", event, "作业中收到复工，忽略")
            else:
                note("resume_without_pause", event, "未登记暂停收到复工，为避免漏记按开工处理")
                current = Segment(event.event_id, event.occurred_at, anomalies=["resume_without_pause"])
                state = "working"
        elif kind == "complete":
            if state == "working" and current is not None:
                current.end_event_id = event.event_id
                current.end_at = event.occurred_at
                segments.append(current)
                current = None
            elif state == "idle":
                note("complete_without_start", event, "未登记开工就收到完工，该事件不计工时")
            state = "done"

    if current is not None:
        current.anomalies.append("open_segment")
        segments.append(current)
        anomalies.append(
            {
                "code": "open_segment",
                "event_id": current.start_event_id,
                "detail": "缺少暂停或完工，片段未闭合，暂不计入结算",
            }
        )

    return Timeline(
        task_id=task_id,
        worker_id=worker_id,
        segments=segments,
        anomalies=anomalies,
        review=review,
        completed=(state == "done"),
    )


def timeline_to_dict(timeline: Timeline) -> dict:
    """时间线的对外视图，含每段来源事件，便于逐段核对。"""

    review = timeline.review
    return {
        "task_id": timeline.task_id,
        "worker_id": timeline.worker_id,
        "completed": timeline.completed,
        "total_minutes": timeline.total_minutes,
        "review": (
            {
                "event_id": review.event_id,
                "decision": review.review_decision,
                "adjust_minutes": review.adjust_minutes,
                "recorded_by": review.recorded_by,
                "occurred_at": review.occurred_at.isoformat(),
            }
            if review
            else None
        ),
        "segments": [
            {
                "start_event_id": seg.start_event_id,
                "end_event_id": seg.end_event_id,
                "start_at": seg.start_at.isoformat(),
                "end_at": seg.end_at.isoformat() if seg.end_at else None,
                "minutes": seg.minutes,
                "anomalies": list(seg.anomalies),
            }
            for seg in timeline.segments
        ],
        "anomalies": list(timeline.anomalies),
    }


def group_events(events: list[WorkEvent]) -> dict[tuple[str, str], list[WorkEvent]]:
    """把有效事件按（任务, 村民）分组，复核事件一并保留。"""

    grouped: dict[tuple[str, str], list[WorkEvent]] = {}
    for event in events:
        if event.status != "active":
            continue
        grouped.setdefault((event.task_id, event.worker_id), []).append(event)
    return grouped


def kind_label(kind: str) -> str:
    return EVENT_KIND_LABELS.get(kind, kind)
