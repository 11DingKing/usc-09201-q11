# 林业灵活用工核验

面向集体林权改革与林下产业协作的**离线优先**用工核验服务：围绕任务、地块、
班组与证据摘要登记开工、暂停、复工、交接与完工，检测跨基地工时重叠与照片重复，
经带班人复核、申诉裁决后生成可解释的待结算工时；个人位置只保留必要范围。

仅依赖 Python 3.11 标准库，无外部依赖。

## 运行与测试

```bash
python3 -m unittest discover -s tests   # 19 项端到端测试
python3 -m service.main                 # 默认 :3000，可用 PORT 覆盖
curl -s localhost:3000/health
```

## 接口

所有业务请求需带 `X-Actor-Id`（服务内置 `admin` 联盟管理员）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/actors` `/plots` `/crews` `/workers` `/tasks` | 基础登记（管理员；任务带班人可建本组） |
| POST | `/tasks/{id}/events` | 批量离线补传事件（幂等、按 seq 归一化） |
| POST | `/tasks/{id}/closures` | 带班人登记未闭合会话收尾 |
| POST | `/tasks/{id}/lock` | 带班人提交复核（锁定后拒绝一切补传） |
| GET | `/tasks/{id}/timeline` | 可解释时间线：片段、按人工时、告警 |
| GET | `/tasks/{id}/conflicts` | 本任务的跨基地重叠、照片复用等冲突 |
| GET | `/tasks/{id}/settlement` | 待结算工时及全部暂扣/补发明细 |
| POST | `/appeals` | 发起工时申诉（须针对真实冲突） |
| GET | `/appeals` | 申诉列表（按角色限定范围） |
| POST | `/appeals/{id}/decision` | 管理员裁决（理由必填） |
| POST | `/tasks/{id}/approve` | 管理员批准工时并清除位置网格 |
| GET | `/workers/{id}` | 村民信息（按职责脱敏，访问留痕） |
| GET | `/conflicts` | 全局冲突（仅管理员） |
| GET | `/audit` `/audit/access` `/audit/verify` | 哈希链审计、隐私访问记录、防篡改校验 |

## 关键约束

* 事件时间必须带时区；原始经纬度字段一律拒绝，只接受地块登记的粗网格。
* 照片只保存 SHA-256 摘要，复用即暂扣对应工时，经申诉核实可豁免。
* 跨基地重叠分钟暂扣，待申诉裁决；批准工时后位置网格立即删除。
* 审计为哈希链追加式结构，任何事后篡改均可被 `/audit/verify` 检出。

领域规则与状态机见 [`docs/domain.md`](docs/domain.md)。
