"""领域规则：事件归一化、时间线核算、冲突检测与结算生成。

设计要点
--------

* 离线优先：客户端按任务维护单调 ``seq``，服务端按 ``(seq, occurred_at)``
  归一化，因此事件倒序到达也能重放出确定的时间线；任务锁定后补传一律拒绝，
  防止管理员事后任意补数。
* 可解释：每一分钟工时都可追溯到具体事件与片段，暂停/跨午夜/临时换人/
  照片复用/跨基地重叠全部以告警形式保留。
* 位置最小化：只接受粗网格单元（如村组级编码），拒绝经纬度字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .store import Event, Store, Task

# 允许的事件类型
EVENT_TYPES = {"start", "pause", "resume", "complete", "handover"}

# 事件发生后超过该阈值才送达，视为延迟补传（仍接受，但标注）
LATE_THRESHOLD = timedelta(hours=72)

# 带班人收尾时，关闭时间不得超过计划完工后的宽限期
FOREMAN_CLOSE_GRACE = timedelta(minutes=180)

# 单个连续作业片段超过该时长需要提示（不断夜护林场景的合理性检查）
LONG_SESSION = timedelta(hours=12)

# 明确拒绝的原始定位字段
FORBIDDEN_LOCATION_FIELDS = {
    "lat",
    "latitude",
    "lng",
    "lon",
    "longitude",
    "gps",
    "coords",
    "coordinates",
}


class DomainError(ValueError):
    """规则校验失败，附带机器可读错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_dt(value: str, field_name: str = "时间") -> datetime:
    """解析必须带时区的 ISO 8601 时间。"""

    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("invalid_time", f"{field_name}不是合法 ISO 8601 时间") from exc
    if dt.tzinfo is None:
        raise DomainError("invalid_time", f"{field_name}必须携带时区偏移，以免跨午夜误算")
    return dt


def mask_name(name: str) -> str:
    """姓名脱敏：保留首字。"""

    if not name:
        return "**"
    return name[0] + "*" * max(1, len(name) - 1)


def strip_location_cells(value: Any) -> Any:
    """递归删除快照中的 ``location_cell`` 键，批准后不再保留任何位置数据。"""

    if isinstance(value, dict):
        return {
            key: strip_location_cells(item)
            for key, item in value.items()
            if key != "location_cell"
        }
    if isinstance(value, list):
        return [strip_location_cells(item) for item in value]
    return value


@dataclass
class Segment:
    """两个事件之间的一段时间线。kind=work 计工时，break 为暂停间歇。"""

    worker_id: str
    start: datetime
    end: datetime
    kind: str
    flags: list[str] = field(default_factory=list)
    opener_event: str | None = None
    closer_event: str | None = None

    @property
    def minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))

    def crosses_midnight(self) -> bool:
        tz = self.start.tzinfo
        return self.start.astimezone(tz).date() != self.end.astimezone(tz).date()

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "kind": self.kind,
            "minutes": self.minutes,
            "flags": self.flags,
            "opener_event": self.opener_event,
            "closer_event": self.closer_event,
        }


def ordered_events(store: Store, task: Task) -> list[Event]:
    """按客户端 seq 归一化后的事件顺序（倒序到达也能还原意图）。"""

    events = list(store.task_events.get(task.id, []))
    return sorted(events, key=lambda e: (e.seq, e.occurred_at, e.receipt))


