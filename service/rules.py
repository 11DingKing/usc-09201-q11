"""结算规则引擎：由事件流生成待结算工时。

每一步扣减都记录规则编码与来源事件，结算行可直接解释：
- R1 有效工时 = 开工到暂停/完工的闭合区间，暂停区间不计；
- R2 跨午夜作业按本地自然日切片；
- R3 同一村民每日计酬上限 10 小时，超出记为封顶扣减；
- R4 跨任务时间重叠只计一次，后到片段的重叠部分暂扣待核；
- R5 申诉未决的片段暂缓结算，裁定后按裁定入账；
- R6 未经复核的时间线不计入待结算；
- R7 复核调整按复核事件登记的调整分钟入账。
"""

from __future__ import annotations

from .conflicts import detect, find_overlap_pairs
from .derive import build_timeline, group_events, kind_label
from .models import Dispute, Task, WorkEvent
from .timeutil import day_key, slice_by_day

RULE_SET_ID = "forestry-hours/v1"
DAILY_CAP_MINUTES = 10 * 60

RULE_TEXT = {
    "R1": "有效工时=开工到暂停/完工的闭合区间",
    "R2": "跨午夜按本地自然日切片",
    "R3": "同一村民每日计酬上限10小时",
    "R4": "跨任务重叠只计一次，后到片段暂扣",
    "R5": "申诉未决暂缓，裁定后按裁定入账",
    "R6": "未经复核不计入待结算",
    "R7": "复核调整按复核事件入账",
}


def _event_summary(event: WorkEvent) -> dict:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "kind_label": kind_label(event.kind),
        "occurred_at": event.occurred_at.isoformat(),
        "source": event.source,
        "seq": event.seq,
    }


