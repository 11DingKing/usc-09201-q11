"""发薪前核验端到端场景（验收）：

两处基地并发补传离线记录 → 核对冲突提示 → 复核 → 发起一次工时申诉 →
生成待结算工时（申诉暂缓）→ 复核员裁定 → 重新生成（批准工时入账）→
核查隐私审计日志与位置最小化。
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from service.main import create_server


def find_line(run: dict, worker_id: str, task_id: str, day: str) -> dict:
    for line in run["lines"]:
        if (
            line["worker_id"] == worker_id
            and line["task_id"] == task_id
            and line["day"] == day
        ):
            return line
    raise AssertionError(f"缺少结算行: {worker_id} {task_id} {day}")


class PaydayFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _call(self, method, path, payload=None, actor=None, role=None):
        headers = {"Content-Type": "application/json"}
        if actor is not None:
            headers["X-Actor-Id"] = actor
            headers["X-Actor-Role"] = role
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _post(self, path, payload, actor, role):
        return self._call("POST", path, payload, actor, role)

    def _get(self, path, actor, role):
        return self._call("GET", path, None, actor, role)

    def test_payday_flow(self) -> None:
        # 1. 基地管理员登记两处基地的养护任务
        status, _ = self._post(
            "/tasks",
            {
                "task_id": "T-A1", "base_id": "base-a", "plot_id": "plot-1",
                "plot_location": {"lat": 29.4761, "lon": 109.3182},
                "team_id": "team-1", "title": "油茶林除草",
                "plan_start": "2026-09-18T00:00:00+08:00",
                "plan_end": "2026-09-21T00:00:00+08:00",
                "worker_ids": ["W1", "W2"],
            },
            "admin-a", "base_admin",
        )
        self.assertEqual(status, 201)
        status, _ = self._post(
            "/tasks",
            {
                "task_id": "T-B1", "base_id": "base-b", "plot_id": "plot-2",
                "plot_location": {"lat": 30.1156, "lon": 110.2249},
                "team_id": "team-2", "title": "黄精抚育",
                "plan_start": "2026-09-18T00:00:00+08:00",
                "plan_end": "2026-09-21T00:00:00+08:00",
                "worker_ids": ["W1", "W3"],
            },
            "admin-b", "base_admin",
        )
        self.assertEqual(status, 201)

        # 2. 发薪前，两处基地带班人并发补传离线记录（山区网络恢复后）
        batch_a = {
            "events": [
                # W1 上午在基地A：08:00-12:00（带证据与位置）
                {"event_id": "a-e1", "task_id": "T-A1", "worker_id": "W1", "kind": "start",
                 "occurred_at": "2026-09-19T08:00:00+08:00", "source": "leader-phone-a",
                 "evidence": {"digest": "photo-aaa", "captured_at": "2026-09-19T08:00:00+08:00",
                              "location": {"lat": 29.4760, "lon": 109.3180}}},
                {"event_id": "a-e2", "task_id": "T-A1", "worker_id": "W1", "kind": "complete",
                 "occurred_at": "2026-09-19T12:00:00+08:00", "source": "leader-phone-a"},
                # W2 在基地A：08:00 开工、10:00 暂停、10:30 复工、12:00 完工（倒序到达）
                {"event_id": "a-e6", "task_id": "T-A1", "worker_id": "W2", "kind": "complete",
                 "occurred_at": "2026-09-19T12:00:00+08:00", "source": "leader-phone-a"},
                {"event_id": "a-e3", "task_id": "T-A1", "worker_id": "W2", "kind": "start",
                 "occurred_at": "2026-09-19T08:00:00+08:00", "source": "leader-phone-a"},
                {"event_id": "a-e4", "task_id": "T-A1", "worker_id": "W2", "kind": "pause",
                 "occurred_at": "2026-09-19T10:00:00+08:00", "source": "leader-phone-a"},
                {"event_id": "a-e5", "task_id": "T-A1", "worker_id": "W2", "kind": "resume",
                 "occurred_at": "2026-09-19T10:30:00+08:00", "source": "leader-phone-a"},
                # W2 重复签名：08:02 又一条开工
                {"event_id": "a-e7", "task_id": "T-A1", "worker_id": "W2", "kind": "start",
                 "occurred_at": "2026-09-19T08:02:00+08:00", "source": "leader-phone-a"},
                # W1 傍晚跨午夜加班：20:00 - 次日 02:00
                {"event_id": "a-e8", "task_id": "T-A1", "worker_id": "W1", "kind": "start",
                 "occurred_at": "2026-09-19T20:00:00+08:00", "source": "leader-phone-a"},
                {"event_id": "a-e9", "task_id": "T-A1", "worker_id": "W1", "kind": "complete",
                 "occurred_at": "2026-09-20T02:00:00+08:00", "source": "leader-phone-a"},
            ]
        }
        batch_b = {
            "events": [
                # W1 同一上午又出现在基地B：10:00-12:30（与基地A重叠 10:00-12:00）
                {"event_id": "b-e1", "task_id": "T-B1", "worker_id": "W1", "kind": "start",
                 "occurred_at": "2026-09-19T10:00:00+08:00", "source": "leader-phone-b",
                 "evidence": {"digest": "photo-bbb",
                              "location": {"lat": 30.11, "lon": 110.22}}},
                {"event_id": "b-e2", "task_id": "T-B1", "worker_id": "W1", "kind": "complete",
                 "occurred_at": "2026-09-19T12:30:00+08:00", "source": "leader-phone-b"},
                # W3 在基地B 09:00-11:00，但用了 W1 在基地A 的同一照片摘要
                {"event_id": "b-e3", "task_id": "T-B1", "worker_id": "W3", "kind": "start",
                 "occurred_at": "2026-09-19T09:00:00+08:00", "source": "leader-phone-b",
                 "evidence": {"digest": "photo-aaa"}},
                {"event_id": "b-e4", "task_id": "T-B1", "worker_id": "W3", "kind": "complete",
                 "occurred_at": "2026-09-19T11:00:00+08:00", "source": "leader-phone-b"},
                # 临时换人未登记：W9 不在 T-B1 名册
                {"event_id": "b-e5", "task_id": "T-B1", "worker_id": "W9", "kind": "start",
                 "occurred_at": "2026-09-19T09:00:00+08:00", "source": "leader-phone-b"},
                {"event_id": "b-e6", "task_id": "T-B1", "worker_id": "W9", "kind": "complete",
                 "occurred_at": "2026-09-19T10:00:00+08:00", "source": "leader-phone-b"},
            ]
        }
        responses = []
        threads = [
            threading.Thread(
                target=lambda: responses.append(
                    self._post("/events/batch", batch_a, "leader-a", "leader")
                )
            ),
            threading.Thread(
                target=lambda: responses.append(
                    self._post("/events/batch", batch_b, "leader-b", "leader")
                )
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(responses), 2)
        created_counts = []
        for status, body in responses:
            self.assertEqual(status, 200)
            self.assertEqual(body["failed"], 0, body)
            created_counts.append(body["created"])
        self.assertEqual(sorted(created_counts), [6, 9])

        # 3. 复核员核对冲突提示：重叠、证据复用、重复签名、未登记派工、位置偏离
        status, body = self._get("/conflicts", "rev-1", "reviewer")
        self.assertEqual(status, 200)
        kinds = {item["kind"] for item in body["conflicts"]}
        self.assertIn("overlap_hours", kinds)
        self.assertIn("evidence_reused", kinds)
        self.assertIn("possible_duplicate", kinds)
        self.assertIn("unassigned_work", kinds)
        self.assertIn("location_mismatch", kinds)
        overlap = next(c for c in body["conflicts"] if c["kind"] == "overlap_hours")
        self.assertEqual(overlap["refs"]["worker_ids"], ["W1"])
        self.assertEqual(overlap["overlaps"], [{"day": "2026-09-19", "minutes": 120}])
        self.assertEqual(overlap["holder_task_id"], "T-B1")

        # 4. 补办临时换人登记后，W9 的未登记提示消除
        status, _ = self._post(
            "/tasks/T-B1/reassign",
            {"add": ["W9"], "effective_from": "2026-09-19T08:00:00+08:00",
             "reason": "W3 中途就医，W9 临时顶替"},
            "admin-b", "base_admin",
        )
        self.assertEqual(status, 200)
        _, body = self._get("/conflicts", "rev-1", "reviewer")
        self.assertNotIn("unassigned_work", {c["kind"] for c in body["conflicts"]})

        # 5. 复核员对五条时间线复核认可
        for index, (task_id, worker_id) in enumerate(
            [("T-A1", "W1"), ("T-A1", "W2"), ("T-B1", "W1"), ("T-B1", "W3"), ("T-B1", "W9")]
        ):
            status, _ = self._post(
                "/events",
                {"event_id": f"rev-{index}", "task_id": task_id, "worker_id": worker_id,
                 "kind": "review", "occurred_at": "2026-09-20T09:00:00+08:00",
                 "source": "review-console", "review_decision": "approve"},
                "rev-1", "reviewer",
            )
            self.assertEqual(status, 201)

        # 6. W2 发起工时申诉：暂停的 30 分钟实际在林地搬运苗木
        status, dispute = self._post(
            "/disputes",
            {"worker_id": "W2", "task_id": "T-A1", "day": "2026-09-19",
             "claimed_minutes": 240, "reason": "暂停期间实际在林地搬运苗木"},
            "W2", "worker",
        )
        self.assertEqual(status, 201)
        dispute_id = dispute["dispute_id"]

        # 7. 结算员生成待结算工时：申诉未决的部分暂缓
        status, run1 = self._post(
            "/settlements/run",
            {"period_start": "2026-09-19", "period_end": "2026-09-20"},
            "fin-1", "finance",
        )
        self.assertEqual(status, 201)
        line_w2 = find_line(run1, "W2", "T-A1", "2026-09-19")
        self.assertEqual(line_w2["status"], "disputed")
        self.assertEqual(line_w2["payable_minutes"], 0)
        self.assertEqual(line_w2["disputed_minutes"], 210)
        # W1：基地A 上午 240 + 傍晚 240 = 480；基地B 重叠暂扣 120，计 30
        line_w1_a = find_line(run1, "W1", "T-A1", "2026-09-19")
        self.assertEqual(line_w1_a["payable_minutes"], 480)
        line_w1_b = find_line(run1, "W1", "T-B1", "2026-09-19")
        self.assertEqual(line_w1_b["held_minutes"], 120)
        self.assertEqual(line_w1_b["payable_minutes"], 30)
        # 跨午夜：次日 02:00 前的一段计入 09-20
        line_w1_next = find_line(run1, "W1", "T-A1", "2026-09-20")
        self.assertEqual(line_w1_next["payable_minutes"], 120)
        # W9 临时顶替的 60 分钟在补登记后正常计入
        line_w9 = find_line(run1, "W9", "T-B1", "2026-09-19")
        self.assertEqual(line_w9["payable_minutes"], 60)

        # 8. 复核员裁定申诉：认可 240 分钟
        status, decided = self._post(
            f"/disputes/{dispute_id}/decision",
            {"outcome": "adjust", "adjusted_minutes": 240,
             "note": "带班人与两名同组村民证实搬运事实"},
            "rev-1", "reviewer",
        )
        self.assertEqual(status, 200)
        self.assertEqual(decided["status"], "resolved")

        # 9. 重新生成结算：批准工时入账，且每行可解释
        status, run2 = self._post(
            "/settlements/run",
            {"period_start": "2026-09-19", "period_end": "2026-09-20"},
            "fin-1", "finance",
        )
        self.assertEqual(status, 201)
        line_w2 = find_line(run2, "W2", "T-A1", "2026-09-19")
        self.assertEqual(line_w2["status"], "payable")
        self.assertEqual(line_w2["payable_minutes"], 240)
        status, explained = self._get(
            f"/settlements/{run2['run_id']}/lines/{line_w2['line_id']}/explain",
            "fin-1", "finance",
        )
        self.assertEqual(status, 200)
        rule_codes = [step["code"] for step in explained["explain"]["rules"]]
        self.assertIn("R1", rule_codes)
        self.assertIn("R5", rule_codes)
        self.assertEqual(explained["explain"]["dispute"]["dispute_id"], dispute_id)
        self.assertTrue(explained["explain"]["events"])

        # 10. 隐私：位置只存网格；个人数据访问全部留痕，仅审计员可查
        status, events = self._get("/tasks/T-A1/events", "rev-1", "reviewer")
        self.assertEqual(status, 200)
        raw = json.dumps(events, ensure_ascii=False)
        self.assertIn("29.48,109.32", raw)
        self.assertNotIn("29.4760", raw)
        status, audit = self._get("/audit-log", "aud-1", "auditor")
        self.assertEqual(status, 200)
        actions = {(entry["actor_id"], entry["action"]) for entry in audit["entries"]}
        self.assertIn(("fin-1", "run_settlement"), actions)
        self.assertIn(("rev-1", "decide_dispute"), actions)
        self.assertIn(("rev-1", "read_conflicts"), actions)
        status, _ = self._get("/audit-log", "fin-1", "finance")
        self.assertEqual(status, 403)
        status, _ = self._call("GET", "/conflicts")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
