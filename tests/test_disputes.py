"""申诉流程测试：发起、权限、裁定与留痕。"""

from __future__ import annotations

import unittest

from tests.helpers import Client, create_task, event_payload


class DisputeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client()
        create_task(self.client, "T-A", worker_ids=["W1", "W2"])
        self.client.post(
            "/events",
            event_payload("e-1", task_id="T-A", worker_id="W2", kind="start"),
            actor="leader-1", role="leader",
        )

    def _file(self, actor="W2", role="worker", **overrides) -> tuple[int, dict]:
        payload = {
            "worker_id": "W2",
            "task_id": "T-A",
            "day": "2026-09-19",
            "reason": "工时少记",
        }
        payload.update(overrides)
        return self.client.post("/disputes", payload, actor=actor, role=role)

    def test_worker_can_only_file_for_self(self) -> None:
        status, _ = self._file(actor="W1", role="worker")
        self.assertEqual(status, 403)
        status, body = self._file(actor="W2", role="worker")
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "open")

    def test_validation(self) -> None:
        status, body = self._file(day="19/09/2026")
        self.assertEqual(status, 400)
        status, body = self._file(task_id="T-X")
        self.assertEqual(status, 404)
        status, body = self._file(reason="")
        self.assertEqual(status, 400)

    def test_decision_requires_reviewer(self) -> None:
        _, dispute = self._file()
        status, _ = self.client.post(
            f"/disputes/{dispute['dispute_id']}/decision",
            {"outcome": "approve", "note": "ok"},
            actor="fin-1", role="finance",
        )
        self.assertEqual(status, 403)

    def test_double_decision_rejected(self) -> None:
        _, dispute = self._file()
        decision = {"outcome": "approve", "note": "属实"}
        status, body = self.client.post(
            f"/disputes/{dispute['dispute_id']}/decision", decision,
            actor="rev-1", role="reviewer",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "resolved")
        status, _ = self.client.post(
            f"/disputes/{dispute['dispute_id']}/decision", decision,
            actor="rev-1", role="reviewer",
        )
        self.assertEqual(status, 409)

    def test_adjust_requires_minutes(self) -> None:
        _, dispute = self._file()
        status, body = self.client.post(
            f"/disputes/{dispute['dispute_id']}/decision",
            {"outcome": "adjust", "note": "缺分钟数"},
            actor="rev-1", role="reviewer",
        )
        self.assertEqual(status, 400)

    def test_worker_reads_own_dispute_only(self) -> None:
        _, dispute = self._file()
        status, _ = self.client.get(
            f"/disputes/{dispute['dispute_id']}", actor="W1", role="worker"
        )
        self.assertEqual(status, 403)
        status, _ = self.client.get(
            f"/disputes/{dispute['dispute_id']}", actor="W2", role="worker"
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