def compute_timeline(store: Store, task: Task) -> dict[str, Any]:
    """重放任务事件，生成可解释时间线。

    返回片段、按人工时、告警、未闭合会话等。不读取除本任务事件与登记数据
    之外的状态；跨任务冲突见 :func:`compute_conflicts`。
    """

    events = ordered_events(store, task)
    segments: list[Segment] = []
    warnings: list[dict[str, Any]] = []

    active: str | None = None
    active_since: datetime | None = None
    opener_event: str | None = None
    opener_reused = False
    paused: tuple[str, datetime] | None = None
    completed = False

    crew = store.crews.get(task.crew_id)
    crew_member_ids = set(crew.member_ids) if crew else set()

    def warn(code: str, event: Event | None, message: str, **extra: Any) -> None:
        item: dict[str, Any] = {
            "code": code,
            "message": message,
            "event_id": event.event_id if event else None,
        }
        item.update(extra)
        warnings.append(item)

    def close_work(
        end: datetime,
        closer: Event | None,
        flags: list[str] | None = None,
    ) -> None:
        nonlocal active, active_since, opener_event, opener_reused
        if active is None or active_since is None:
            return
        seg_flags = list(flags or [])
        seg = Segment(
            worker_id=active,
            start=active_since,
            end=end,
            kind="work",
            opener_event=opener_event,
            closer_event=closer.event_id if closer else None,
        )
        if opener_reused:
            seg_flags.append("photo_reuse")
        if seg.crosses_midnight():
            seg_flags.append("crosses_midnight")
        if end - active_since >= LONG_SESSION:
            seg_flags.append("long_session")
        seg.flags = seg_flags
        segments.append(seg)
        active = None
        active_since = None
        opener_event = None
        opener_reused = False

    seen_seqs: dict[int, str] = {}
    expected_seq = 1

    for ev in events:
        if ev.worker_id not in crew_member_ids:
            warn("worker_not_in_crew", ev, "事件提交人不属于该任务班组")
        if ev.reused_digests:
            warn(
                "photo_digest_reused",
                ev,
                "开工照片摘要与既有证据重复",
                reused_digests=list(ev.reused_digests),
            )
        if ev.location_cell is not None and not ev.location_in_range:
            warn(
                "location_out_of_range",
                ev,
                "登记位置不在地块允许网格内",
                location_cell=ev.location_cell,
            )
        lag = ev.received_at - ev.occurred_at
        if lag >= LATE_THRESHOLD:
            warn("late_submission", ev, f"事件延迟 {lag.total_seconds() / 3600:.0f} 小时补传")

        if ev.seq in seen_seqs:
            warn("duplicate_seq", ev, f"seq={ev.seq} 与事件 {seen_seqs[ev.seq]} 重复")
        else:
            seen_seqs[ev.seq] = ev.event_id
            while expected_seq < ev.seq:
                if expected_seq not in seen_seqs:
                    warn("sequence_gap", None, f"缺少 seq={expected_seq} 事件", missing_seq=expected_seq)
                expected_seq += 1
            expected_seq = ev.seq + 1

        if ev.type == "start":
            if completed:
                warn("start_after_complete", ev, "任务已完工，开工事件被忽略")
                continue
            if active == ev.worker_id:
                warn("duplicate_start", ev, "该成员已在作业中，重复开工被忽略")
                continue
            if active is not None:
                # 临时换人但未显式交接：在新开工点自动收尾前任，并保留痕迹
                close_work(
                    ev.occurred_at,
                    ev,
                    flags=["implicit_handover"],
                )
                warn(
                    "implicit_handover",
                    ev,
                    f"前任 {active} 未交接即换人，作业片段已在该时点自动收尾",
                    from_worker=active,
                    to_worker=ev.worker_id,
                )
            elif paused is not None and paused[0] != ev.worker_id:
                warn(
                    "handover_after_pause",
                    ev,
                    f"任务暂停期间由 {paused[0]} 临时换为 {ev.worker_id}",
                    from_worker=paused[0],
                    to_worker=ev.worker_id,
                )
                paused = None
            elif paused is not None and paused[0] == ev.worker_id:
                # 暂停后直接开工而非复工：按复工语义关闭暂停片段
                segments.append(
                    Segment(
                        worker_id=ev.worker_id,
                        start=paused[1],
                        end=ev.occurred_at,
                        kind="break",
                        closer_event=ev.event_id,
                    )
                )
                paused = None
            active = ev.worker_id
            active_since = ev.occurred_at
            opener_event = ev.event_id
            opener_reused = bool(ev.reused_digests)

        elif ev.type == "pause":
            if active == ev.worker_id:
                close_work(ev.occurred_at, ev)
                paused = (ev.worker_id, ev.occurred_at)
            elif active is not None:
                warn("pause_by_other_worker", ev, "他人正在作业，暂停事件被忽略", active_worker=active)
            else:
                warn("pause_without_session", ev, "没有进行中的作业，暂停事件被忽略")

        elif ev.type == "resume":
            if active is not None:
                warn("resume_while_active", ev, "作业进行中，复工事件被忽略")
            elif paused is None:
                warn("resume_without_pause", ev, "并无暂停记录，复工事件被忽略")
            elif paused[0] != ev.worker_id:
                warn("resume_by_other_worker", ev, "仅原暂停人可以复工", paused_worker=paused[0])
            else:
                segments.append(
                    Segment(
                        worker_id=ev.worker_id,
                        start=paused[1],
                        end=ev.occurred_at,
                        kind="break",
                        opener_event=None,
                        closer_event=ev.event_id,
                    )
                )
                paused = None
                active = ev.worker_id
                active_since = ev.occurred_at
                opener_event = ev.event_id
                opener_reused = bool(ev.reused_digests)

        elif ev.type == "handover":
            if not ev.handover_to:
                warn("handover_without_target", ev, "交接事件缺少 handover_to")
            elif ev.handover_to not in crew_member_ids:
                warn("handover_target_not_in_crew", ev, "交接对象不属于该班组", target=ev.handover_to)
            if active == ev.worker_id:
                close_work(ev.occurred_at, ev)
                warn(
                    "explicit_handover",
                    ev,
                    f"{ev.worker_id} 交接给 {ev.handover_to}",
                    from_worker=ev.worker_id,
                    to_worker=ev.handover_to,
                )
            elif paused is not None and paused[0] == ev.worker_id:
                warn("handover_from_pause", ev, "暂停状态下交接，暂停后不计工时")
                paused = None
            else:
                warn("handover_without_session", ev, "交接人没有进行中的作业")

        elif ev.type == "complete":
            if active is not None and active != ev.worker_id:
                warn(
                    "complete_by_other_worker",
                    ev,
                    "由非作业人提交完工，在该时点收尾作业",
                    active_worker=active,
                )
            close_work(ev.occurred_at, ev)
            paused = None
            completed = True

    unclosed: str | None = None
    if active is not None:
        unclosed = active
        warn(
            "unclosed_session",
            None,
            f"{active} 的作业缺少暂停/完工事件，需带班人按规则收尾或发起申诉",
            worker_id=active,
            opened_at=active_since.isoformat() if active_since else None,
        )

    # 带班人在复核时登记的收尾（受计划窗口与宽限期约束，区别于任意补数）
    closures = task.closures or {}
    if unclosed is not None and unclosed in closures:
        closure = closures[unclosed]
        closed_at = closure["closed_at"]
        last_ev = max(
            (e for e in events if e.worker_id == unclosed),
            key=lambda e: e.occurred_at,
            default=None,
        )
        earliest = active_since
        latest = task.planned_end + FOREMAN_CLOSE_GRACE
        if last_ev is not None:
            earliest = max(earliest or last_ev.occurred_at, last_ev.occurred_at)
        if earliest is not None and closed_at < earliest:
            warn("closure_before_last_event", None, "收尾时间早于最后事件，收尾无效", worker_id=unclosed)
        elif closed_at > latest:
            warn("closure_beyond_grace", None, "收尾时间超出计划窗口宽限期，收尾无效", worker_id=unclosed)
        else:
            close_work(
                closed_at,
                None,
                flags=["foreman_closed"],
            )
            segments[-1].closer_event = None
            unclosed = None

    # 按人汇总
    workers: dict[str, dict[str, Any]] = {}
    for seg in segments:
        bucket = workers.setdefault(
            seg.worker_id,
            {"worker_id": seg.worker_id, "work_minutes": 0, "flagged_minutes": 0, "segments": 0},
        )
        if seg.kind == "work":
            bucket["work_minutes"] += seg.minutes
            bucket["segments"] += 1
            if "photo_reuse" in seg.flags:
                bucket["flagged_minutes"] += seg.minutes

    return {
        "task_id": task.id,
        "status": task.status,
        "segments": [s.to_dict() for s in segments],
        "workers": list(workers.values()),
        "warnings": warnings,
        "completed": completed,
        "unclosed_worker": unclosed,
        "ordered_events": [
            {
                "event_id": e.event_id,
                "seq": e.seq,
                "type": e.type,
                "worker_id": e.worker_id,
                "occurred_at": e.occurred_at.isoformat(),
                "received_at": e.received_at.isoformat(),
                "photo_digest": e.photo_digest,
                "photo_reused": bool(e.reused_digests),
                "location_cell": e.location_cell,
                "location_in_range": e.location_in_range,
                "handover_to": e.handover_to,
                "note": e.note,
            }
            for e in events
        ],
    }


