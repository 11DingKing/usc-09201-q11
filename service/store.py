"""线程安全的内存存储与防篡改审计链。

所有业务数据只保存在内存中，便于在山区联盟内网节点上运行与测试；
生产部署可替换该模块的实现而不影响领域规则。审计条目按哈希链追加，
任何事后篡改都会在 ``/audit/verify`` 中暴露。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    """返回带时区的当前时间。"""

    return datetime.now(timezone.utc)


@dataclass
class Plot:
    """林地地块，归属某个基地，并登记允许作业的位置网格。"""

    id: str
    base_name: str
    name: str
    location_cells: set[str]


@dataclass
class Crew:
    """班组：一名带班人与若干成员。"""

    id: str
    name: str
    foreman_id: str
    member_ids: list[str]


@dataclass
class Worker:
    """务工村民。姓名属于个人信息，读取时按职责脱敏。"""

    id: str
    name: str
    crew_id: str


@dataclass
class Actor:
    """系统操作人，角色决定授权范围。"""

    id: str
    role: str
    name: str
    crew_id: str | None = None


@dataclass
class Task:
    """林下养护任务，绑定地块与班组，带计划作业窗口。"""

    id: str
    plot_id: str
    crew_id: str
    title: str
    planned_start: datetime
    planned_end: datetime
    status: str = "draft"  # draft（补传中）/ locked（已复核锁定）/ approved（已批准）
    locked_at: str | None = None
    approved_at: str | None = None
    location_purged: bool = False
    snapshot: dict[str, Any] | None = None
    # 带班人对未闭合会话的登记收尾：worker_id -> {"closed_at": dt, ...}
    closures: dict[str, Any] | None = None
    # 批准时对照片复用存疑工时是否认可（经申诉核实后可豁免暂扣）
    photo_waiver: bool = False


@dataclass
class Event:
    """开工/暂停/复工/完工/交接事件。

    位置只保存粗化后的网格单元，绝不保存经纬度原值；照片只保存摘要。
    """

    event_id: str
    task_id: str
    worker_id: str
    type: str
    occurred_at: datetime
    seq: int
    photo_digest: str | None
    location_cell: str | None
    location_in_range: bool
    note: str
    handover_to: str | None
    received_at: datetime
    receipt: int
    reused_digests: list[str] = field(default_factory=list)


@dataclass
class Appeal:
    """工时申诉。"""

    id: str
    task_id: str
    worker_id: str
    category: str  # overlap_dispute / unclosed_session
    ref: str | None
    claimed_minutes: int
    reason: str
    evidence_digests: list[str]
    filed_by: str
    created_at: str
    status: str = "open"  # open / upheld / rejected
    award_minutes: int = 0
    decision_reason: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None


@dataclass
class AccessRecord:
    """个人信息访问记录，用于隐私审计。"""

    ts: str
    actor_id: str
    resource: str
    fields: list[str]
    purpose: str


@dataclass
class AuditEntry:
    """追加式审计条目，含前一条哈希。"""

    seq: int
    ts: str
    actor_id: str
    action: str
    target: str
    detail: dict[str, Any]
    version: int
    prev_hash: str
    hash: str


class Store:
    """集中保存全部数据；任何读写都应在 ``lock`` 下进行。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.version = 1
        self.receipt = 0

        self.actors: dict[str, Actor] = {}
        self.plots: dict[str, Plot] = {}
        self.crews: dict[str, Crew] = {}
        self.workers: dict[str, Worker] = {}
        self.tasks: dict[str, Task] = {}
        self.events: dict[str, Event] = {}
        self.task_events: dict[str, list[Event]] = {}
        self.appeals: dict[str, Appeal] = {}

        # 照片摘要首次出现位置：event_id -> digest；digest -> 首个 event_id
        self.digest_first: dict[str, str] = {}
        self.digest_reuses: list[dict[str, str]] = []

        self.access_log: list[AccessRecord] = []
        self.audit: list[AuditEntry] = []
        self._add_audit("system", "service_initialized", "-", {})

    def bump_version(self) -> int:
        """产生新的数据版本（调用方需持锁）。"""

        self.version += 1
        return self.version

    def next_receipt(self) -> int:
        self.receipt += 1
        return self.receipt

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def _add_audit(
        self,
        actor_id: str,
        action: str,
        target: str,
        detail: dict[str, Any],
    ) -> AuditEntry:
        prev_hash = self.audit[-1].hash if self.audit else "0" * 64
        seq = len(self.audit) + 1
        entry_payload = {
            "seq": seq,
            "ts": utc_now().isoformat(),
            "actor_id": actor_id,
            "action": action,
            "target": target,
            "detail": detail,
            "version": self.version,
            "prev_hash": prev_hash,
        }
        digest = hashlib.sha256(
            (prev_hash + self._canonical(entry_payload)).encode("utf-8")
        ).hexdigest()
        entry = AuditEntry(hash=digest, **entry_payload)
        self.audit.append(entry)
        return entry

    def add_audit(
        self,
        actor_id: str,
        action: str,
        target: str,
        detail: dict[str, Any],
    ) -> AuditEntry:
        """追加审计条目（调用方需持锁）。"""

        return self._add_audit(actor_id, action, target, detail)

    def log_access(
        self,
        actor_id: str,
        resource: str,
        fields: list[str],
        purpose: str,
    ) -> None:
        self.access_log.append(
            AccessRecord(
                ts=utc_now().isoformat(),
                actor_id=actor_id,
                resource=resource,
                fields=fields,
                purpose=purpose,
            )
        )

    def verify_audit_chain(self) -> bool:
        """重新计算整条哈希链，检查审计记录是否被改动。"""

        prev_hash = "0" * 64
        for entry in self.audit:
            payload = {
                "seq": entry.seq,
                "ts": entry.ts,
                "actor_id": entry.actor_id,
                "action": entry.action,
                "target": entry.target,
                "detail": entry.detail,
                "version": entry.version,
                "prev_hash": prev_hash,
            }
            digest = hashlib.sha256(
                (prev_hash + self._canonical(payload)).encode("utf-8")
            ).hexdigest()
            if digest != entry.hash or entry.prev_hash != prev_hash:
                return False
            prev_hash = entry.hash
        return True
