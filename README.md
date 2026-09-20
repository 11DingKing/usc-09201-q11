# 林业灵活用工核验

本项目提供集体林权改革与林下产业协作领域的核验服务：围绕任务、地块、班组和
证据摘要登记开工、暂停、复工、完工与复核，按规则生成待结算工时。业务数据与
敏感配置应存放在受控环境中。

## 运行

- `python3 -m unittest`：运行全部测试（含发薪前并发补传端到端场景）。
- `python3 -m service.main`：启动服务（默认端口 3000，`PORT` 可改），
  访问 `/health` 确认状态。
- 设置 `JOURNAL_FILE=/path/to/journal.jsonl` 可开启日志持久化，重启自动重放。

## 接口一览

除 `/health` 外，所有接口需要请求头 `X-Actor-Id` 与 `X-Actor-Role`
（leader / base_admin / reviewer / finance / auditor / worker）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tasks` | 登记任务与初始名册（base_admin） |
| POST | `/tasks/{id}/reassign` | 临时换人，生成新名册版本（base_admin） |
| GET | `/tasks/{id}` `/timelines` `/events` | 任务详情 / 推导时间线 / 事件明细 |
| POST | `/events` | 登记单条事件（幂等，支持 `supersedes` 更正） |
| POST | `/events/batch` | 离线批量补传，逐条返回结果 |
| GET | `/conflicts` | 冲突提示（重叠、证据复用、重复签名等） |
| POST | `/disputes` | 发起工时申诉（村民本人/带班人/管理员） |
| POST | `/disputes/{id}/decision` | 复核裁定：approve / adjust / reject（reviewer） |
| POST | `/settlements/run` | 按规则生成待结算工时（finance/reviewer） |
| GET | `/settlements/{run}/lines/{line}/explain` | 结算行解释链 |
| GET | `/workers/{id}/hours?from=&to=` | 村民工时（含暂扣/争议/封顶明细） |
| GET | `/audit-log` | 隐私审计日志（auditor） |

## 事件示例

```json
{
  "event_id": "a-e1",
  "task_id": "T-A1",
  "worker_id": "W1",
  "kind": "start",
  "occurred_at": "2026-09-19T08:00:00+08:00",
  "source": "leader-phone-a",
  "evidence": {
    "digest": "photo-aaa",
    "captured_at": "2026-09-19T08:00:00+08:00",
    "location": {"lat": 29.4760, "lon": 109.3180}
  }
}
```

精确坐标只用于当场换算，落库的是约 1 公里网格（如 `29.48,109.32`）。
领域约定与结算规则详见 `docs/domain.md`。
