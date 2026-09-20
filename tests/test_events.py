"""事件登记与时间线推导测试：乱序到达、跨午夜、更正与异常。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from service.timeutil import slice_by_day
from tests.helpers import Client, create_task, event_payload

TZ8 = timezone(timedelta(hours=8))


class IngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client()
        create_task(self.client)

    def _post_event(self, payload: dict, role: str = "leader", actor: str = "leader-1"):
        return self.client.post("/events", payload, actor=actor, role=role)

    def test_idempotent_reingest(self) -> None:
        payload = event_payload("e-1")
        status, _ = self._post_event(payload)
        self.assertEqual(status, 201)
        status, body = self._post_event(payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["event_id"], "e-1")
        # 同一 event_id 不同内容 → 冲突
        changed = dict(payload, occurred_at="2026-09-19T09:00:00+08:00")
        status, body = self._post_event(changed)
        self.assertEqual(status, 409)

    def test_out_of_order_arrival_reconstructs_timeline(self) -> None:
        # 倒序补传：完工先于开工到达
        events = [
            event_payload("e-3", kind="complete", occurred_at="2026-09-19T12:00:00+08:00"),
            event_payload("e-2", kind="pause", occurred_at="2026-09-19T10:00:00+08:00"),
            event_payload("e-1", kind="start", occurred_at="2026-09-19T08:00:00+08:00"),
            event_payload("e-4", kind="resume", occurred_at="2026-09-19T10:30:00+08:00"),
        ]
        for payload in events:
            status, body = self._post_event(payload)
            self.assertEqual(status, 201, body)
        status, body = self.client.get(
            "/tasks/T-1/timelines", actor="rev-1", role="reviewer"
        )
        self.assertEqual(status, 200)
        (timeline,) = body["timelines"]
        self.assertTrue(timeline["completed"])
        # 08:00-10:00 = 120，10:30-12:00 = 90
        self.assertEqual(timeline["total_minutes"], 210)
        self.assertEqual(len(timeline["segments"]), 2)

    def test_open_segment_flagged_not_counted(self) -> None:
        self._post_event(event_payload("e-1", kind="start"))
        status, body = self.client.get(
            "/tasks/T-1/timelines", actor="rev-1", role="reviewer"
        )
        (timeline,) = body["timelines"]
        self.assertEqual(timeline["total_minutes"], 0)
        codes = {item["code"] for item in timeline["anomalies"]}
        self.assertIn("open_segment", codes)

    def test_supersede_replaces_old_version(self) -> None:
        self._post_event(event_payload("e-1", kind="start"))
        self._post_event(event_payload("e-2", kind="complete", occurred_at="2026-09-19T12:00:00+08:00"))
        # 更正：完工时间应为 11:00，用新版本取代旧事件
        status, body = self._post_event(
            event_payload(
                "e-3",
                kind="complete",
                occurred_at="2026-09-19T11:00:00+08:00",
                supersedes="e-2",
            )
        )
        self.assertEqual(status, 201)
        status, body = self.client.get("/tasks/T-1/events", actor="rev-1", role="reviewer")
        versions = {item["event_id"]: item["status"] for item in body["events"]}
        self.assertEqual(versions["e-2"], "superseded")
        self.assertEqual(versions["e-3"], "active")
        status, body = self.client.get(
            "/tasks/T-1/timelines", actor="rev-1", role="reviewer"
        )
        (timeline,) = body["timelines"]
        self.assertEqual(timeline["total_minutes"], 180)

    def test_review_requires_reviewer_role(self) -> None:
        status, body = self._post_event(
            event_payload("e-9", kind="review", review_decision="approve"),
            role="leader",
        )
        self.assertEqual(status, 403)
        status, body = self.client.post(
            "/events",
            event_payload("e-9", kind="review", review_decision="approve"),
            actor="rev-1",
            role="reviewer",
        )
        self.assertEqual(status, 201, body)

    def test_batch_partial_failure_does_not_block_others(self) -> None:
        status, body = self.client.post(
            "/events/batch",
            {
                "events": [
                    event_payload("e-1", kind="start"),
                    {"event_id": "bad"},  # 缺字段
                    event_payload("e-2", kind="complete", occurred_at="2026-09-19T12:00:00+08:00"),
                ]
            },
            actor="leader-1",
            role="leader",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["created"], 2)
        self.assertEqual(body["failed"], 1)
        statuses = [item["status"] for item in body["results"]]
        self.assertEqual(statuses, ["created", "error", "created"])


class SliceTest(unittest.TestCase):
    def test_cross_midnight_split(self) -> None:
        start = datetime(2026, 9, 19, 20, 0, tzinfo=TZ8)
        end = datetime(2026, 9, 20, 2, 0, tzinfo=TZ8)
        self.assertEqual(
            slice_by_day(start, end),
            [("2026-09-19", 240), ("2026-09-20", 120)],
        )

    def test_same_day_slice(self) -> None:
        start = datetime(2026, 9, 19, 8, 0, tzinfo=TZ8)
        end = datetime(2026, 9, 19, 12, 0, tzinfo=TZ8)
        self.assertEqual(slice_by_day(start, end), [("2026-09-19", 240)])


if __name__ == "__main__":
    unittest.main()
