"""领域实体与序列化。

所有业务记录都区分三件事：来源（source/recorded_by）、发生时间（occurred_at）、
当前有效版本（status/supersedes 或名册版本号）。事件不可变，更正只能以新事件
取代旧事件，旧事件保留用于追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .timeutil import parse_dt

EVENT_KINDS = ("start", "pause", "resume", "complete", "review")
EVENT_KIND_LABELS = {
    "start": "开工",
    "pause": "暂停",
    "resume": "复工",
    "complete": "完工",
    "review": "复核",
}
REVIEW_DECISIONS = ("approve", "adjust")
DISPUTE_OUTCOMES = ("approve", "adjust", "reject")


def _iso(moment: datetime) -> str:
    return moment.isoformat()


@dataclass
class EvidenceRef:
    """证据摘要：只保存摘要值与降精度位置网格，不保存原始照片与精确坐标。"""

    digest: str
    captured_at: str | None = None
    location_cell: str | None = None

    def to_dict(self) -> dict:
        return {
            "digest": self.digest,
            "captured_at": self.captured_at,
            "location_cell": self.location_cell,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceRef":
        return cls(
            digest=data["digest"],
            captured_at=data.get("captured_at"),
            location_cell=data.get("location_cell"),
        )


@dataclass
class RosterVersion:
    """班组名册的一个有效版本，临时换人会生成新版本。"""

    version: int
    effective_from: datetime
    worker_ids: list[str]
    reason: str
    recorded_at: datetime

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "effective_from": _iso(self.effective_from),
            "worker_ids": list(self.worker_ids),
            "reason": self.reason,
            "recorded_at": _iso(self.recorded_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RosterVersion":
        return cls(
            version=data["version"],
            effective_from=parse_dt(data["effective_from"]),
            worker_ids=list(data["worker_ids"]),
            reason=data.get("reason", ""),
            recorded_at=parse_dt(data["recorded_at"]),
        )


@dataclass
class Task:
    """养护任务：绑定基地、地块与班组，名册按版本演进。"""

    task_id: str
    base_id: str
    plot_id: str
    plot_cell: str | None
    team_id: str
    title: str
    plan_start: str
    plan_end: str
    created_at: datetime
    roster: list[RosterVersion] = field(default_factory=list)

    def roster_at(self, moment: datetime) -> list[str]:
        """返回某一时刻有效的名册（用于判断临时换人是否已登记）。"""

        chosen: list[str] = []
        for version in sorted(self.roster, key=lambda r: (r.effective_from, r.version)):
            if version.effective_from <= moment:
                chosen = version.worker_ids
        return list(chosen)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "base_id": self.base_id,
            "plot_id": self.plot_id,
            "plot_cell": self.plot_cell,
            "team_id": self.team_id,
            "title": self.title,
            "plan_start": self.plan_start,
            "plan_end": self.plan_end,
            "created_at": _iso(self.created_at),
            "roster": [rv.to_dict() for rv in self.roster],
            "current_worker_ids": list(self.roster[-1].worker_ids) if self.roster else [],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(
            task_id=data["task_id"],
            base_id=data["base_id"],
            plot_id=data["plot_id"],
            plot_cell=data.get("plot_cell"),
            team_id=data["team_id"],
            title=data["title"],
            plan_start=data["plan_start"],
            plan_end=data["plan_end"],
            created_at=parse_dt(data["created_at"]),
            roster=[RosterVersion.from_dict(rv) for rv in data.get("roster", [])],
        )


@dataclass
class WorkEvent:
    """作业事件：开工/暂停/复工/完工/复核，离线产生、可乱序补传。"""

    event_id: str
    task_id: str
    worker_id: str
    kind: str
    occurred_at: datetime
    source: str
    recorded_by: str
    received_at: datetime
    seq: int
    note: str = ""
    evidence: EvidenceRef | None = None
    supersedes: str | None = None
    status: str = "active"  # active / superseded
    review_decision: str | None = None
    adjust_minutes: int | None = None
    fingerprint: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "kind": self.kind,
            "kind_label": EVENT_KIND_LABELS.get(self.kind, self.kind),
            "occurred_at": _iso(self.occurred_at),
            "source": self.source,
            "recorded_by": self.recorded_by,
            "received_at": _iso(self.received_at),
            "seq": self.seq,
            "note": self.note,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "supersedes": self.supersedes,
            "status": self.status,
            "review_decision": self.review_decision,
            "adjust_minutes": self.adjust_minutes,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkEvent":
        evidence = data.get("evidence")
        return cls(
            event_id=data["event_id"],
            task_id=data["task_id"],
            worker_id=data["worker_id"],
            kind=data["kind"],
            occurred_at=parse_dt(data["occurred_at"]),
            source=data["source"],
            recorded_by=data["recorded_by"],
            received_at=parse_dt(data["received_at"]),
            seq=data["seq"],
            note=data.get("note", ""),
            evidence=EvidenceRef.from_dict(evidence) if evidence else None,
            supersedes=data.get("supersedes"),
            status=data.get("status", "active"),
            review_decision=data.get("review_decision"),
            adjust_minutes=data.get("adjust_minutes"),
            fingerprint=data.get("fingerprint", ""),
        )


@dataclass
class Dispute:
    """工时申诉：任何补数都必须走申诉加复核裁定，全程留痕。"""

    dispute_id: str
    worker_id: str
    task_id: str
    day: str
    claimed_minutes: int | None
    reason: str
    filed_by: str
    filed_role: str
    filed_at: datetime
    status: str = "open"  # open / resolved
    decision: dict | None = None

    def to_dict(self) -> dict:
        return {
            "dispute_id": self.dispute_id,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "day": self.day,
            "claimed_minutes": self.claimed_minutes,
            "reason": self.reason,
            "filed_by": self.filed_by,
            "filed_role": self.filed_role,
            "filed_at": _iso(self.filed_at),
            "status": self.status,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Dispute":
        return cls(
            dispute_id=data["dispute_id"],
            worker_id=data["worker_id"],
            task_id=data["task_id"],
            day=data["day"],
            claimed_minutes=data.get("claimed_minutes"),
            reason=data["reason"],
            filed_by=data["filed_by"],
            filed_role=data.get("filed_role", ""),
            filed_at=parse_dt(data["filed_at"]),
            status=data.get("status", "open"),
            decision=data.get("decision"),
        )


@dataclass
class SettlementLine:
    """待结算工时行：每（任务、村民、自然日）一行，含完整解释链。"""

    line_id: str
    worker_id: str
    task_id: str
    base_id: str
    day: str
    gross_minutes: int
    held_minutes: int
    disputed_minutes: int
    unreviewed_minutes: int
    capped_minutes: int
    payable_minutes: int
    status: str
    explain: dict

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "base_id": self.base_id,
            "day": self.day,
            "gross_minutes": self.gross_minutes,
            "held_minutes": self.held_minutes,
            "disputed_minutes": self.disputed_minutes,
            "unreviewed_minutes": self.unreviewed_minutes,
            "capped_minutes": self.capped_minutes,
            "payable_minutes": self.payable_minutes,
            "status": self.status,
            "explain": self.explain,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SettlementLine":
        return cls(
            line_id=data["line_id"],
            worker_id=data["worker_id"],
            task_id=data["task_id"],
            base_id=data["base_id"],
            day=data["day"],
            gross_minutes=data["gross_minutes"],
            held_minutes=data["held_minutes"],
            disputed_minutes=data["disputed_minutes"],
            unreviewed_minutes=data["unreviewed_minutes"],
            capped_minutes=data["capped_minutes"],
            payable_minutes=data["payable_minutes"],
            status=data["status"],
            explain=data.get("explain", {}),
        )


@dataclass
class SettlementRun:
    """一次结算生成：结果不可变，重新生成会产生新批次，便于对照。"""

    run_id: str
    period_start: str
    period_end: str
    base_id: str | None
    rule_set: str
    created_at: datetime
    created_by: str
    lines: list[SettlementLine]
    summary: dict

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "base_id": self.base_id,
            "rule_set": self.rule_set,
            "created_at": _iso(self.created_at),
            "created_by": self.created_by,
            "summary": self.summary,
            "lines": [line.to_dict() for line in self.lines],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SettlementRun":
        return cls(
            run_id=data["run_id"],
            period_start=data["period_start"],
            period_end=data["period_end"],
            base_id=data.get("base_id"),
            rule_set=data["rule_set"],
            created_at=parse_dt(data["created_at"]),
            created_by=data["created_by"],
            lines=[SettlementLine.from_dict(item) for item in data.get("lines", [])],
            summary=data.get("summary", {}),
        )


@dataclass
class AuditEntry:
    """隐私审计条目：谁在何时以什么角色访问了什么个人数据。"""

    audit_id: str
    at: datetime
    actor_id: str
    role: str
    action: str
    resource: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "audit_id": self.audit_id,
            "at": _iso(self.at),
            "actor_id": self.actor_id,
            "role": self.role,
            "action": self.action,
            "resource": self.resource,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEntry":
        return cls(
            audit_id=data["audit_id"],
            at=parse_dt(data["at"]),
            actor_id=data["actor_id"],
            role=data["role"],
            action=data["action"],
            resource=data["resource"],
            detail=data.get("detail", ""),
        )
