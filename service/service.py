"""领域服务：任务、事件、申诉、结算与审计的应用入口。

所有写操作在锁内完成并先落日志；读操作基于一致性快照计算，
保证并发补传时结果可重放、可解释。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import conflicts as conflicts_mod
from . import rules as rules_mod
from .derive import build_timeline, timeline_to_dict
from .errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from .models import (
    DISPUTE_OUTCOMES,
    EVENT_KINDS,
    REVIEW_DECISIONS,
    AuditEntry,
    Dispute,
    EvidenceRef,
    RosterVersion,
    SettlementLine,
    SettlementRun,
    Task,
    WorkEvent,
)
from .privacy import Actor, require, to_cell
from .store import Store
from .timeutil import parse_dt

MAX_BATCH_SIZE = 500


def _need(payload: dict, *fields: str) -> None:
    missing = [field for field in fields if payload.get(field) in (None, "")]
    if missing:
        raise ValidationError(f"缺少必填字段: {', '.join(missing)}")


def _parse_dt_field(payload: dict, field: str) -> datetime:
    try:
        return parse_dt(payload[field])
    except (KeyError, ValueError) as exc:
        raise ValidationError(f"字段 {field} 无效: {exc}") from exc


def _parse_day(value: object, field: str = "day") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 格式的日期")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 格式的日期") from exc
    return value


def _fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Service:
    """林业灵活用工核验服务的应用门面。"""

    def __init__(self, store: Store | None = None, clock=None) -> None:
        self.store = store or Store()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ---- 任务与名册 ----

    def create_task(self, actor: Actor, payload: dict) -> dict:
        require(actor, {"base_admin"})
        _need(payload, "base_id", "plot_id", "team_id", "title", "plan_start", "plan_end")
        worker_ids = payload.get("worker_ids") or []
        if not isinstance(worker_ids, list) or not all(
            isinstance(item, str) and item for item in worker_ids
        ):
            raise ValidationError("worker_ids 必须是村民工号列表")
        plan_start = _parse_dt_field(payload, "plan_start")
        plan_end = _parse_dt_field(payload, "plan_end")
        if plan_end <= plan_start:
            raise ValidationError("plan_end 必须晚于 plan_start")
        plot_cell = None
        if payload.get("plot_location") is not None:
            location = payload["plot_location"]
            if not isinstance(location, dict):
                raise ValidationError("plot_location 必须是 {lat, lon} 对象")
            plot_cell = to_cell(location.get("lat"), location.get("lon"))
        elif payload.get("plot_cell"):
            plot_cell = str(payload["plot_cell"])

        now = self.clock()
        with self.store.lock:
            task_id = payload.get("task_id") or self.store.next_id("task")
            if task_id in self.store.tasks:
                raise ConflictError(f"任务 {task_id} 已存在")
            task = Task(
                task_id=task_id,
                base_id=payload["base_id"],
                plot_id=payload["plot_id"],
                plot_cell=plot_cell,
                team_id=payload["team_id"],
                title=payload["title"],
                plan_start=plan_start.isoformat(),
                plan_end=plan_end.isoformat(),
                created_at=now,
                roster=[
                    RosterVersion(
                        version=1,
                        effective_from=plan_start,
                        worker_ids=sorted(set(worker_ids)),
                        reason="初始派工",
                        recorded_at=now,
                    )
                ],
            )
            self.store.put_task(task)
        return task.to_dict()

    def reassign(self, actor: Actor, task_id: str, payload: dict) -> dict:
        """临时换人：生成新的名册版本，事件按发生时间套用对应版本。"""

        require(actor, {"base_admin"})
        _need(payload, "effective_from", "reason")
        add = {item for item in (payload.get("add") or []) if isinstance(item, str)}
        remove = {item for item in (payload.get("remove") or []) if isinstance(item, str)}
        if not add and not remove:
            raise ValidationError("add 与 remove 不能同时为空")
        effective_from = _parse_dt_field(payload, "effective_from")
        now = self.clock()
        with self.store.lock:
            task = self.store.tasks.get(task_id)
            if task is None:
                raise NotFoundError(f"任务不存在: {task_id}")
            current = set(task.roster[-1].worker_ids) if task.roster else set()
            new_ids = sorted((current | add) - remove)
            version = RosterVersion(
                version=(task.roster[-1].version + 1 if task.roster else 1),
                effective_from=effective_from,
                worker_ids=new_ids,
                reason=str(payload["reason"]),
                recorded_at=now,
            )
            task.roster.append(version)
            self.store.put_task(task)
        self._audit(
            actor,
            "reassign",
            f"task:{task_id}",
            f"临时换人：新增{sorted(add)} 移除{sorted(remove)}（{payload['reason']}）",
        )
        return task.to_dict()

    def get_task(self, actor: Actor, task_id: str) -> dict:
        require(actor, {"leader", "base_admin", "reviewer", "auditor"})
        task = self.store.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"任务不存在: {task_id}")
        return task.to_dict()

    # ---- 事件登记 ----

    def _build_evidence(self, data: object) -> EvidenceRef | None:
        if data is None:
            return None
        if not isinstance(data, dict) or not data.get("digest"):
            raise ValidationError("证据必须包含 digest（照片等材料的摘要值）")
        cell = None
        location = data.get("location")
        if location is not None:
            if not isinstance(location, dict):
                raise ValidationError("location 必须是 {lat, lon} 对象")
            cell = to_cell(location.get("lat"), location.get("lon"))
        elif data.get("location_cell"):
            cell = str(data["location_cell"])
        captured_at = data.get("captured_at")
        return EvidenceRef(
            digest=str(data["digest"]),
            captured_at=str(captured_at) if captured_at else None,
            location_cell=cell,
        )

    def ingest_event(self, actor: Actor, payload: dict) -> tuple[dict, bool]:
        """登记一条事件；同一 event_id 重复提交按幂等处理。"""

        require(actor, {"leader", "base_admin", "reviewer"})
        _need(payload, "event_id", "task_id", "worker_id", "kind", "occurred_at", "source")
        kind = payload["kind"]
        if kind not in EVENT_KINDS:
            raise ValidationError(f"未知事件类型: {kind}")
        if kind == "review":
            require(actor, {"reviewer", "base_admin"})
        occurred_at = _parse_dt_field(payload, "occurred_at")
        evidence = self._build_evidence(payload.get("evidence"))
        review_decision = None
        adjust_minutes = None
        if kind == "review":
            review_decision = payload.get("review_decision")
            if review_decision not in REVIEW_DECISIONS:
                raise ValidationError("复核事件必须给出 review_decision: approve/adjust")
            if review_decision == "adjust":
                adjust_minutes = payload.get("adjust_minutes")
                if not isinstance(adjust_minutes, int):
                    raise ValidationError("adjust 复核必须给出整数 adjust_minutes")

        fingerprint = _fingerprint(payload)
        now = self.clock()
        with self.store.lock:
            if payload["task_id"] not in self.store.tasks:
                raise NotFoundError(f"任务不存在: {payload['task_id']}")
            existing = self.store.events.get(payload["event_id"])
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ConflictError(
                        "同一 event_id 提交了不同内容，请使用新 event_id 并通过 supersedes 更正"
                    )
                return existing.to_dict(), False
            supersedes = payload.get("supersedes")
            if supersedes:
                old = self.store.events.get(supersedes)
                if old is None:
                    raise NotFoundError(f"被更正的事件不存在: {supersedes}")
                if (
                    old.task_id != payload["task_id"]
                    or old.worker_id != payload["worker_id"]
                ):
                    raise ValidationError("supersedes 只能更正同一任务同一村民的事件")
                old.status = "superseded"
                self.store.put_event(old)
            event = WorkEvent(
                event_id=payload["event_id"],
                task_id=payload["task_id"],
                worker_id=payload["worker_id"],
                kind=kind,
                occurred_at=occurred_at,
                source=str(payload["source"]),
                recorded_by=str(payload.get("recorded_by") or actor.actor_id),
                received_at=now,
                seq=self.store.next_seq(),
                note=str(payload.get("note") or ""),
                evidence=evidence,
                supersedes=supersedes,
                review_decision=review_decision,
                adjust_minutes=adjust_minutes,
                fingerprint=fingerprint,
            )
            self.store.put_event(event)
        return event.to_dict(), True

    def ingest_batch(self, actor: Actor, payload: dict) -> dict:
        """批量补传：逐条尽力而为，单条失败不影响整批。"""

        require(actor, {"leader", "base_admin", "reviewer"})
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            raise ValidationError("events 必须是非空列表")
        if len(events) > MAX_BATCH_SIZE:
            raise ValidationError(f"单批最多 {MAX_BATCH_SIZE} 条")
        results = []
        created = 0
        duplicated = 0
        failed = 0
        for index, item in enumerate(events):
            if not isinstance(item, dict):
                results.append(
                    {
                        "index": index,
                        "event_id": None,
                        "status": "error",
                        "error": {"code": "validation_error", "message": "事件必须是对象"},
                    }
                )
                failed += 1
                continue
            try:
                _, was_created = self.ingest_event(actor, item)
                if was_created:
                    created += 1
                    status = "created"
                else:
                    duplicated += 1
                    status = "duplicate"
                results.append(
                    {"index": index, "event_id": item.get("event_id"), "status": status}
                )
            except DomainError as exc:
                failed += 1
                results.append(
                    {
                        "index": index,
                        "event_id": item.get("event_id"),
                        "status": "error",
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
        return {
            "results": results,
            "created": created,
            "duplicated": duplicated,
            "failed": failed,
        }

    # ---- 推导与冲突 ----

    def task_timelines(self, actor: Actor, task_id: str) -> dict:
        require(actor, {"leader", "base_admin", "reviewer", "auditor"})
        tasks, events, _ = self.store.snapshot()
        if task_id not in tasks:
            raise NotFoundError(f"任务不存在: {task_id}")
        grouped: dict[str, list[WorkEvent]] = {}
        for event in events:
            if event.task_id == task_id:
                grouped.setdefault(event.worker_id, []).append(event)
        timelines = [
            timeline_to_dict(build_timeline(task_id, worker_id, grouped_events))
            for worker_id, grouped_events in sorted(grouped.items())
        ]
        self._audit(actor, "read_timelines", f"task:{task_id}", "查看任务时间线（含位置网格）")
        return {"task_id": task_id, "timelines": timelines}

    def task_events(self, actor: Actor, task_id: str) -> dict:
        require(actor, {"base_admin", "reviewer", "auditor"})
        tasks, events, _ = self.store.snapshot()
        if task_id not in tasks:
            raise NotFoundError(f"任务不存在: {task_id}")
        items = [
            event.to_dict()
            for event in sorted(events, key=lambda e: (e.occurred_at, e.seq))
            if event.task_id == task_id
        ]
        self._audit(actor, "read_events", f"task:{task_id}", "查看任务事件明细（含位置网格）")
        return {"task_id": task_id, "events": items}

    def list_conflicts(
        self,
        actor: Actor,
        base_id: str | None = None,
        worker_id: str | None = None,
        kind: str | None = None,
    ) -> dict:
        require(actor, {"base_admin", "reviewer", "auditor"})
        tasks, events, _ = self.store.snapshot()
        result = conflicts_mod.detect(events, tasks)
        if base_id:
            result = [item for item in result if base_id in item["refs"]["base_ids"]]
        if worker_id:
            result = [item for item in result if worker_id in item["refs"]["worker_ids"]]
        if kind:
            result = [item for item in result if item["kind"] == kind]
        self._audit(actor, "read_conflicts", "conflicts", "查看冲突提示")
        return {"conflicts": result, "count": len(result)}

    # ---- 申诉 ----

    def file_dispute(self, actor: Actor, payload: dict) -> dict:
        require(actor, {"worker", "leader", "base_admin"})
        _need(payload, "worker_id", "task_id", "day", "reason")
        if actor.role == "worker" and actor.actor_id != payload["worker_id"]:
            raise ForbiddenError("村民只能为本人发起申诉")
        day = _parse_day(payload["day"])
        claimed = payload.get("claimed_minutes")
        if claimed is not None and (not isinstance(claimed, int) or claimed < 0):
            raise ValidationError("claimed_minutes 必须是非负整数")
        now = self.clock()
        with self.store.lock:
            if payload["task_id"] not in self.store.tasks:
                raise NotFoundError(f"任务不存在: {payload['task_id']}")
            dispute_id = self.store.next_id("disp")
            dispute = Dispute(
                dispute_id=dispute_id,
                worker_id=payload["worker_id"],
                task_id=payload["task_id"],
                day=day,
                claimed_minutes=claimed,
                reason=str(payload["reason"]),
                filed_by=actor.actor_id,
                filed_role=actor.role,
                filed_at=now,
            )
            self.store.put_dispute(dispute)
        self._audit(
            actor,
            "file_dispute",
            f"dispute:{dispute_id}",
            f"发起工时申诉（{payload['worker_id']} {day}）",
        )
        return dispute.to_dict()

    def decide_dispute(self, actor: Actor, dispute_id: str, payload: dict) -> dict:
        require(actor, {"reviewer"})
        _need(payload, "outcome", "note")
        outcome = payload["outcome"]
        if outcome not in DISPUTE_OUTCOMES:
            raise ValidationError(f"outcome 必须是: {', '.join(DISPUTE_OUTCOMES)}")
        adjusted = payload.get("adjusted_minutes")
        if outcome == "adjust" and (not isinstance(adjusted, int) or adjusted < 0):
            raise ValidationError("adjust 裁定必须给出非负整数 adjusted_minutes")
        with self.store.lock:
            dispute = self.store.disputes.get(dispute_id)
            if dispute is None:
                raise NotFoundError(f"申诉不存在: {dispute_id}")
            if dispute.status != "open":
                raise ConflictError("申诉已裁定，不能重复决定")
            dispute.status = "resolved"
            dispute.decision = {
                "outcome": outcome,
                "adjusted_minutes": adjusted,
                "note": str(payload["note"]),
                "decided_by": actor.actor_id,
                "decided_at": self.clock().isoformat(),
            }
            self.store.put_dispute(dispute)
        self._audit(
            actor, "decide_dispute", f"dispute:{dispute_id}", f"裁定申诉：{outcome}"
        )
        return dispute.to_dict()

    def get_dispute(self, actor: Actor, dispute_id: str) -> dict:
        require(actor, {"worker", "leader", "base_admin", "reviewer", "auditor", "finance"})
        dispute = self.store.disputes.get(dispute_id)
        if dispute is None:
            raise NotFoundError(f"申诉不存在: {dispute_id}")
        if actor.role == "worker" and actor.actor_id != dispute.worker_id:
            raise ForbiddenError("村民只能查看本人申诉")
        self._audit(actor, "read_dispute", f"dispute:{dispute_id}", "查看申诉详情")
        return dispute.to_dict()

    def list_disputes(
        self,
        actor: Actor,
        status: str | None = None,
        worker_id: str | None = None,
    ) -> dict:
        require(actor, {"base_admin", "reviewer", "auditor", "finance"})
        items = [dispute.to_dict() for dispute in self.store.disputes.values()]
        if status:
            items = [item for item in items if item["status"] == status]
        if worker_id:
            items = [item for item in items if item["worker_id"] == worker_id]
        items.sort(key=lambda item: item["dispute_id"])
        self._audit(actor, "read_disputes", "disputes", "查看申诉列表")
        return {"disputes": items, "count": len(items)}

    # ---- 结算 ----

    def run_settlement(self, actor: Actor, payload: dict) -> dict:
        require(actor, {"finance", "reviewer"})
        _need(payload, "period_start", "period_end")
        period_start = _parse_day(payload["period_start"], "period_start")
        period_end = _parse_day(payload["period_end"], "period_end")
        if period_end < period_start:
            raise ValidationError("period_end 不能早于 period_start")
        base_id = payload.get("base_id")
        tasks, events, disputes = self.store.snapshot()
        drafts = rules_mod.compute_lines(
            tasks, events, disputes, period_start, period_end, base_id=base_id
        )
        with self.store.lock:
            run_id = self.store.next_id("run")
            lines = [
                SettlementLine(line_id=f"{run_id}-L{index:03d}", **draft)
                for index, draft in enumerate(drafts, start=1)
            ]
            summary = {
                "line_count": len(lines),
                "workers": len({line.worker_id for line in lines}),
                "payable_minutes": sum(line.payable_minutes for line in lines),
                "held_minutes": sum(line.held_minutes for line in lines),
                "disputed_minutes": sum(line.disputed_minutes for line in lines),
                "unreviewed_minutes": sum(line.unreviewed_minutes for line in lines),
                "capped_minutes": sum(line.capped_minutes for line in lines),
            }
            run = SettlementRun(
                run_id=run_id,
                period_start=period_start,
                period_end=period_end,
                base_id=base_id,
                rule_set=rules_mod.RULE_SET_ID,
                created_at=self.clock(),
                created_by=actor.actor_id,
                lines=lines,
                summary=summary,
            )
            self.store.put_run(run)
        self._audit(
            actor,
            "run_settlement",
            f"settlement:{run_id}",
            f"生成待结算工时（{period_start}~{period_end}）",
        )
        return run.to_dict()

    def get_run(self, actor: Actor, run_id: str) -> dict:
        require(actor, {"finance", "reviewer", "auditor"})
        run = self.store.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"结算批次不存在: {run_id}")
        self._audit(actor, "read_settlement", f"settlement:{run_id}", "查看结算批次")
        return run.to_dict()

    def list_runs(self, actor: Actor) -> dict:
        require(actor, {"finance", "reviewer", "auditor"})
        items = [
            {
                "run_id": run.run_id,
                "period_start": run.period_start,
                "period_end": run.period_end,
                "base_id": run.base_id,
                "rule_set": run.rule_set,
                "created_at": run.created_at.isoformat(),
                "created_by": run.created_by,
                "summary": run.summary,
            }
            for run in self.store.runs.values()
        ]
        items.sort(key=lambda item: item["run_id"])
        self._audit(actor, "read_settlements", "settlements", "查看结算批次列表")
        return {"runs": items, "count": len(items)}

    def explain_line(self, actor: Actor, run_id: str, line_id: str) -> dict:
        require(actor, {"finance", "reviewer", "auditor"})
        run = self.store.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"结算批次不存在: {run_id}")
        for line in run.lines:
            if line.line_id == line_id:
                self._audit(
                    actor,
                    "read_explain",
                    f"settlement:{run_id}",
                    f"查看结算行解释 {line_id}",
                )
                return line.to_dict()
        raise NotFoundError(f"结算行不存在: {line_id}")

    # ---- 村民工时 ----

    def worker_hours(
        self, actor: Actor, worker_id: str, day_from: str, day_to: str
    ) -> dict:
        if actor.role == "worker":
            if actor.actor_id != worker_id:
                raise ForbiddenError("村民只能查看本人工时")
        else:
            require(actor, {"finance", "reviewer", "auditor"})
        day_from = _parse_day(day_from, "from")
        day_to = _parse_day(day_to, "to")
        if day_to < day_from:
            raise ValidationError("to 不能早于 from")
        tasks, events, disputes = self.store.snapshot()
        drafts = rules_mod.compute_lines(
            tasks, events, disputes, day_from, day_to, worker_id=worker_id
        )
        result = rules_mod.aggregate_hours(drafts, worker_id, day_from, day_to)
        self._audit(actor, "read_hours", f"worker:{worker_id}", "读取村民工时")
        return result

    # ---- 隐私审计 ----

    def audit_log(self, actor: Actor, limit: int = 200) -> dict:
        require(actor, {"auditor"})
        with self.store.lock:
            entries = [entry.to_dict() for entry in self.store.audit[-limit:]][::-1]
        self._audit(actor, "read_audit", "audit-log", "查阅隐私审计日志")
        return {"entries": entries, "count": len(entries)}

    def _audit(self, actor: Actor, action: str, resource: str, detail: str) -> None:
        with self.store.lock:
            entry = AuditEntry(
                audit_id=self.store.next_id("audit"),
                at=self.clock(),
                actor_id=actor.actor_id,
                role=actor.role,
                action=action,
                resource=resource,
                detail=detail,
            )
            self.store.add_audit(entry)