def worker_work_segments(store: Store) -> dict[str, list[dict[str, Any]]]:
    """汇总所有任务中每人的作业片段（含基地信息），用于跨基地冲突检测。"""

    index: dict[str, list[dict[str, Any]]] = {}
    for task in store.tasks.values():
        timeline = compute_timeline(store, task)
        plot = store.plots.get(task.plot_id)
        base_name = plot.base_name if plot else None
        for seg in timeline["segments"]:
            if seg["kind"] != "work":
                continue
            index.setdefault(seg["worker_id"], []).append(
                {
                    "task_id": task.id,
                    "base_name": base_name,
                    "start": datetime.fromisoformat(seg["start"]),
                    "end": datetime.fromisoformat(seg["end"]),
                    "minutes": seg["minutes"],
                    "flags": seg["flags"],
                }
            )
    for segs in index.values():
        segs.sort(key=lambda s: s["start"])
    return index


def _overlap_minutes(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> int:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0, int((end - start).total_seconds() // 60))


def compute_conflicts(store: Store, task_id: str | None = None) -> dict[str, Any]:
    """检测跨基地工时重叠、照片重复使用等需要解释的冲突。"""

    index = worker_work_segments(store)
    overlaps: list[dict[str, Any]] = []
    for worker_id, segs in index.items():
        for i, seg in enumerate(segs):
            if task_id is not None and seg["task_id"] != task_id:
                # 重叠涉及两个任务：只要其中一个是当前任务就应呈现，
                # 不能因另一任务的片段排序更早而漏掉
                pair_in_task = any(
                    other["task_id"] == task_id for other in segs[i + 1 :]
                )
                if not pair_in_task:
                    continue
            for other in segs[i + 1 :]:
                minutes = _overlap_minutes(seg["start"], seg["end"], other["start"], other["end"])
                if minutes <= 0:
                    continue
                overlaps.append(
                    {
                        "worker_id": worker_id,
                        "task_a": seg["task_id"],
                        "task_b": other["task_id"],
                        "base_a": seg["base_name"],
                        "base_b": other["base_name"],
                        "cross_base": seg["base_name"] != other["base_name"],
                        "minutes": minutes,
                        "start": max(seg["start"], other["start"]).isoformat(),
                        "end": min(seg["end"], other["end"]).isoformat(),
                    }
                )

    reuses = [
        {
            "photo_digest": item["digest"],
            "first_event": item["first_event"],
            "reused_event": item["reused_event"],
            "reused_task": item["reused_task"],
        }
        for item in store.digest_reuses
        if task_id is None or item["reused_task"] == task_id
    ]

    unclosed = []
    gaps = []
    for task in store.tasks.values():
        if task_id is not None and task.id != task_id:
            continue
        timeline = compute_timeline(store, task)
        if timeline["unclosed_worker"]:
            unclosed.append({"task_id": task.id, "worker_id": timeline["unclosed_worker"]})
        for w in timeline["warnings"]:
            if w["code"] == "sequence_gap":
                gaps.append({"task_id": task.id, **w})

    return {
        "overlaps": sorted(overlaps, key=lambda x: (-x["minutes"], x["worker_id"])),
        "photo_reuses": reuses,
        "unclosed_sessions": unclosed,
        "sequence_gaps": gaps,
    }


def _held_overlap_ranges(
    store: Store, task: Task
) -> dict[str, list[tuple[datetime, datetime]]]:
    """该任务每个工人与其他任务重叠的时间区间（并集），结算时暂扣。"""

    index = worker_work_segments(store)
    held: dict[str, list[tuple[datetime, datetime]]] = {}
    timeline = compute_timeline(store, task)
    for seg in timeline["segments"]:
        if seg["kind"] != "work":
            continue
        worker_id = seg["worker_id"]
        ranges: list[tuple[datetime, datetime]] = []
        s = datetime.fromisoformat(seg["start"])
        e = datetime.fromisoformat(seg["end"])
        for other in index.get(worker_id, []):
            if other["task_id"] == task.id:
                continue
            o_start = max(s, other["start"])
            o_end = min(e, other["end"])
            if o_end > o_start:
                ranges.append((o_start, o_end))
        if ranges:
            ranges.sort()
            merged = [ranges[0]]
            for r_start, r_end in ranges[1:]:
                if r_start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], r_end))
                else:
                    merged.append((r_start, r_end))
            held.setdefault(worker_id, []).extend(merged)
    return held