def compute_lines(
    tasks: dict[str, Task],
    events: list[WorkEvent],
    disputes: list[Dispute],
    period_start: str,
    period_end: str,
    base_id: str | None = None,
    worker_id: str | None = None,
) -> list[dict]:
    """计算期间内每（任务, 村民, 自然日）的工时草案，含解释链。

    输入为一致性快照，函数本身无副作用，可重复计算得到相同结果。
    """

    grouped = group_events(events)
    overlaps = find_overlap_pairs([e for e in events if e.status == "active"])

    held_map: dict[tuple[str, str, str], dict] = {}
    for pair in overlaps:
        for day, minutes in pair.day_minutes:
            key = (pair.holder_task, pair.worker_id, day)
            slot = held_map.setdefault(key, {"minutes": 0, "conflict_ids": set()})
            slot["minutes"] += minutes
            slot["conflict_ids"].add(pair.conflict_id)

    conflict_index: dict[tuple[str, str], set[str]] = {}
    for conflict in detect(events, tasks):
        for tid in conflict["refs"]["task_ids"]:
            for wid in conflict["refs"]["worker_ids"]:
                conflict_index.setdefault((tid, wid), set()).add(conflict["conflict_id"])

    dispute_map: dict[tuple[str, str, str], Dispute] = {}
    for dispute in sorted(disputes, key=lambda d: (d.filed_at, d.dispute_id)):
        dispute_map[(dispute.worker_id, dispute.task_id, dispute.day)] = dispute

    drafts: list[dict] = []
    for (task_id, wid), grouped_events in sorted(grouped.items()):
        task = tasks.get(task_id)
        if task is None:
            continue
        if base_id and task.base_id != base_id:
            continue
        if worker_id and wid != worker_id:
            continue
        timeline = build_timeline(task_id, wid, grouped_events)

        day_minutes: dict[str, int] = {}
        day_segments: dict[str, list] = {}
        for segment in timeline.segments:
            if segment.end_at is None:
                continue
            for day, minutes in slice_by_day(segment.start_at, segment.end_at):
                if period_start <= day <= period_end:
                    day_minutes[day] = day_minutes.get(day, 0) + minutes
                    day_segments.setdefault(day, []).append(segment)
        if not day_minutes:
            continue

        review = timeline.review
        adjustments: list[dict] = []
        if review and review.review_decision == "adjust" and review.adjust_minutes:
            remaining = review.adjust_minutes
            for day in sorted(day_minutes, reverse=True):
                if remaining == 0:
                    break
                adjusted = day_minutes[day] + remaining
                if adjusted >= 0:
                    adjustments.append(
                        {
                            "day": day,
                            "applied_minutes": remaining,
                            "rule": "R7",
                            "review_event": review.event_id,
                        }
                    )
                    day_minutes[day] = adjusted
                    remaining = 0
                else:
                    adjustments.append(
                        {
                            "day": day,
                            "applied_minutes": -day_minutes[day],
                            "rule": "R7",
                            "review_event": review.event_id,
                        }
                    )
                    day_minutes[day] = 0
                    remaining = adjusted
            if remaining:
                adjustments.append(
                    {
                        "unapplied_minutes": remaining,
                        "rule": "R7",
                        "review_event": review.event_id,
                        "note": "超出可调整工时，未入账",
                    }
                )

        reviewed = review is not None
        event_summaries = [
            _event_summary(event)
            for event in sorted(grouped_events, key=lambda e: (e.occurred_at, e.seq))
        ]

        for day in sorted(day_minutes):
            gross = day_minutes[day]
            segments = day_segments[day]
            held_info = held_map.get(
                (task_id, wid, day), {"minutes": 0, "conflict_ids": set()}
            )
            held = min(held_info["minutes"], gross)
            eligible = gross - held
            dispute = dispute_map.get((wid, task_id, day))

            steps: list[dict] = [
                {"code": "R1", "effect": f"闭合片段合计 {gross} 分钟"}
            ]
            if any(
                day_key(seg.start_at) != day_key(seg.end_at) for seg in segments
            ):
                steps.append({"code": "R2", "effect": "跨午夜片段已按自然日拆分"})
            for adjustment in adjustments:
                if adjustment.get("day") == day or "unapplied_minutes" in adjustment:
                    steps.append(
                        {
                            "code": "R7",
                            "effect": (
                                f"复核事件 {adjustment['review_event']} 调整 "
                                f"{adjustment.get('applied_minutes', adjustment.get('unapplied_minutes'))} 分钟"
                            ),
                        }
                    )
            if held:
                steps.append(
                    {
                        "code": "R4",
                        "effect": f"与他任务重叠，暂扣 {held} 分钟待核",
                        "conflict_ids": sorted(held_info["conflict_ids"]),
                    }
                )

            payable = 0
            disputed_minutes = 0
            unreviewed_minutes = 0
            if not reviewed:
                status = "pending_review"
                unreviewed_minutes = eligible
                steps.append({"code": "R6", "effect": "未经复核，暂缓计入待结算"})
            elif dispute and dispute.status == "open":
                status = "disputed"
                disputed_minutes = eligible
                steps.append(
                    {
                        "code": "R5",
                        "effect": f"申诉 {dispute.dispute_id} 未决，暂缓 {eligible} 分钟",
                    }
                )
            elif dispute and dispute.decision:
                outcome = dispute.decision["outcome"]
                if outcome == "approve":
                    payable = eligible
                    steps.append(
                        {"code": "R5", "effect": f"申诉 {dispute.dispute_id} 裁定认可，按记录入账"}
                    )
                elif outcome == "adjust":
                    payable = dispute.decision["adjusted_minutes"]
                    steps.append(
                        {
                            "code": "R5",
                            "effect": (
                                f"申诉 {dispute.dispute_id} 裁定调整为 {payable} 分钟"
                            ),
                        }
                    )
                else:
                    payable = 0
                    steps.append(
                        {"code": "R5", "effect": f"申诉 {dispute.dispute_id} 裁定驳回"}
                    )
                status = "payable" if payable else "rejected"
            else:
                payable = eligible
                status = "payable" if payable else "none"

            explain = {
                "events": event_summaries,
                "review": (
                    {
                        "event_id": review.event_id,
                        "decision": review.review_decision,
                        "adjust_minutes": review.adjust_minutes,
                        "recorded_by": review.recorded_by,
                    }
                    if review
                    else None
                ),
                "dispute": (
                    {
                        "dispute_id": dispute.dispute_id,
                        "status": dispute.status,
                        "reason": dispute.reason,
                        "decision": dispute.decision,
                    }
                    if dispute
                    else None
                ),
                "adjustments": [
                    item
                    for item in adjustments
                    if item.get("day") == day or "unapplied_minutes" in item
                ],
                "rules": steps,
                "conflict_ids": sorted(
                    conflict_index.get((task_id, wid), set())
                    | set(held_info["conflict_ids"])
                ),
            }
            drafts.append(
                {
                    "worker_id": wid,
                    "task_id": task_id,
                    "base_id": task.base_id,
                    "day": day,
                    "gross_minutes": gross,
                    "held_minutes": held,
                    "disputed_minutes": disputed_minutes,
                    "unreviewed_minutes": unreviewed_minutes,
                    "capped_minutes": 0,
                    "payable_minutes": payable,
                    "status": status,
                    "explain": explain,
                }
            )

    # R3 日计酬上限：按（村民, 日, 任务编号）顺序累计，超出部分记为封顶扣减
    drafts.sort(key=lambda item: (item["worker_id"], item["day"], item["task_id"]))
    cumulative: dict[tuple[str, str], int] = {}
    for draft in drafts:
        key = (draft["worker_id"], draft["day"])
        used = cumulative.get(key, 0)
        if used + draft["payable_minutes"] > DAILY_CAP_MINUTES:
            allowed = max(DAILY_CAP_MINUTES - used, 0)
            capped = draft["payable_minutes"] - allowed
            draft["capped_minutes"] = capped
            draft["payable_minutes"] = allowed
            draft["explain"]["rules"].append(
                {
                    "code": "R3",
                    "effect": (
                        f"当日计酬上限 {DAILY_CAP_MINUTES} 分钟，封顶扣减 {capped} 分钟"
                    ),
                }
            )
            if draft["payable_minutes"] == 0 and draft["status"] == "payable":
                draft["status"] = "capped"
        cumulative[key] = used + draft["payable_minutes"]

    return drafts


