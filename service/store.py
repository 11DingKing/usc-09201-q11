"""存储层：内存索引 + 可选 JSONL 日志。

所有写入先追加日志再更新索引，重启后按日志重放恢复，
保证事件、申诉、结算批次与审计记录可持久追溯。
"""

from __future__ import annotations

import json
import os
import threading

from .models import AuditEntry, Dispute, SettlementRun, Task, WorkEvent


class Store:
    """线程安全的内存存储，可选 JSONL 追加日志用于重放。"""

    def __init__(self, journal_path: str | None = None) -> None:
        self.lock = threading.RLock()
        self.tasks: dict[str, Task] = {}
        self.events: dict[str, WorkEvent] = {}
        self.disputes: dict[str, Dispute] = {}
        self.runs: dict[str, SettlementRun] = {}
        self.audit: list[AuditEntry] = []
        self.seq = 0
        self._counters: dict[str, int] = {}
        self._journal_path = journal_path
        self._journal = None
        if journal_path:
            self._replay(journal_path)
            self._journal = open(journal_path, "a", encoding="utf-8")

    def close(self) -> None:
        if self._journal:
            self._journal.close()
            self._journal = None

    # ---- 日志重放 ----

    def _replay(self, path: str) -> None:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                self._apply(record["op"], record["data"])

    def _apply(self, op: str, data: dict) -> None:
        if op == "task":
            task = Task.from_dict(data)
            self.tasks[task.task_id] = task
            self._bump("task", task.task_id)
        elif op == "event":
            event = WorkEvent.from_dict(data)
            self.events[event.event_id] = event
            self.seq = max(self.seq, event.seq)
        elif op == "dispute":
            dispute = Dispute.from_dict(data)
            self.disputes[dispute.dispute_id] = dispute
            self._bump("disp", dispute.dispute_id)
        elif op == "run":
            run = SettlementRun.from_dict(data)
            self.runs[run.run_id] = run
            self._bump("run", run.run_id)
        elif op == "audit":
            entry = AuditEntry.from_dict(data)
            self.audit.append(entry)
            self._bump("audit", entry.audit_id)

    def _bump(self, prefix: str, raw_id: str) -> None:
        try:
            number = int(raw_id.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return
        self._counters[prefix] = max(self._counters.get(prefix, 0), number)

    def _write(self, op: str, data: dict) -> None:
        if self._journal:
            self._journal.write(
                json.dumps({"op": op, "data": data}, ensure_ascii=False) + "\n"
            )
            self._journal.flush()

    # ---- 序号与编号 ----

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def next_id(self, prefix: str) -> str:
        number = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = number
        return f"{prefix}-{number}"

    # ---- 写入（调用方需持有 lock） ----

    def put_task(self, task: Task) -> None:
        self._write("task", task.to_dict())
        self.tasks[task.task_id] = task

    def put_event(self, event: WorkEvent) -> None:
        self._write("event", event.to_dict())
        self.events[event.event_id] = event

    def put_dispute(self, dispute: Dispute) -> None:
        self._write("dispute", dispute.to_dict())
        self.disputes[dispute.dispute_id] = dispute

    def put_run(self, run: SettlementRun) -> None:
        self._write("run", run.to_dict())
        self.runs[run.run_id] = run

    def add_audit(self, entry: AuditEntry) -> None:
        self._write("audit", entry.to_dict())
        self.audit.append(entry)

    # ---- 一致性快照 ----

    def snapshot(self) -> tuple[dict[str, Task], list[WorkEvent], list[Dispute]]:
        """取一份读一致性快照，供推导与结算在锁外计算。"""

        with self.lock:
            return (
                dict(self.tasks),
                list(self.events.values()),
                list(self.disputes.values()),
            )
