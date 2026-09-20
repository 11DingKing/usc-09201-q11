"""隐私与审计测试：位置最小化、角色门禁与访问留痕。"""

from __future__ import annotations

import json
import unittest

from tests.helpers import Client, create_task, event_payload


class PrivacyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client()
        create_task(self.client, "T-A", worker_ids=["W1"])
        self.client.post(
            "/events",
            event_payload(
                "e-1", task_id="T-A", worker_id="W1", kind="start",
                evidence={
                    "digest": "photo-1",
                    "captured_at": "2026-09-19T08:00:00+08:00",
                    "location": {"lat": 29.4761, "lon": 109.3182},
                },
            ),
            actor="leader-1", role="leader",
        )

    def test_location_reduced_to_cell(self) -> None:
        status, body = self.client.get("/tasks/T-A/events", actor="rev-1", role="reviewer")
        self.assertEqual(status, 200)
        event = body["events"][0]
        self.assertEqual(event["evidence"]["location_cell"], "29.48,109.32")
        raw = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("29.4761", raw)
        self.assertNotIn("109.3182", raw)
        self.assertNotIn("lat", raw)

    def test_unauthenticated_rejected(self) -> None:
        status, body = self.client.get("/conflicts", actor=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        status, _ = self.client.get("/health", actor=None)
        self.assertEqual(status, 200)

    def test_unknown_role_rejected(self) -> None:
        status, _ = self.client.get("/conflicts", actor="x", role="superadmin")
        self.assertEqual(status, 401)

    def test_role_scope_enforced(self) -> None:
        # 结算员不能查看含位置网格的事件明细
        status, _ = self.client.get("/tasks/T-A/events", actor="fin-1", role="finance")
        self.assertEqual(status, 403)
        # 带班人不能生成结算
        status, _ = self.client.post(
            "/settlements/run",
            {"period_start": "2026-09-19", "period_end": "2026-09-19"},
            actor="leader-1", role="leader",
        )
        self.assertEqual(status, 403)

    def test_worker_reads_own_hours_only(self) -> None:
        status, _ = self.client.get("/workers/W1/hours", actor="W1", role="worker")
        self.assertEqual(status, 200)
        status, _ = self.client.get("/workers/W2/hours", actor="W1", role="worker")
        self.assertEqual(status, 403)

    def test_access_is_audited(self) -> None:
        self.client.get("/workers/W1/hours", actor="fin-1", role="finance")
        self.client.get("/tasks/T-A/timelines", actor="rev-1", role="reviewer")
        status, body = self.client.get("/audit-log", actor="aud-1", role="auditor")
        self.assertEqual(status, 200)
        actions = {(entry["actor_id"], entry["action"]) for entry in body["entries"]}
        self.assertIn(("fin-1", "read_hours"), actions)
        self.assertIn(("rev-1", "read_timelines"), actions)
        for entry in body["entries"]:
            self.assertTrue(entry["at"])
            self.assertTrue(entry["resource"])

    def test_audit_log_restricted_to_auditor(self) -> None:
        status, _ = self.client.get("/audit-log", actor="fin-1", role="finance")
        self.assertEqual(status, 403)
        status, _ = self.client.get("/audit-log", actor="rev-1", role="reviewer")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