def aggregate_hours(
    drafts: list[dict], worker_id: str, day_from: str, day_to: str
) -> dict:
    """把工时草案按自然日汇总，供村民工时查询。"""

    days: dict[str, dict] = {}
    for draft in drafts:
        slot = days.setdefault(
            draft["day"],
            {
                "day": draft["day"],
                "payable_minutes": 0,
                "held_minutes": 0,
                "disputed_minutes": 0,
                "unreviewed_minutes": 0,
                "capped_minutes": 0,
                "tasks": [],
            },
        )
        for key in (
            "payable_minutes",
            "held_minutes",
            "disputed_minutes",
            "unreviewed_minutes",
            "capped_minutes",
        ):
            slot[key] += draft[key]
        slot["tasks"].append(
            {
                "task_id": draft["task_id"],
                "base_id": draft["base_id"],
                "status": draft["status"],
                "payable_minutes": draft["payable_minutes"],
                "held_minutes": draft["held_minutes"],
                "disputed_minutes": draft["disputed_minutes"],
            }
        )
    totals = {
        key: sum(day[key] for day in days.values())
        for key in (
            "payable_minutes",
            "held_minutes",
            "disputed_minutes",
            "unreviewed_minutes",
            "capped_minutes",
        )
    }
    return {
        "worker_id": worker_id,
        "from": day_from,
        "to": day_to,
        "rule_set": RULE_SET_ID,
        "days": [days[key] for key in sorted(days)],
        "totals": totals,
    }