def build_settlement(store: Store, task: Task) -> dict[str, Any]:
    """按规则生成待结算工时：作业分钟扣除照片存疑与跨任务重叠分钟。

    申诉批准补发的分钟在批准阶段叠加。所有扣减项均保留明细，供发薪前核对。
    """

    timeline = compute_timeline(store, task)
    held_ranges = _held_overlap_ranges(store, task)
    plot = store.plots.get(task.plot_id)

    worker_rows: dict[str, dict[str, Any]] = {}
    for seg in timeline["segments"]:
        if seg["kind"] != "work":
            continue
        row = worker_rows.setdefault(
            seg["worker_id"],
            {
                "worker_id": seg["worker_id"],
                "work_minutes": 0,
                "photo_held_minutes": 0,
                "overlap_held_minutes": 0,
                "appeal_award_minutes": 0,
                "payable_minutes": 0,
                "bases": set(),
            },
        )
        row["work_minutes"] += seg["minutes"]
        if plot:
            row["bases"].add(plot.base_name)
        if "photo_reuse" in seg["flags"] and not task.photo_waiver:
            row["photo_held_minutes"] += seg["minutes"]

    for worker_id, ranges in held_ranges.items():
        row = worker_rows.setdefault(
            worker_id,
            {
                "worker_id": worker_id,
                "work_minutes": 0,
                "photo_held_minutes": 0,
                "overlap_held_minutes": 0,
                "appeal_award_minutes": 0,
                "payable_minutes": 0,
                "bases": {plot.base_name} if plot else set(),
            },
        )
        row["overlap_held_minutes"] += sum(
            int((e - s).total_seconds() // 60) for s, e in ranges
        )

    appeals = [a for a in store.appeals.values() if a.task_id == task.id]
    for appeal in appeals:
        if appeal.status == "upheld":
            row = worker_rows.setdefault(
                appeal.worker_id,
                {
                    "worker_id": appeal.worker_id,
                    "work_minutes": 0,
                    "photo_held_minutes": 0,
                    "overlap_held_minutes": 0,
                    "appeal_award_minutes": 0,
                    "payable_minutes": 0,
                    "bases": {plot.base_name} if plot else set(),
                },
            )
            row["appeal_award_minutes"] += appeal.award_minutes

    rows = []
    for row in worker_rows.values():
        # 照片暂扣与重叠暂扣可能覆盖同一分钟，总额以实际作业分钟为上限
        total_held = min(
            row["work_minutes"],
            row["photo_held_minutes"] + row["overlap_held_minutes"],
        )
        row["total_held_minutes"] = total_held
        row["payable_minutes"] = max(
            0,
            row["work_minutes"] - total_held + row["appeal_award_minutes"],
        )
        row["bases"] = sorted(row["bases"])
        rows.append(row)

    return {
        "task_id": task.id,
        "status": task.status,
        "plot_id": task.plot_id,
        "crew_id": task.crew_id,
        "base_name": plot.base_name if plot else None,
        "planned_window": {
            "start": task.planned_start.isoformat(),
            "end": task.planned_end.isoformat(),
        },
        "workers": sorted(rows, key=lambda r: r["worker_id"]),
        "total_payable_minutes": sum(r["payable_minutes"] for r in rows),
        "warnings": timeline["warnings"],
        "unclosed_worker": timeline["unclosed_worker"],
        "appeals": [
            {
                "id": a.id,
                "worker_id": a.worker_id,
                "category": a.category,
                "status": a.status,
                "claimed_minutes": a.claimed_minutes,
                "award_minutes": a.award_minutes,
            }
            for a in sorted(appeals, key=lambda a: a.created_at)
        ],
        "data_version": store.version,
    }
