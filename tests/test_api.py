"""端到端核验流程测试：通过真实 HTTP 服务驱动全部接口。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from service.main import create_server


class Client:
    """简单 JSON HTTP 客户端。"""

    def __init__(self, base: str) -> None:
        self.base = base

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        actor: str | None = "admin",
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Actor-Id": actor or "",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)


class ServiceTest(unittest.TestCase):
    """搭建两基地、两班组、共用村民的联盟场景。"""

    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.api = Client(f"http://{host}:{port}")

        # 两名带班人、组员操作人（w1 同时是 c1/c2 的村民与组员操作人）
        for actor in (
            {"id": "f1", "role": "foreman", "name": "雷带班"},
            {"id": "f2", "role": "foreman", "name": "蓝带班"},
            {"id": "w1", "role": "member", "name": "王五"},
            {"id": "w2", "role": "member", "name": "周三"},
            {"id": "w3", "role": "member", "name": "吴四"},
        ):
            status, _ = self.api.request("POST", "/admin/actors", actor)
            self.assertEqual(status, 200)

        # 两处基地的地块
        for plot in (
            {"id": "pA", "base_name": "青溪基地", "name": "一号竹笋林",
             "location_cells": ["QX:01", "QX:02"]},
            {"id": "pB", "base_name": "云岭基地", "name": "二号油茶林",
             "location_cells": ["YL:07"]},
        ):
            status, _ = self.api.request("POST", "/admin/plots", plot)
            self.assertEqual(status, 200)

        # 两个班组
        for crew in (
            {"id": "c1", "name": "青溪一班", "foreman_id": "f1",
             "member_ids": ["w1", "w2", "w3"]},
            {"id": "c2", "name": "云岭二班", "foreman_id": "f2",
             "member_ids": ["w1"]},
        ):
            status, _ = self.api.request("POST", "/admin/crews", crew)
            self.assertEqual(status, 200)

        # 村民：w1 同时在两个班组灵活务工
        for worker in (
            {"id": "w1", "name": "王五", "crew_id": "c1"},
            {"id": "w2", "name": "周三", "crew_id": "c1"},
            {"id": "w3", "name": "吴四", "crew_id": "c1"},
        ):
            status, _ = self.api.request("POST", "/admin/workers", worker)
            self.assertEqual(status, 200)
        status, body = self.api.request(
            "POST", "/admin/workers", {"id": "w1", "name": "王五", "crew_id": "c2"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["added"])
        # 同编号冒名登记必须被拒
        status, body = self.api.request(
            "POST", "/admin/workers", {"id": "w1", "name": "赵六", "crew_id": "c2"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "name_mismatch")

        # 任务
        tasks = [
            ("t1", "pA", "c1", "2026-09-10T07:00+08:00", "2026-09-10T13:00+08:00"),
            ("t2", "pB", "c2", "2026-09-10T10:00+08:00", "2026-09-10T14:00+08:00"),
            ("t3", "pA", "c1", "2026-09-11T13:00+08:00", "2026-09-11T16:00+08:00"),
            ("t4", "pA", "c1", "2026-09-11T15:00+08:00", "2026-09-11T18:00+08:00"),
            ("t5", "pA", "c1", "2026-09-12T07:00+08:00", "2026-09-12T13:00+08:00"),
            ("t6", "pA", "c1", "2026-09-13T07:00+08:00", "2026-09-13T12:00+08:00"),
            ("t7", "pA", "c1", "2026-09-14T07:00+08:00", "2026-09-14T12:00+08:00"),
            ("t8", "pA", "c1", "2026-09-15T07:00+08:00", "2026-09-15T12:00+08:00"),
            ("t9", "pA", "c1", "2026-09-16T07:00+08:00", "2026-09-16T11:00+08:00"),
        ]
        for tid, plot, crew, start, end in tasks:
            status, body = self.api.request(
                "POST", "/admin/tasks",
                {"id": tid, "plot_id": plot, "crew_id": crew, "title": f"养护{tid}",
                 "planned_start": start, "planned_end": end},
            )
            self.assertEqual(status, 200, body)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    # --------------------------------------------------------------- 授权

    def test_auth_and_scope(self) -> None:
        status, body = self.api.request("GET", "/audit", actor=None)
        self.assertEqual(status, 401)
        # 带班人不能登记任务到他人班组
        status, body = self.api.request(
            "POST", "/admin/tasks",
            {"id": "x", "plot_id": "pB", "crew_id": "c2", "title": "越权",
             "planned_start": "2026-09-10T07:00+08:00",
             "planned_end": "2026-09-10T08:00+08:00"},
            actor="f1",
        )
        self.assertEqual(status, 403)
        # 组员不能直接补传事件
        status, body = self.api.request(
            "POST", "/tasks/t1/events",
            {"events": [{"event_id": "e", "worker_id": "w2", "type": "start",
                         "seq": 1, "occurred_at": "2026-09-10T08:00+08:00"}]},
            actor="w2",
        )
        self.assertEqual(status, 403)
        # 全局跨基地冲突仅管理员可查
        status, _ = self.api.request("GET", "/conflicts", actor="f1")
        self.assertEqual(status, 403)

    # --------------------------------------------------------------- 事件规则

    def _upload(self, task: str, actor: str, events: list[dict]) -> tuple[int, dict]:
        return self.api.request(
            "POST", f"/tasks/{task}/events", {"events": events}, actor=actor
        )

    def test_reverse_arrival_and_idempotent_retry(self) -> None:
        # 先到完工（seq=2），后到开工（seq=1），模拟离线倒序补传
        status, body = self._upload("t3", "f1", [
            {"event_id": "t3-c", "worker_id": "w2", "type": "complete", "seq": 2,
             "occurred_at": "2026-09-11T15:00+08:00", "photo_digest": None},
        ])
        self.assertEqual(status, 200, body)
        status, body = self._upload("t3", "f1", [
            {"event_id": "t3-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-11T14:00+08:00",
             "location_cell": "QX:01",
             "photo_digest": "a" * 64},
        ])
        self.assertEqual(status, 200)
        # 离线重试：相同 event_id 幂等，绝不重复入账
        status, body = self._upload("t3", "f1", [
            {"event_id": "t3-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-11T14:00+08:00"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["duplicated"], ["t3-s"])

        status, timeline = self.api.request("GET", "/tasks/t3/timeline", actor="f1")
        self.assertEqual(status, 200)
        self.assertEqual([e["seq"] for e in timeline["ordered_events"]], [1, 2])
        workers = {w["worker_id"]: w for w in timeline["workers"]}
        self.assertEqual(workers["w2"]["work_minutes"], 60)

    def test_cross_midnight_and_pause(self) -> None:
        status, _ = self._upload("t9", "f1", [
            {"event_id": "t9-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-16T22:00+08:00", "location_cell": "QX:02"},
            {"event_id": "t9-p", "worker_id": "w2", "type": "pause", "seq": 2,
             "occurred_at": "2026-09-16T23:00+08:00"},
            {"event_id": "t9-r", "worker_id": "w2", "type": "resume", "seq": 3,
             "occurred_at": "2026-09-16T23:30+08:00"},
            {"event_id": "t9-c", "worker_id": "w2", "type": "complete", "seq": 4,
             "occurred_at": "2026-09-17T00:30+08:00"},
        ])
        self.assertEqual(status, 200)
        status, timeline = self.api.request("GET", "/tasks/t9/timeline", actor="f1")
        work = [s for s in timeline["segments"] if s["kind"] == "work"]
        self.assertTrue(any("crosses_midnight" in s["flags"] for s in work))
        workers = {w["worker_id"]: w for w in timeline["workers"]}
        # 22:00-23:00（60）+ 23:30-00:30（60）= 120 分钟，30 分钟间歇不计
        self.assertEqual(workers["w2"]["work_minutes"], 120)

    def test_explicit_and_implicit_handover(self) -> None:
        # 显式交接
        status, _ = self._upload("t5", "f1", [
            {"event_id": "t5-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-12T08:00+08:00"},
            {"event_id": "t5-h", "worker_id": "w2", "type": "handover", "seq": 2,
             "occurred_at": "2026-09-12T10:00+08:00", "handover_to": "w3"},
            {"event_id": "t5-s2", "worker_id": "w3", "type": "start", "seq": 3,
             "occurred_at": "2026-09-12T10:00+08:00"},
            {"event_id": "t5-c", "worker_id": "w3", "type": "complete", "seq": 4,
             "occurred_at": "2026-09-12T12:00+08:00"},
        ])
        self.assertEqual(status, 200)
        status, timeline = self.api.request("GET", "/tasks/t5/timeline", actor="f1")
        minutes = {w["worker_id"]: w["work_minutes"] for w in timeline["workers"]}
        self.assertEqual(minutes, {"w2": 120, "w3": 120})
        self.assertTrue(
            any(w["code"] == "explicit_handover" for w in timeline["warnings"])
        )
        # 临时换人但没有交接事件：自动收尾前任并显式标注
        status, _ = self._upload("t6", "f1", [
            {"event_id": "t6-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-13T08:00+08:00"},
            {"event_id": "t6-s2", "worker_id": "w3", "type": "start", "seq": 2,
             "occurred_at": "2026-09-13T10:00+08:00"},
            {"event_id": "t6-c", "worker_id": "w3", "type": "complete", "seq": 3,
             "occurred_at": "2026-09-13T11:00+08:00"},
        ])
        self.assertEqual(status, 200)
        status, timeline = self.api.request("GET", "/tasks/t6/timeline", actor="f1")
        minutes = {w["worker_id"]: w["work_minutes"] for w in timeline["workers"]}
        self.assertEqual(minutes, {"w2": 120, "w3": 60})
        self.assertTrue(
            any(w["code"] == "implicit_handover" for w in timeline["warnings"])
        )

    def test_photo_reuse_and_location_rules(self) -> None:
        digest = "b" * 64
        # 原始经纬度一律拒绝，只接受网格编码
        status, body = self._upload("t4", "f1", [
            {"event_id": "bad", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-11T16:00+08:00", "lat": 28.1, "lng": 118.4},
        ])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "raw_location_forbidden")

        # 首次使用照片摘要（t4 与 t3 使用不同摘要，先登记 t4 首次使用）
        status, _ = self._upload("t4", "f1", [
            {"event_id": "t4-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-11T16:00+08:00",
             "location_cell": "QX:01", "photo_digest": digest},
            {"event_id": "t4-c", "worker_id": "w2", "type": "complete", "seq": 2,
             "occurred_at": "2026-09-11T17:00+08:00"},
        ])
        self.assertEqual(status, 200)
        # 另一任务复用同一照片
        status, _ = self._upload("t3", "f1", [
            {"event_id": "t3-reuse", "worker_id": "w2", "type": "start", "seq": 3,
             "occurred_at": "2026-09-11T15:30+08:00", "photo_digest": digest},
        ])
        self.assertEqual(status, 200)
        status, conflicts = self.api.request(
            "GET", "/tasks/t3/conflicts", actor="f1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(conflicts["photo_reuses"]), 1)
        self.assertEqual(conflicts["photo_reuses"][0]["first_event"], "t4-s")

        # 位置超出地块允许网格只给告警，不影响事件入账
        status, _ = self._upload("t7", "f1", [
            {"event_id": "t7-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-14T08:00+08:00",
             "location_cell": "QX:99"},
        ])
        self.assertEqual(status, 200)
        status, timeline = self.api.request("GET", "/tasks/t7/timeline", actor="f1")
        self.assertTrue(
            any(w["code"] == "location_out_of_range" for w in timeline["warnings"])
        )

    def test_late_submission_flag(self) -> None:
        # 事件发生远早于送达（补传），应被标注但仍入账
        status, _ = self._upload("t8", "f1", [
            {"event_id": "t8-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-10T08:00+08:00"},
        ])
        self.assertEqual(status, 200)
        status, timeline = self.api.request("GET", "/tasks/t8/timeline", actor="f1")
        self.assertTrue(
            any(w["code"] == "late_submission" for w in timeline["warnings"])
        )

    # --------------------------------------------------------------- 跨基地冲突

    def test_cross_base_overlap_and_settlement_hold(self) -> None:
        # t1（青溪）08:00-12:00 与 t2（云岭）11:00-13:00，同一人 w1 重叠 60 分钟
        status, _ = self._upload("t1", "f1", [
            {"event_id": "t1-s", "worker_id": "w1", "type": "start", "seq": 1,
             "occurred_at": "2026-09-10T08:00+08:00", "photo_digest": "c" * 64},
            {"event_id": "t1-c", "worker_id": "w1", "type": "complete", "seq": 2,
             "occurred_at": "2026-09-10T12:00+08:00"},
        ])
        self.assertEqual(status, 200)
        status, _ = self._upload("t2", "f2", [
            {"event_id": "t2-s", "worker_id": "w1", "type": "start", "seq": 1,
             "occurred_at": "2026-09-10T11:00+08:00", "photo_digest": "d" * 64},
            {"event_id": "t2-c", "worker_id": "w1", "type": "complete", "seq": 2,
             "occurred_at": "2026-09-10T13:00+08:00"},
        ])
        self.assertEqual(status, 200)

        status, conflicts = self.api.request("GET", "/conflicts", actor="admin")
        self.assertEqual(status, 200)
        self.assertEqual(len(conflicts["overlaps"]), 1)
        overlap = conflicts["overlaps"][0]
        self.assertTrue(overlap["cross_base"])
        self.assertEqual(overlap["minutes"], 60)
        self.assertEqual({overlap["base_a"], overlap["base_b"]},
                         {"青溪基地", "云岭基地"})

        # 两班各自锁定
        status, _ = self.api.request("POST", "/tasks/t1/lock", actor="f1")
        self.assertEqual(status, 200)
        status, _ = self.api.request("POST", "/tasks/t2/lock", actor="f2")
        self.assertEqual(status, 200)

        # 结算暂扣重叠分钟
        status, settlement = self.api.request(
            "GET", "/tasks/t1/settlement", actor="f1"
        )
        row = settlement["workers"][0]
        self.assertEqual(row["work_minutes"], 240)
        self.assertEqual(row["overlap_held_minutes"], 60)
        self.assertEqual(row["payable_minutes"], 180)

    # --------------------------------------------------------------- 申诉

    def test_appeal_must_target_real_conflict(self) -> None:
        self.test_cross_base_overlap_and_settlement_hold()
        # 不存在的冲突不能发起申诉，防止管理员与村民合谋凭空补数
        status, body = self.api.request(
            "POST", "/appeals",
            {"task_id": "t1", "worker_id": "w1", "category": "overlap_dispute",
             "ref": "t9", "claimed_minutes": 60, "reason": "报错基地了"},
            actor="f1",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "no_such_conflict")
        # 申诉分钟不得超过实际重叠
        status, body = self.api.request(
            "POST", "/appeals",
            {"task_id": "t1", "worker_id": "w1", "category": "overlap_dispute",
             "claimed_minutes": 61, "reason": "当天确实在青溪作业"},
            actor="f1",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "claim_exceeds_overlap")

    def test_appeal_decision_blocks_and_changes_settlement(self) -> None:
        self.test_cross_base_overlap_and_settlement_hold()
        status, appeal = self.api.request(
            "POST", "/appeals",
            {"task_id": "t1", "worker_id": "w1", "category": "overlap_dispute",
             "claimed_minutes": 60,
             "reason": "11 点后已回到青溪，云岭是同村他人代签",
             "evidence_digests": ["e" * 64]},
            actor="f1",
        )
        self.assertEqual(status, 200, appeal)
        appeal_id = appeal["id"]

        # 有待决申诉时禁止批准
        status, body = self.api.request(
            "POST", "/tasks/t1/approve", {}, actor="admin"
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "pending_appeal")

        # 带班人无权裁决
        status, _ = self.api.request(
            "POST", f"/appeals/{appeal_id}/decision",
            {"decision": "upheld", "reason": "带班自查"},
            actor="f1",
        )
        self.assertEqual(status, 403)

        # 管理员支持部分工时：批准 45 分钟，必须给理由
        status, body = self.api.request(
            "POST", f"/appeals/{appeal_id}/decision",
            {"decision": "upheld", "award_minutes": 45,
             "reason": "核对青溪收工照片与云岭带班记录，认可 11:00-11:45"},
            actor="admin",
        )
        self.assertEqual(status, 200, body)
        status, settlement = self.api.request(
            "GET", "/tasks/t1/settlement", actor="admin"
        )
        row = settlement["workers"][0]
        # 240 - 60 暂扣 + 45 申诉补发 = 225
        self.assertEqual(row["payable_minutes"], 225)
        self.assertEqual(row["appeal_award_minutes"], 45)

        # 重复裁决被拒
        status, body = self.api.request(
            "POST", f"/appeals/{appeal_id}/decision",
            {"decision": "rejected", "reason": "改主意"},
            actor="admin",
        )
        self.assertEqual(status, 409)

    def test_member_files_and_sees_only_own_appeal(self) -> None:
        self.test_cross_base_overlap_and_settlement_hold()
        # w1 同时是组员操作人，可为本人申诉
        status, _ = self.api.request(
            "POST", "/appeals",
            {"task_id": "t1", "category": "overlap_dispute",
             "claimed_minutes": 60, "reason": "我当天没去云岭"},
            actor="w1",
        )
        self.assertEqual(status, 200)
        # 组员只能替本人申诉
        status, body = self.api.request(
            "POST", "/appeals",
            {"task_id": "t1", "worker_id": "w2", "category": "overlap_dispute",
             "claimed_minutes": 60, "reason": "代他人"},
            actor="w1",
        )
        self.assertEqual(status, 403)
        status, body = self.api.request("GET", "/appeals", actor="w2")
        self.assertEqual(status, 200)
        self.assertEqual(body["appeals"], [])

    # --------------------------------------------------------------- 未闭合与收尾

    def _open_t7(self) -> None:
        status, _ = self._upload("t7", "f1", [
            {"event_id": "t7-start", "worker_id": "w2", "type": "start", "seq": 2,
             "occurred_at": "2026-09-14T08:00+08:00"},
        ])
        self.assertEqual(status, 200)

    def test_foreman_closure_then_lock(self) -> None:
        self._open_t7()
        status, body = self.api.request("POST", "/tasks/t7/lock", actor="f1")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unclosed_session")
        # 带班人按计划窗口登记收尾（8:00-11:30 = 210 分钟）
        status, _ = self.api.request(
            "POST", "/tasks/t7/closures",
            {"closures": {"w2": "2026-09-14T11:30+08:00"}},
            actor="f1",
        )
        self.assertEqual(status, 200)
        status, body = self.api.request("POST", "/tasks/t7/lock", actor="f1")
        self.assertEqual(status, 200, body)
        status, settlement = self.api.request(
            "GET", "/tasks/t7/settlement", actor="f1"
        )
        self.assertEqual(settlement["workers"][0]["work_minutes"], 210)

    def test_closure_beyond_grace_requires_appeal(self) -> None:
        # t8 开工后未完工；收尾时间超出计划完工 3 小时宽限（09-15 15:00）
        status, _ = self._upload("t8", "f1", [
            {"event_id": "t8-start", "worker_id": "w2", "type": "start", "seq": 2,
             "occurred_at": "2026-09-15T08:00+08:00"},
        ])
        self.assertEqual(status, 200)
        status, _ = self.api.request(
            "POST", "/tasks/t8/closures",
            {"closures": {"w2": "2026-09-15T16:00+08:00"}},
            actor="f1",
        )
        self.assertEqual(status, 200)
        status, timeline = self.api.request("GET", "/tasks/t8/timeline", actor="f1")
        self.assertTrue(
            any(w["code"] == "closure_beyond_grace" for w in timeline["warnings"])
        )
        self.assertEqual(timeline["unclosed_worker"], "w2")
        # 仍不能锁定
        status, body = self.api.request("POST", "/tasks/t8/lock", actor="f1")
        self.assertEqual(status, 400)

        # 发起未闭合会话申诉，管理员支持 180 分钟
        status, appeal = self.api.request(
            "POST", "/appeals",
            {"task_id": "t8", "worker_id": "w2", "category": "unclosed_session",
             "claimed_minutes": 240, "reason": "断夜护林后手机没电无法完工打卡"},
            actor="f1",
        )
        self.assertEqual(status, 200, appeal)
        status, _ = self.api.request(
            "POST", f"/appeals/{appeal['id']}/decision",
            {"decision": "upheld", "award_minutes": 180,
             "reason": "同组两人佐证巡护至 11 点，按 3 小时核准"},
            actor="admin",
        )
        self.assertEqual(status, 200)
        status, body = self.api.request("POST", "/tasks/t8/lock", actor="f1")
        self.assertEqual(status, 200, body)
        status, settlement = self.api.request(
            "GET", "/tasks/t8/settlement", actor="f1"
        )
        row = settlement["workers"][0]
        self.assertEqual(row["work_minutes"], 0)
        self.assertEqual(row["appeal_award_minutes"], 180)
        self.assertEqual(row["payable_minutes"], 180)

    # --------------------------------------------------------------- 锁定与反补数

    def test_locked_task_rejects_backfill(self) -> None:
        status, _ = self._upload("t5", "f1", [
            {"event_id": "t5-s", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-12T08:00+08:00"},
            {"event_id": "t5-c", "worker_id": "w2", "type": "complete", "seq": 2,
             "occurred_at": "2026-09-12T12:00+08:00"},
        ])
        self.assertEqual(status, 200)
        status, _ = self.api.request("POST", "/tasks/t5/lock", actor="f1")
        self.assertEqual(status, 200)
        # 锁定后任何补传一律拒绝，管理员也不行
        status, body = self._upload("t5", "admin", [
            {"event_id": "late-add", "worker_id": "w2", "type": "start", "seq": 3,
             "occurred_at": "2026-09-12T12:30+08:00"},
        ])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "task_locked")

    # --------------------------------------------------------------- 批准与位置最小化

    def test_approve_purges_location_and_photo_waiver(self) -> None:
        digest = "f" * 64
        # t3：w2 一小时，照片首次使用；t4 复用同一照片，60 分钟全部暂扣
        status, _ = self._upload("t3", "f1", [
            {"event_id": "t3-start", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-11T14:00+08:00",
             "location_cell": "QX:01", "photo_digest": digest},
            {"event_id": "t3-done", "worker_id": "w2", "type": "complete", "seq": 2,
             "occurred_at": "2026-09-11T15:00+08:00"},
        ])
        self.assertEqual(status, 200)
        status, _ = self._upload("t4", "f1", [
            {"event_id": "t4-start", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-11T16:00+08:00",
             "location_cell": "QX:02", "photo_digest": digest},
            {"event_id": "t4-done", "worker_id": "w2", "type": "complete", "seq": 2,
             "occurred_at": "2026-09-11T17:00+08:00"},
        ])
        self.assertEqual(status, 200)
        status, _ = self.api.request("POST", "/tasks/t3/lock", actor="f1")
        self.assertEqual(status, 200)
        status, _ = self.api.request("POST", "/tasks/t4/lock", actor="f1")
        self.assertEqual(status, 200)
        status, settlement = self.api.request(
            "GET", "/tasks/t4/settlement", actor="f1"
        )
        self.assertEqual(settlement["workers"][0]["photo_held_minutes"], 60)
        self.assertEqual(settlement["workers"][0]["payable_minutes"], 0)

        # 管理员核实后豁免照片暂扣
        status, body = self.api.request(
            "POST", "/tasks/t4/approve", {"photo_waiver": True}, actor="admin"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["settlement"]["workers"][0]["payable_minutes"], 60)
        self.assertGreaterEqual(body["location_cells_purged"], 1)

        # 批准后事件与快照中不再保留任何网格位置，只剩"是否在范围内"的结论
        status, timeline = self.api.request("GET", "/tasks/t4/timeline", actor="f1")
        self.assertTrue(all(e["location_cell"] is None for e in timeline["ordered_events"]))
        status, audit = self.api.request("GET", "/audit", actor="admin")
        audit_text = json.dumps(audit, ensure_ascii=False)
        self.assertNotIn("QX:01", audit_text)
        self.assertNotIn("QX:02", audit_text)
        status, access = self.api.request("GET", "/audit/access", actor="admin")
        self.assertNotIn("QX:0", json.dumps(access, ensure_ascii=False))

        # 已批准不能再次批准
        status, body = self.api.request(
            "POST", "/tasks/t4/approve", {}, actor="admin"
        )
        self.assertEqual(status, 409)

    # --------------------------------------------------------------- 隐私

    def test_name_masking_and_access_audit(self) -> None:
        # 本班组带班人可见
        status, body = self.api.request("GET", "/workers/w2", actor="f1")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "周三")
        self.assertFalse(body["masked"])
        # 其他基地带班人只见脱敏名
        status, body = self.api.request("GET", "/workers/w2", actor="f2")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "周*")
        self.assertTrue(body["masked"])
        # 本人可见全名，他人只见脱敏
        status, body = self.api.request("GET", "/workers/w2", actor="w2")
        self.assertEqual(body["name"], "周三")
        status, body = self.api.request("GET", "/workers/w3", actor="w2")
        self.assertEqual(body["name"], "吴*")

        # 隐私访问审计仅管理员可查，且记录每次访问（含被拒访问的目的）
        status, body = self.api.request("GET", "/audit/access", actor="f1")
        self.assertEqual(status, 403)
        status, body = self.api.request("GET", "/audit/access", actor="admin")
        self.assertEqual(status, 200)
        resources = {(r["resource"], r["purpose"]) for r in body["records"]}
        self.assertIn(("worker:w2", "带班人核对本班组工时"), resources)
        self.assertTrue(any("越权" in p for _, p in resources))

    def test_audit_chain_detects_tampering(self) -> None:
        status, body = self.api.request("GET", "/audit/verify", actor="admin")
        self.assertEqual(status, 200)
        self.assertTrue(body["intact"])
        # 直接篡改内存中的审计明细（模拟有人事后改账）
        self.server.api.store.audit[3].detail = {"tampered": True}
        status, body = self.api.request("GET", "/audit/verify", actor="admin")
        self.assertFalse(body["intact"])

    # --------------------------------------------------------------- 并发补传

    def test_concurrent_uploads_from_two_bases(self) -> None:
        def upload_f1() -> tuple[int, dict]:
            return self._upload("t1", "f1", [
                {"event_id": "par-t1-s", "worker_id": "w1", "type": "start",
                 "seq": 1, "occurred_at": "2026-09-10T08:00+08:00"},
                {"event_id": "par-t1-c", "worker_id": "w1", "type": "complete",
                 "seq": 2, "occurred_at": "2026-09-10T12:00+08:00"},
            ])

        def upload_f2() -> tuple[int, dict]:
            return self._upload("t2", "f2", [
                {"event_id": "par-t2-s", "worker_id": "w1", "type": "start",
                 "seq": 1, "occurred_at": "2026-09-10T11:00+08:00"},
                {"event_id": "par-t2-c", "worker_id": "w1", "type": "complete",
                 "seq": 2, "occurred_at": "2026-09-10T13:00+08:00"},
            ])

        with ThreadPoolExecutor(max_workers=2) as pool:
            r1, r2 = list(pool.map(lambda f: f(), [upload_f1, upload_f2]))
        self.assertEqual(r1[0], 200, r1[1])
        self.assertEqual(r2[0], 200, r2[1])

        # 无事件丢失、回执编号全局唯一
        accepted = r1[1]["accepted"] + r2[1]["accepted"]
        self.assertEqual(len(accepted), 4)
        receipts = [item["receipt"] for item in accepted]
        self.assertEqual(len(receipts), len(set(receipts)))

        # 并发结果与串行一致：60 分钟跨基地重叠
        status, conflicts = self.api.request("GET", "/conflicts", actor="admin")
        self.assertEqual(status, 200)
        self.assertEqual(conflicts["overlaps"][0]["minutes"], 60)

    # --------------------------------------------------------------- 其他输入校验

    def test_timezone_required(self) -> None:
        status, body = self._upload("t3", "f1", [
            {"event_id": "no-tz", "worker_id": "w2", "type": "start", "seq": 9,
             "occurred_at": "2026-09-11T14:00"},
        ])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_time")

    def test_invalid_batch_is_atomic(self) -> None:
        # 第一条合法、第二条非法：整批拒绝，合法事件也不得入账
        status, body = self._upload("t3", "f1", [
            {"event_id": "good", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-11T14:00+08:00"},
            {"event_id": "bad", "worker_id": "w2", "type": "pause", "seq": 2,
             "occurred_at": "not-a-time"},
        ])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_time")
        status, timeline = self.api.request("GET", "/tasks/t3/timeline", actor="f1")
        self.assertEqual(timeline["ordered_events"], [])
        # 批次内 event_id 重复同样整批拒绝
        status, body = self._upload("t3", "f1", [
            {"event_id": "dup", "worker_id": "w2", "type": "start", "seq": 1,
             "occurred_at": "2026-09-11T14:00+08:00"},
            {"event_id": "dup", "worker_id": "w2", "type": "complete", "seq": 2,
             "occurred_at": "2026-09-11T15:00+08:00"},
        ])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "duplicate_event_in_batch")


if __name__ == "__main__":
    unittest.main()
