"""结算规则测试：R1~R7 各类入账、扣减与暂缓。"""

from __future__ import annotations

import unittest

from tests.helpers import Client, create_task, event_payload


def shift(client: Client, task_id: str, worker_id: str, start: str, end: str, prefix: str):
    """登记一对开工/完工事件。"""
    client.post(
        "/events",
        event_payload(f"{prefix}-s", task_id=task_id, worker_id=worker_id,
                      kind="start", occurred_at=start),
        actor="leader-1", role="leader",
    )
    client.post(
        "/events",
        event_payload(f"{prefix}-c", task_id=task_id, worker_id=worker_id,
                      kind="complete", occurred_at=end),
        actor="leader-1", role="leader",
    )


def review(client: Client, task_id: str, worker_id: str, event_id: str,
           decision: str = "approve", adjust_minutes: int | None = None):
    payload = event_payload(
        event_id, task_id=task_id, worker_id=worker_id, kind="review",
        occurred_at="2026-09-20T09:00:00+08:00", review_decision=decision,
    )
    if adjust_minutes is not None:
        payload["adjust_minutes"] = adjust_minutes
    status, body = client.post("/events", payload, actor="rev-1", role="reviewer")
    assert status == 201, body


def run(client: Client, start="2026-09-19", end="2026-09-20") -> dict:
    status, body = client.post(
        "/settlements/run",
        {"period_start": start, "period_end": end},
        actor="fin-1", role="finance",
    )
    assert status == 201, body
    return body


def line_of(run_body: dict, worker_id: str, task_id: str, day: str) -> dict:
    for line in run_body["lines"]:
        if (
            line["worker_id"] == worker_id
            and line["task_id"] == task_id
            and line["day"] == day
        ):
            return line
    raise AssertionError(f"缺少结算行: {worker_id} {task_id} {day}")


class SettlementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Client()
        create_task(self.client, "T-A", worker_ids=["W1", "W2"])
        create_task(
            self.client, "T-B", base_id="base-b", plot_id="plot-2",
            team_id="team-2", worker_ids=["W1"],
        )

    def test_pending_review_excluded_until_reviewed(self) -> None:
        shift(self.client, "T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1")
        first = run(self.client)
        line = line_of(first, "W1", "T-A", "2026-09-19")
        self.assertEqual(line["status"], "pending_review")
        self.assertEqual(line["payable_minutes"], 0)
        self.assertEqual(line["unreviewed_minutes"], 240)
        review(self.client, "T-A", "W1", "r-1")
        second = run(self.client)
        line = line_of(second, "W1", "T-A", "2026-09-19")
        self.assertEqual(line["status"], "payable")
        self.assertEqual(line["payable_minutes"], 240)

    def test_cross_midnight_split_into_two_days(self) -> None:
        shift(self.client, "T-A", "W1", "2026-09-19T20:00:00+08:00", "2026-09-20T02:00:00+08:00", "a1")
        review(self.client, "T-A", "W1", "r-1")
        body = run(self.client)
        day1 = line_of(body, "W1", "T-A", "2026-09-19")
        day2 = line_of(body, "W1", "T-A", "2026-09-20")
        self.assertEqual(day1["payable_minutes"], 240)
        self.assertEqual(day2["payable_minutes"], 120)
        codes = [step["code"] for step in day1["explain"]["rules"]]
        self.assertIn("R2", codes)

    def test_daily_cap_marks_excess(self) -> None:
        # 当天 6h + 5h = 11h，超出 10h 上限 60 分钟
        shift(self.client, "T-A", "W1", "2026-09-19T06:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1")
        shift(self.client, "T-B", "W1", "2026-09-19T13:00:00+08:00", "2026-09-19T18:00:00+08:00", "b1")
        review(self.client, "T-A", "W1", "r-1")
        review(self.client, "T-B", "W1", "r-2")
        body = run(self.client)
        line_a = line_of(body, "W1", "T-A", "2026-09-19")
        line_b = line_of(body, "W1", "T-B", "2026-09-19")
        self.assertEqual(line_a["payable_minutes"], 360)
        self.assertEqual(line_b["payable_minutes"], 240)
        self.assertEqual(line_b["capped_minutes"], 60)
        codes = [step["code"] for step in line_b["explain"]["rules"]]
        self.assertIn("R3", codes)

    def test_overlap_held_on_later_task(self) -> None:
        shift(self.client, "T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1")
        shift(self.client, "T-B", "W1", "2026-09-19T10:00:00+08:00", "2026-09-19T12:30:00+08:00", "b1")
        review(self.client, "T-A", "W1", "r-1")
        review(self.client, "T-B", "W1", "r-2")
        body = run(self.client)
        line_a = line_of(body, "W1", "T-A", "2026-09-19")
        line_b = line_of(body, "W1", "T-B", "2026-09-19")
        self.assertEqual(line_a["payable_minutes"], 240)
        self.assertEqual(line_b["held_minutes"], 120)
        self.assertEqual(line_b["payable_minutes"], 30)
        self.assertTrue(line_b["explain"]["conflict_ids"])

    def test_review_adjustment_applied(self) -> None:
        shift(self.client, "T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1")
        review(self.client, "T-A", "W1", "r-1", decision="adjust", adjust_minutes=-30)
        body = run(self.client)
        line = line_of(body, "W1", "T-A", "2026-09-19")
        self.assertEqual(line["gross_minutes"], 210)
        self.assertEqual(line["payable_minutes"], 210)
        self.assertEqual(line["explain"]["review"]["decision"], "adjust")

    def test_open_dispute_withholds_then_decision_pays(self) -> None:
        shift(self.client, "T-A", "W2", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a2")
        review(self.client, "T-A", "W2", "r-2")
        status, dispute = self.client.post(
            "/disputes",
            {"worker_id": "W2", "task_id": "T-A", "day": "2026-09-19",
             "claimed_minutes": 270, "reason": "完工后还清理了工具"},
            actor="W2", role="worker",
        )
        self.assertEqual(status, 201)
        first = run(self.client)
        line = line_of(first, "W2", "T-A", "2026-09-19")
        self.assertEqual(line["status"], "disputed")
        self.assertEqual(line["payable_minutes"], 0)
        self.assertEqual(line["disputed_minutes"], 240)
        # 复核员裁定调整为 270 分钟
        self.client.post(
            f"/disputes/{dispute['dispute_id']}/decision",
            {"outcome": "adjust", "adjusted_minutes": 270, "note": "带班人证实收尾工作"},
            actor="rev-1", role="reviewer",
        )
        second = run(self.client)
        line = line_of(second, "W2", "T-A", "2026-09-19")
        self.assertEqual(line["status"], "payable")
        self.assertEqual(line["payable_minutes"], 270)
        self.assertEqual(line["disputed_minutes"], 0)

    def test_rejected_dispute_zeroes_line(self) -> None:
        shift(self.client, "T-A", "W2", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a2")
        review(self.client, "T-A", "W2", "r-2")
        status, dispute = self.client.post(
            "/disputes",
            {"worker_id": "W2", "task_id": "T-A", "day": "2026-09-19", "reason": "test"},
            actor="leader-1", role="leader",
        )
        self.client.post(
            f"/disputes/{dispute['dispute_id']}/decision",
            {"outcome": "reject", "note": "与事实不符"},
            actor="rev-1", role="reviewer",
        )
        body = run(self.client)
        line = line_of(body, "W2", "T-A", "2026-09-19")
        self.assertEqual(line["status"], "rejected")
        self.assertEqual(line["payable_minutes"], 0)

    def test_worker_hours_aggregation(self) -> None:
        shift(self.client, "T-A", "W1", "2026-09-19T08:00:00+08:00", "2026-09-19T12:00:00+08:00", "a1")
        review(self.client, "T-A", "W1", "r-1")
        status, body = self.client.get(
            "/workers/W1/hours?from=2026-09-19&to=2026-09-20",
            actor="fin-1", role="finance",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["payable_minutes"], 240)
        self.assertEqual(body["days"][0]["day"], "2026-09-19")


if __name__ == "__main__":
    unittest.main()
