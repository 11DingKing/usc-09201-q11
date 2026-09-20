"""JSONL 日志持久化测试：写入后可重放恢复，编号不冲突。"""

from __future__ import annotations

import os
import tempfile
import unittest

from service.service import Service
from service.store import Store
from tests.helpers import Client, create_task, event_payload


class JournalTest(unittest.TestCase):
    def test_replay_restores_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            service = Service(Store(journal_path=path))
            client = Client(service)
            create_task(client, "T-A", worker_ids=["W1"])
            client.post(
                "/events",
                event_payload("e-1", task_id="T-A", worker_id="W1", kind="start"),
                actor="leader-1", role="leader",
            )
            client.post(
                "/disputes",
                {"worker_id": "W1", "task_id": "T-A", "day": "2026-09-19", "reason": "少记"},
                actor="W1", role="worker",
            )
            client.get("/workers/W1/hours", actor="fin-1", role="finance")
            service.store.close()

            # 用同一日志文件重建服务
            restored = Service(Store(journal_path=path))
            client2 = Client(restored)
            status, body = client2.get("/tasks/T-A", actor="rev-1", role="reviewer")
            self.assertEqual(status, 200)
            self.assertEqual(body["task_id"], "T-A")
            status, body = client2.get("/disputes", actor="rev-1", role="reviewer")
            self.assertEqual(body["count"], 1)
            status, body = client2.get("/audit-log", actor="aud-1", role="auditor")
            actions = {entry["action"] for entry in body["entries"]}
            self.assertIn("read_hours", actions)
            # 新写入编号不与重放内容冲突
            status, dispute = client2.post(
                "/disputes",
                {"worker_id": "W1", "task_id": "T-A", "day": "2026-09-20", "reason": "再申诉"},
                actor="W1", role="worker",
            )
            self.assertEqual(status, 201)
            self.assertEqual(dispute["dispute_id"], "disp-2")
            restored.store.close()

    def test_journal_never_stores_raw_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            service = Service(Store(journal_path=path))
            client = Client(service)
            create_task(client, "T-A", worker_ids=["W1"])
            client.post(
                "/events",
                event_payload(
                    "e-1", task_id="T-A", worker_id="W1", kind="start",
                    evidence={"digest": "p-1", "location": {"lat": 29.4761, "lon": 109.3182}},
                ),
                actor="leader-1", role="leader",
            )
            service.store.close()
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("29.48,109.32", content)
            self.assertNotIn("29.4761", content)


if __name__ == "__main__":
    unittest.main()
