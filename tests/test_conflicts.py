"""冲突检测测试：重叠、证据复用、重复签名、未登记派工与位置偏离。"""

from __future__ import annotations

import unittest

from tests.helpers import Client, create_task, event_payload, task_payload


class ConflictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client()
        create_task(self.client, "T-A", worker_ids=["W1", "W2"])
        create_task(
            self.client,
            "T-B",
            base_id="base-b",
            plot_id="plot-2",
            plot_location={"lat": 30.1156, "lon": 110.2249},
            team_id="team-2",
            worker_ids=["W1", "W3"],
        )

    def _events(self, task_id: str, worker_id: str, start: str, end: str, prefix: str, evidence=None):
        self.client.post(
            "/events",
            event_payload(prefix + "-s", task_id=task_id, worker_id=worker_id,
                          kind="start", occurred_at=start, evidence=evidence),
            actor="leader-1", role="leader",
        )
        self.client.post(
            "/events",
            event_payload(prefix + "-c", task_id=task_id, worker_id=worker_id,
                          kind="complete", occurred_at=end),
            actor="leader-1", role="leader",
        )

    def _conflicts(self, **query):
        path = "/conflicts"
        if query:
            path += "?" + "&".join(f"{key}={value}" for key, value in query.items())
        status, body = self.client.get(path, actor="rev-1", role="reviewer")
        self.assertEqual(status, 200)
        return body["conflicts"]

    def test_cross_base_overlap_flagged_and_held_on_later_task(self) -> None:
        self._events("T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1")
        self._events("T-B", "W1", "2026-09-19T10:00:00+08:00", "2026-09-19T12:30:00+08:00", "b1")
        overlaps = [c for c in self._conflicts() if c["kind"] == "overlap_hours"]
        self.assertEqual(len(overlaps), 1)
        conflict = overlaps[0]
        self.assertEqual(conflict["severity"], "critical")
        self.assertEqual(conflict["refs"]["base_ids"], ["base-a", "base-b"])
        self.assertEqual(conflict["holder_task_id"], "T-B")
        self.assertEqual(conflict["overlaps"], [{"day": "2026-09-19", "minutes": 120}])

    def test_evidence_reused_flagged(self) -> None:
        evidence = {"digest": "photo-same", "captured_at": "2026-09-19T08:00:00+08:00"}
        self._events("T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1", evidence)
        self._events("T-B", "W3", "2026-09-19T09:00:00+08:00", "2026-09-19T11:00:00+08:00", "b1", evidence)
        reused = [c for c in self._conflicts() if c["kind"] == "evidence_reused"]
        self.assertEqual(len(reused), 1)
        self.assertEqual(set(reused[0]["refs"]["worker_ids"]), {"W1", "W3"})

    def test_possible_duplicate_signature(self) -> None:
        for event_id, at in (("e-1", "2026-09-19T08:00:00+08:00"), ("e-2", "2026-09-19T08:05:00+08:00")):
            self.client.post(
                "/events",
                event_payload(event_id, task_id="T-A", worker_id="W2", kind="start", occurred_at=at),
                actor="leader-1", role="leader",
            )
        duplicates = [c for c in self._conflicts() if c["kind"] == "possible_duplicate"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["refs"]["worker_ids"], ["W2"])

    def test_unassigned_work_flagged_but_recorded(self) -> None:
        self._events("T-B", "W9", "2026-09-19T09:00:00+08:00", "2026-09-19T10:00:00+08:00", "b9")
        unassigned = [c for c in self._conflicts() if c["kind"] == "unassigned_work"]
        self.assertEqual(len(unassigned), 2)  # 开工与完工各一条
        # 登记临时换人后提示消失
        self.client.post(
            "/tasks/T-B/reassign",
            {"add": ["W9"], "effective_from": "2026-09-19T08:00:00+08:00", "reason": "临时顶替"},
        )
        self.assertEqual(
            [c for c in self._conflicts() if c["kind"] == "unassigned_work"], []
        )

    def test_location_mismatch_info(self) -> None:
        evidence = {"digest": "photo-x", "location": {"lat": 31.5, "lon": 111.5}}
        self._events("T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T09:00:00+08:00", "a1", evidence)
        mismatch = [c for c in self._conflicts() if c["kind"] == "location_mismatch"]
        self.assertTrue(mismatch)
        self.assertEqual(mismatch[0]["severity"], "info")

    def test_conflict_ids_are_deterministic(self) -> None:
        self._events("T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1")
        self._events("T-B", "W1", "2026-09-19T10:00:00+08:00", "2026-09-19T12:30:00+08:00", "b1")
        first = [c["conflict_id"] for c in self._conflicts()]
        second = [c["conflict_id"] for c in self._conflicts()]
        self.assertEqual(first, second)

    def test_conflicts_filtered_by_base(self) -> None:
        self._events("T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1")
        self._events("T-B", "W1", "2026-09-19T10:00:00+08:00", "2026-09-19T12:30:00+08:00", "b1")
        only_a = self._conflicts(base_id="base-a")
        self.assertTrue(all("base-a" in c["refs"]["base_ids"] for c in only_a))


if __name__ == "__main__":
    unittest.main()
