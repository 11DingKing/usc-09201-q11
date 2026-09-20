"""林业灵活用工核验服务 HTTP 入口。

路由概览
--------

登记：``POST /admin/actors|plots|crews|workers|tasks``
作业：``POST /tasks/{id}/events``（批量、离线幂等补传）、
      ``POST /tasks/{id}/closures``（带班人收尾未闭合会话）
复核：``POST /tasks/{id}/lock``、``GET /tasks/{id}/timeline|conflicts|settlement``
申诉：``POST /appeals``、``GET /appeals``、``POST /appeals/{id}/decision``
批准：``POST /tasks/{id}/approve``（联盟管理员，批准后清除位置网格）
隐私：``GET /workers/{id}``、``GET /audit/access``、
      ``GET /audit/verify``、``GET /audit``

鉴权通过 ``X-Actor-Id`` 头标识操作人；角色为 admin（联盟管理员）、
foreman（带班人）、member（务工村民）。
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from . import rules
from .rules import DomainError, parse_dt
from .store import Actor, Appeal, Crew, Event, Plot, Store, Task, Worker, utc_now

DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
CELL_RE = re.compile(r"^[A-Za-z0-9:_-]{1,64}$")

ROLES = {"admin", "foreman", "member"}


def seed_store() -> Store:
    """创建存储并内置一名联盟管理员，用于其余登记的授权引导。"""

    store = Store()
    admin = Actor(id="admin", role="admin", name="联盟管理员")
    store.actors[admin.id] = admin
    store.add_audit("admin", "actor_seeded", "admin", {"role": "admin"})
    return store


class Api:
    """持有存储并完成全部请求处理，便于测试直接调用。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or seed_store()

    # ------------------------------------------------------------------ 工具

    def actor(self, handler: BaseHTTPRequestHandler) -> Actor:
        actor_id = handler.headers.get("X-Actor-Id", "")
        actor = self.store.actors.get(actor_id)
        if actor is None:
            raise DomainError("unauthorized", "缺少或无法识别 X-Actor-Id")
        return actor

    def require_role(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise DomainError("forbidden", f"需要角色：{'/'.join(roles)}")

    def require_crew(self, actor: Actor, crew_id: str) -> None:
        if actor.role == "admin":
            return
        if actor.role != "foreman" or actor.crew_id != crew_id:
            raise DomainError("forbidden", "只能操作本班组任务")

    def get_task(self, task_id: str) -> Task:
        task = self.store.tasks.get(task_id)
        if task is None:
            raise DomainError("not_found", f"任务 {task_id} 不存在")
        return task

    # ------------------------------------------------------------------ 登记

    def register_actor(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        self.require_role(actor, "admin")
        self._fields(body, ("id", "role", "name"))
        if body["role"] not in ROLES:
            raise DomainError("invalid_role", "role 非法")
        if body["id"] in self.store.actors:
            raise DomainError("already_exists", "操作人已登记")
        crew_id = body.get("crew_id")
        if crew_id and crew_id not in self.store.crews:
            raise DomainError("unknown_crew", "crew_id 尚未登记")
        record = Actor(
            id=body["id"], role=body["role"], name=body["name"], crew_id=crew_id
        )
        self.store.actors[record.id] = record
        self.store.add_audit(
            actor.id, "actor_registered", record.id, {"role": record.role}
        )
        return {"id": record.id, "role": record.role}

    def register_plot(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        self.require_role(actor, "admin")
        self._fields(body, ("id", "base_name", "name"))
        cells = body.get("location_cells", [])
        if not isinstance(cells, list) or not cells:
            raise DomainError("invalid_cells", "location_cells 必须是非空网格编码列表")
        if any(not isinstance(c, str) or not CELL_RE.match(c) for c in cells):
            raise DomainError("invalid_cells", "网格编码格式非法")
        if body["id"] in self.store.plots:
            raise DomainError("already_exists", "地块已登记")
        plot = Plot(
            id=body["id"], base_name=body["base_name"], name=body["name"],
            location_cells=set(cells),
        )
        self.store.plots[plot.id] = plot
        self.store.add_audit(
            actor.id, "plot_registered", plot.id,
            {"base_name": plot.base_name, "cell_count": len(cells)},
        )
        return {"id": plot.id, "base_name": plot.base_name, "cells": len(cells)}

    def register_crew(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        self.require_role(actor, "admin")
        self._fields(body, ("id", "name", "foreman_id", "member_ids"))
        if not isinstance(body["member_ids"], list) or not body["member_ids"]:
            raise DomainError("invalid_members", "member_ids 必须是非空列表")
        if body["id"] in self.store.crews:
            raise DomainError("already_exists", "班组已登记")
        foreman = self.store.actors.get(body["foreman_id"])
        if foreman is None or foreman.role != "foreman":
            raise DomainError("unknown_foreman", "foreman_id 不是已登记带班人")
        crew = Crew(
            id=body["id"], name=body["name"], foreman_id=body["foreman_id"],
            member_ids=list(body["member_ids"]),
        )
        foreman.crew_id = crew.id
        self.store.crews[crew.id] = crew
        self.store.add_audit(
            actor.id, "crew_registered", crew.id,
            {"foreman_id": crew.foreman_id, "members": len(crew.member_ids)},
        )
        return {"id": crew.id, "foreman_id": crew.foreman_id}

    def register_worker(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        self.require_role(actor, "admin")
        self._fields(body, ("id", "name", "crew_id"))
        if body["crew_id"] not in self.store.crews:
            raise DomainError("unknown_crew", "crew_id 尚未登记")
        existing = self.store.workers.get(body["id"])
        if existing is not None:
            # 同一村民可在多个班组灵活务工：重复登记即追加班组名单，
            # 姓名必须与既有记录一致以防冒名
            if existing.name != body["name"]:
                raise DomainError("name_mismatch", "该编号已登记且姓名不一致")
            crew = self.store.crews[body["crew_id"]]
            if body["id"] not in crew.member_ids:
                crew.member_ids.append(body["id"])
                self.store.add_audit(
                    actor.id, "worker_crew_added", body["id"],
                    {"crew_id": body["crew_id"]},
                )
            return {"id": existing.id, "crew_id": body["crew_id"], "added": True}
        worker = Worker(id=body["id"], name=body["name"], crew_id=body["crew_id"])
        self.store.workers[worker.id] = worker
        crew = self.store.crews[body["crew_id"]]
        if worker.id not in crew.member_ids:
            crew.member_ids.append(worker.id)
        self.store.add_audit(
            actor.id, "worker_registered", worker.id, {"crew_id": worker.crew_id}
        )
        return {"id": worker.id}

    def register_task(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        self.require_role(actor, "admin", "foreman")
        self._fields(body, ("id", "plot_id", "crew_id", "title",
                            "planned_start", "planned_end"))
        if body["plot_id"] not in self.store.plots:
            raise DomainError("unknown_plot", "plot_id 尚未登记")
        if body["crew_id"] not in self.store.crews:
            raise DomainError("unknown_crew", "crew_id 尚未登记")
        self.require_crew(actor, body["crew_id"])
        start = parse_dt(body["planned_start"], "planned_start")
        end = parse_dt(body["planned_end"], "planned_end")
        if end <= start:
            raise DomainError("invalid_window", "计划完工必须晚于开工")
        if body["id"] in self.store.tasks:
            raise DomainError("already_exists", "任务已登记")
        task = Task(
            id=body["id"], plot_id=body["plot_id"], crew_id=body["crew_id"],
            title=body["title"], planned_start=start, planned_end=end,
            closures={},
        )
        self.store.tasks[task.id] = task
        self.store.task_events[task.id] = []
        self.store.bump_version()
        self.store.add_audit(
            actor.id, "task_registered", task.id,
            {"plot_id": task.plot_id, "crew_id": task.crew_id},
        )
        return {"id": task.id, "status": task.status}

    # ------------------------------------------------------------------ 事件

    def upload_events(
        self, actor: Actor, task_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        self.require_crew(actor, task.crew_id)
        if actor.role == "member":
            raise DomainError("forbidden", "事件由带班人统一登记补传")
        if task.status != "draft":
            raise DomainError("task_locked", "任务已锁定复核，不能再补传事件")
        raw_events = body.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            raise DomainError("invalid_events", "events 必须是非空列表")

        accepted: list[dict[str, Any]] = []
        duplicated: list[str] = []
        duplicated_seen: set[str] = set()
        plot = self.store.plots[task.plot_id]
        crew = self.store.crews[task.crew_id]

        # 第一遍：整批校验并暂存，任一事件非法则整批拒绝，避免部分入账
        staged: list[Event] = []
        staged_ids: set[str] = set()
        # 本批内新出现的摘要也要能识别"同批复用"
        batch_new_digests: dict[str, str] = {}
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise DomainError("invalid_event", "事件必须是对象")
            self._fields(raw, ("event_id", "worker_id", "type", "seq", "occurred_at"))
            event_id = str(raw["event_id"])
            if event_id in self.store.events or event_id in duplicated_seen:
                # 离线重试：幂等确认，绝不重复入账
                if event_id not in duplicated_seen:
                    duplicated.append(event_id)
                    duplicated_seen.add(event_id)
                continue
            if event_id in staged_ids:
                raise DomainError("duplicate_event_in_batch", f"批次内 event_id 重复：{event_id}")
            staged_ids.add(event_id)
            if raw["type"] not in rules.EVENT_TYPES:
                raise DomainError("invalid_event_type", f"事件类型非法：{raw['type']}")
            worker_id = str(raw["worker_id"])
            if worker_id not in crew.member_ids:
                raise DomainError("worker_not_in_crew", "事件提交人不属于该班组")
            seq = raw["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                raise DomainError("invalid_seq", "seq 必须是从 1 开始的整数")
            occurred = parse_dt(raw["occurred_at"], "occurred_at")
            if occurred > utc_now():
                raise DomainError("future_event", "事件发生时间不能晚于当前时间")
            if raw["type"] == "handover" and not raw.get("handover_to"):
                raise DomainError("handover_without_target", "交接事件缺少 handover_to")
            handover_to = raw.get("handover_to")
            if handover_to is not None and handover_to not in crew.member_ids:
                raise DomainError("handover_target_not_in_crew", "交接对象不属于该班组")

            forbidden = set(raw) & rules.FORBIDDEN_LOCATION_FIELDS
            if forbidden:
                raise DomainError(
                    "raw_location_forbidden",
                    f"禁止提交原始定位字段：{sorted(forbidden)}，仅接受网格编码",
                )
            cell = raw.get("location_cell")
            if cell is not None:
                if not isinstance(cell, str) or not CELL_RE.match(cell):
                    raise DomainError("invalid_cell", "location_cell 网格编码非法")
            in_range = cell is not None and cell in plot.location_cells

            digest = raw.get("photo_digest")
            if digest is not None and not DIGEST_RE.match(str(digest)):
                raise DomainError("invalid_digest", "photo_digest 必须是 64 位十六进制摘要")

            receipt = self.store.next_receipt()
            staged.append(
                Event(
                    event_id=event_id,
                    task_id=task.id,
                    worker_id=worker_id,
                    type=raw["type"],
                    occurred_at=occurred,
                    seq=seq,
                    photo_digest=digest,
                    location_cell=cell,
                    location_in_range=in_range,
                    note=str(raw.get("note", ""))[:200],
                    handover_to=handover_to,
                    received_at=utc_now(),
                    receipt=receipt,
                )
            )

        # 第二遍：全部合法后统一提交（含摘要复用判定与审计）
        for event in staged:
            reused: list[str] = []
            digest = event.photo_digest
            if digest:
                first = self.store.digest_first.get(digest) or batch_new_digests.get(digest)
                if first is not None and first != event.event_id:
                    reused.append(digest)
                    self.store.digest_reuses.append(
                        {
                            "digest": digest,
                            "first_event": first,
                            "reused_event": event.event_id,
                            "reused_task": task.id,
                        }
                    )
                else:
                    self.store.digest_first[digest] = event.event_id
                    batch_new_digests[digest] = event.event_id
            event.reused_digests = reused

            self.store.events[event.event_id] = event
            self.store.task_events[task.id].append(event)
            accepted.append(
                {
                    "event_id": event.event_id,
                    "seq": event.seq,
                    "receipt": event.receipt,
                    "photo_reused": bool(reused),
                    "location_in_range": event.location_in_range,
                }
            )
            self.store.add_audit(
                actor.id, "event_received", event.event_id,
                {
                    "task_id": task.id, "type": event.type, "seq": event.seq,
                    "occurred_at": event.occurred_at.isoformat(),
                    "received_at": event.received_at.isoformat(),
                    "late": (event.received_at - event.occurred_at) >= rules.LATE_THRESHOLD,
                    "photo_reused": bool(reused),
                    "location_in_range": event.location_in_range,
                    "has_location": event.location_cell is not None,
                },
            )

        self.store.bump_version()
        return {
            "task_id": task.id,
            "accepted": accepted,
            "duplicated": duplicated,
            "data_version": self.store.version,
        }

    def add_closures(
        self, actor: Actor, task_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        self.require_crew(actor, task.crew_id)
        if actor.role != "foreman":
            raise DomainError("forbidden", "只有带班人可以登记收尾")
        if task.status != "draft":
            raise DomainError("task_locked", "任务已锁定，不能再登记收尾")
        closures = body.get("closures")
        if not isinstance(closures, dict) or not closures:
            raise DomainError("invalid_closures", "closures 必须是非空映射")
        crew = self.store.crews[task.crew_id]
        registered: dict[str, str] = {}
        for worker_id, value in closures.items():
            if worker_id not in crew.member_ids:
                raise DomainError("worker_not_in_crew", f"{worker_id} 不属于该班组")
            closed_at = parse_dt(value, "收尾时间")
            if closed_at > utc_now():
                raise DomainError("future_event", "收尾时间不能晚于当前时间")
            task.closures[worker_id] = {"closed_at": closed_at, "by": actor.id}
            registered[worker_id] = closed_at.isoformat()
        self.store.bump_version()
        self.store.add_audit(
            actor.id, "closures_registered", task.id, {"workers": list(registered)}
        )
        return {"task_id": task.id, "closures": registered}

    # ------------------------------------------------------------------ 复核

    def lock_task(self, actor: Actor, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        self.require_crew(actor, task.crew_id)
        if actor.role != "foreman":
            raise DomainError("forbidden", "只有带班人可以提交复核")
        if task.status != "draft":
            raise DomainError("conflict", "任务不在待复核状态")
        if not self.store.task_events.get(task.id):
            raise DomainError("no_events", "没有任何事件，不能提交复核")
        timeline = rules.compute_timeline(self.store, task)
        if timeline["unclosed_worker"]:
            resolved = any(
                a.status == "upheld"
                and a.category == "unclosed_session"
                and a.worker_id == timeline["unclosed_worker"]
                for a in self.store.appeals.values()
                if a.task_id == task.id
            )
            if not resolved:
                raise DomainError(
                    "unclosed_session",
                    "仍有未闭合作业会话，请先登记收尾或等待申诉处理",
                )
        conflicts = rules.compute_conflicts(self.store, task.id)
        task.status = "locked"
        task.locked_at = utc_now().isoformat()
        task.snapshot = {
            "locked_at": task.locked_at,
            "timeline": timeline,
            "conflicts": conflicts,
            "data_version": self.store.version,
        }
        self.store.bump_version()
        self.store.add_audit(
            actor.id, "task_locked", task.id,
            {
                "warnings": len(timeline["warnings"]),
                "overlaps": len(conflicts["overlaps"]),
                "photo_reuses": len(conflicts["photo_reuses"]),
            },
        )
        return {
            "task_id": task.id,
            "status": task.status,
            "warnings": timeline["warnings"],
            "conflicts": conflicts,
        }

    def timeline_view(self, actor: Actor, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        self.require_crew(actor, task.crew_id)
        return rules.compute_timeline(self.store, task)

    def conflicts_view(
        self, actor: Actor, task_id: str | None = None
    ) -> dict[str, Any]:
        if task_id is not None:
            task = self.get_task(task_id)
            self.require_crew(actor, task.crew_id)
        else:
            # 全局冲突涉及跨基地全部班组，仅联盟管理员可查；
            # 带班人通过具体任务的 conflicts 视图查看本组相关冲突
            self.require_role(actor, "admin")
        return rules.compute_conflicts(self.store, task_id)

    def settlement_view(self, actor: Actor, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        self.require_crew(actor, task.crew_id)
        return rules.build_settlement(self.store, task)

    # ------------------------------------------------------------------ 申诉

    def file_appeal(self, actor: Actor, body: dict[str, Any]) -> dict[str, Any]:
        self.require_role(actor, "admin", "foreman", "member")
        self._fields(body, ("task_id", "category", "claimed_minutes", "reason"))
        task = self.get_task(body["task_id"])
        if actor.role == "foreman":
            self.require_crew(actor, task.crew_id)
        category = body["category"]
        if category not in ("overlap_dispute", "unclosed_session", "photo_dispute"):
            raise DomainError("invalid_category", "申诉类别非法")
        # 未闭合会话会阻止锁定，因此允许在补传阶段就申诉；
        # 重叠与照片争议在锁定复核时发起。
        if category == "unclosed_session":
            if task.status not in ("draft", "locked", "approved"):
                raise DomainError("task_not_reviewable", "任务状态不允许发起申诉")
        elif task.status not in ("locked", "approved"):
            raise DomainError("task_not_locked", "任务锁定后才能发起该类申诉")
        claimed = body["claimed_minutes"]
        if not isinstance(claimed, int) or isinstance(claimed, bool) or claimed <= 0:
            raise DomainError("invalid_minutes", "claimed_minutes 必须是正整数")
        if claimed > 24 * 60:
            raise DomainError("invalid_minutes", "单日申诉工时不能超过 24 小时")

        worker_id = body.get("worker_id", actor.id)
        if actor.role == "member":
            if body.get("worker_id") not in (None, actor.id):
                raise DomainError("forbidden", "组员只能为本人发起申诉")
            worker_id = actor.id
        crew = self.store.crews[task.crew_id]
        if worker_id not in crew.member_ids:
            raise DomainError("worker_not_in_crew", "申诉对象不属于该班组")

        conflicts = rules.compute_conflicts(self.store, task.id)
        timeline = rules.compute_timeline(self.store, task)
        ref = body.get("ref")
        if category == "overlap_dispute":
            pairs = {
                (o["task_a"], o["task_b"]): o for o in conflicts["overlaps"]
                if o["worker_id"] == worker_id
            }
            other = ref
            match = next(
                (o for o in conflicts["overlaps"]
                 if o["worker_id"] == worker_id
                 and (other is None or o["task_a"] == other or o["task_b"] == other)),
                None,
            )
            if match is None:
                raise DomainError(
                    "no_such_conflict",
                    "没有找到该村民对应的跨基地工时重叠，申诉必须针对真实冲突",
                )
            if claimed > match["minutes"]:
                raise DomainError(
                    "claim_exceeds_overlap",
                    f"申诉分钟不能超过重叠分钟 {match['minutes']}",
                )
            ref = match["task_b"] if match["task_a"] == task.id else match["task_a"]
        elif category == "unclosed_session":
            if timeline["unclosed_worker"] != worker_id:
                raise DomainError(
                    "no_unclosed_session", "该任务没有该村民待申诉的未闭合会话"
                )
        elif category == "photo_dispute":
            if ref is None or not any(
                r["reused_event"] == ref or r["first_event"] == ref
                for r in conflicts["photo_reuses"]
            ):
                raise DomainError("no_such_reuse", "ref 必须指向重复使用照片的事件")

        digests = body.get("evidence_digests", [])
        if not isinstance(digests, list) or any(
            not isinstance(d, str) or not DIGEST_RE.match(d) for d in digests
        ):
            raise DomainError("invalid_digest", "证据必须是 64 位十六进制摘要列表")

        appeal_id = f"AP{len(self.store.appeals) + 1:04d}"
        appeal = Appeal(
            id=appeal_id,
            task_id=task.id,
            worker_id=worker_id,
            category=category,
            ref=ref,
            claimed_minutes=claimed,
            reason=str(body["reason"])[:500],
            evidence_digests=list(digests),
            filed_by=actor.id,
            created_at=utc_now().isoformat(),
        )
        self.store.appeals[appeal.id] = appeal
        self.store.bump_version()
        self.store.add_audit(
            actor.id, "appeal_filed", appeal.id,
            {"task_id": task.id, "category": category, "claimed_minutes": claimed},
        )
        return self._appeal_dict(appeal)

    def decide_appeal(
        self, actor: Actor, appeal_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.require_role(actor, "admin")
        appeal = self.store.appeals.get(appeal_id)
        if appeal is None:
            raise DomainError("not_found", "申诉不存在")
        if appeal.status != "open":
            raise DomainError("conflict", "申诉已作出决定")
        decision = body.get("decision")
        if decision not in ("upheld", "rejected"):
            raise DomainError("invalid_decision", "decision 必须是 upheld/rejected")
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("reason_required", "必须填写决定理由，保证流程可解释")
        award = 0
        if decision == "upheld":
            award = body.get("award_minutes", appeal.claimed_minutes)
            if not isinstance(award, int) or isinstance(award, bool) or award <= 0:
                raise DomainError("invalid_minutes", "award_minutes 必须是正整数")
            if award > appeal.claimed_minutes:
                raise DomainError("award_exceeds_claim", "批准分钟不能超过申诉分钟")
        appeal.status = decision
        appeal.award_minutes = award
        appeal.decision_reason = reason.strip()[:500]
        appeal.decided_by = actor.id
        appeal.decided_at = utc_now().isoformat()
        self.store.bump_version()
        self.store.add_audit(
            actor.id, "appeal_decided", appeal.id,
            {"decision": decision, "award_minutes": award},
        )
        return self._appeal_dict(appeal)

    def list_appeals(self, actor: Actor, status: str | None) -> dict[str, Any]:
        appeals = list(self.store.appeals.values())
        if status:
            appeals = [a for a in appeals if a.status == status]
        if actor.role == "foreman":
            appeals = [
                a for a in appeals
                if self.store.tasks[a.task_id].crew_id == actor.crew_id
            ]
        elif actor.role == "member":
            appeals = [a for a in appeals if a.worker_id == actor.id]
        return {"appeals": [self._appeal_dict(a) for a in appeals]}

    @staticmethod
    def _appeal_dict(appeal: Appeal) -> dict[str, Any]:
        return {
            "id": appeal.id,
            "task_id": appeal.task_id,
            "worker_id": appeal.worker_id,
            "category": appeal.category,
            "ref": appeal.ref,
            "claimed_minutes": appeal.claimed_minutes,
            "reason": appeal.reason,
            "evidence_digests": appeal.evidence_digests,
            "filed_by": appeal.filed_by,
            "created_at": appeal.created_at,
            "status": appeal.status,
            "award_minutes": appeal.award_minutes,
            "decision_reason": appeal.decision_reason,
            "decided_by": appeal.decided_by,
            "decided_at": appeal.decided_at,
        }

    # ------------------------------------------------------------------ 批准

    def approve_task(self, actor: Actor, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.require_role(actor, "admin")
        task = self.get_task(task_id)
        if task.status != "locked":
            raise DomainError("conflict", "只有已锁定复核的任务才能批准")
        open_appeals = [
            a.id for a in self.store.appeals.values()
            if a.task_id == task.id and a.status == "open"
        ]
        if open_appeals:
            raise DomainError(
                "pending_appeal",
                f"存在未决申诉 {open_appeals}，须先处理再批准工时",
            )

        settlement = rules.build_settlement(self.store, task)
        task.photo_waiver = bool(body.get("photo_waiver", False))
        if task.photo_waiver:
            # 重新计算：经管理员核实认可的照片存疑分钟不再暂扣
            settlement = rules.build_settlement(self.store, task)

        # 批准即完成核验目的，删除全部网格位置，只保留是否在范围内的结论
        purged = 0
        for event in self.store.task_events.get(task.id, []):
            if event.location_cell is not None:
                event.location_cell = None
                purged += 1
        task.location_purged = True

        task.status = "approved"
        task.approved_at = utc_now().isoformat()
        task.snapshot = {
            "approved_at": task.approved_at,
            "settlement": settlement,
            "location_cells_purged": purged,
            "data_version": self.store.version,
        }
        # 锁时快照随之替换，确保任何位置网格都不残留
        task.snapshot = rules.strip_location_cells(task.snapshot)
        self.store.bump_version()
        self.store.add_audit(
            actor.id, "task_approved", task.id,
            {
                "total_payable_minutes": settlement["total_payable_minutes"],
                "photo_waiver": task.photo_waiver,
                "location_cells_purged": purged,
            },
        )
        return {
            "task_id": task.id,
            "status": task.status,
            "settlement": settlement,
            "location_cells_purged": purged,
        }

    # ------------------------------------------------------------------ 隐私

    def worker_view(self, actor: Actor, worker_id: str) -> dict[str, Any]:
        worker = self.store.workers.get(worker_id)
        if worker is None:
            raise DomainError("not_found", "村民不存在")
        fields: list[str] = []
        reveal = False
        purpose = ""
        if actor.role == "admin":
            reveal = True
            purpose = "联盟管理员履行核验职责"
            fields = ["name"]
        elif actor.role == "foreman" and actor.crew_id == worker.crew_id:
            reveal = True
            purpose = "带班人核对本班组工时"
            fields = ["name"]
        elif actor.id == worker.id:
            reveal = True
            purpose = "本人查询"
            fields = ["name"]

        self.store.log_access(
            actor.id, f"worker:{worker_id}", fields if reveal else [],
            purpose if reveal else "越权访问被拒绝（仅返回脱敏信息）",
        )
        self.store.add_audit(
            actor.id, "worker_accessed", worker_id,
            {"revealed_name": reveal, "actor_role": actor.role},
        )
        return {
            "id": worker.id,
            "crew_id": worker.crew_id,
            "name": worker.name if reveal else rules.mask_name(worker.name),
            "masked": not reveal,
        }

    def access_log_view(self, actor: Actor) -> dict[str, Any]:
        self.require_role(actor, "admin")
        return {
            "records": [
                {
                    "ts": r.ts, "actor_id": r.actor_id, "resource": r.resource,
                    "fields": r.fields, "purpose": r.purpose,
                }
                for r in self.store.access_log
            ]
        }

    def audit_view(self, actor: Actor) -> dict[str, Any]:
        self.require_role(actor, "admin")
        return {
            "entries": [
                {
                    "seq": e.seq, "ts": e.ts, "actor_id": e.actor_id,
                    "action": e.action, "target": e.target, "detail": e.detail,
                    "version": e.version, "hash": e.hash[:16] + "…",
                }
                for e in self.store.audit
            ],
            "data_version": self.store.version,
        }

    def audit_verify(self, actor: Actor) -> dict[str, Any]:
        self.require_role(actor, "admin")
        ok = self.store.verify_audit_chain()
        self.store.add_audit(
            actor.id, "audit_verified", "-", {"intact": ok}
        )
        return {"intact": ok, "entries": len(self.store.audit)}

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _fields(body: dict[str, Any], required: tuple[str, ...]) -> None:
        missing = [f for f in required if f not in body]
        if missing:
            raise DomainError("missing_fields", f"缺少必填字段：{missing}")


class Handler(BaseHTTPRequestHandler):
    """将 HTTP 请求路由到 :class:`Api`。"""

    api: Api  # 由 create_server 注入

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            # lock 等 POST 动作允许空请求体
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainError("invalid_json", "请求体不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise DomainError("invalid_body", "请求体必须是 JSON 对象")
        return body

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {
            key: value
            for key, value in (
                pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair
            )
        }
        with self.api.store.lock:
            try:
                actor = self.api.actor(self) if path != "/health" else None
                body = self._read_body() if method in ("POST", "PUT") else {}
                result = self._route(method, path, query, body, actor)
                self._send(200, result if result is not None else {"status": "ok"})
            except DomainError as exc:
                status = {
                    "unauthorized": 401,
                    "forbidden": 403,
                    "not_found": 404,
                }.get(exc.code, 400)
                if exc.code in ("already_exists", "conflict", "task_locked",
                                "pending_appeal"):
                    status = 409
                self._send(status, {"error": exc.code, "message": str(exc)})

    def _route(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: dict[str, Any],
        actor: Actor | None,
    ) -> dict[str, Any] | None:
        if method == "GET" and path == "/health":
            return {"status": "ok"}

        assert actor is not None
        segments = [s for s in path.split("/") if s]

        if method == "POST" and len(segments) == 2 and segments[0] == "admin":
            return {
                "actors": self.api.register_actor,
                "plots": self.api.register_plot,
                "crews": self.api.register_crew,
                "workers": self.api.register_worker,
                "tasks": self.api.register_task,
            }[segments[1]](actor, body)

        if len(segments) == 3 and segments[0] == "tasks":
            task_id = segments[1]
            action = segments[2]
            if method == "POST" and action == "events":
                return self.api.upload_events(actor, task_id, body)
            if method == "POST" and action == "closures":
                return self.api.add_closures(actor, task_id, body)
            if method == "POST" and action == "lock":
                return self.api.lock_task(actor, task_id)
            if method == "POST" and action == "approve":
                return self.api.approve_task(actor, task_id, body)
            if method == "GET" and action == "timeline":
                return self.api.timeline_view(actor, task_id)
            if method == "GET" and action == "settlement":
                return self.api.settlement_view(actor, task_id)
            if method == "GET" and action == "conflicts":
                return self.api.conflicts_view(actor, task_id)

        if method == "POST" and path == "/appeals":
            return self.api.file_appeal(actor, body)
        if method == "GET" and path == "/appeals":
            return self.api.list_appeals(actor, query.get("status"))
        if method == "POST" and len(segments) == 3 and segments[0] == "appeals":
            return self.api.decide_appeal(actor, segments[1], body)

        if method == "GET" and len(segments) == 2 and segments[0] == "workers":
            return self.api.worker_view(actor, segments[1])
        if method == "GET" and path == "/conflicts":
            return self.api.conflicts_view(actor)
        if method == "GET" and path == "/audit/access":
            return self.api.access_log_view(actor)
        if method == "GET" and path == "/audit/verify":
            return self.api.audit_verify(actor)
        if method == "GET" and path == "/audit":
            return self.api.audit_view(actor)

        raise DomainError("not_found", "路径不存在")

    def do_GET(self) -> None:  # noqa: N802
        self._safe_dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._safe_dispatch("POST")

    def _safe_dispatch(self, method: str) -> None:
        try:
            self._dispatch(method)
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"error": "internal", "message": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str = "0.0.0.0", port: int = 0) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例。"""

    api = Api()

    class BoundHandler(Handler):
        pass

    BoundHandler.api = api
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.api = api  # type: ignore[attr-defined]
    return server


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
