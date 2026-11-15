# 维史校准台（dimhist）

维史校准台是一个**完全离线、确定性、可审计**的 SCD2 缓慢变化维表历史校准工作台。
它把本地变更事件日志回放成期望历史，与仓内实际维表历史做结构诊断和半开区间
as-of 对账，并输出可解释的 dry-run 修复计划、SQLite 审计台账和多格式报告。

本项目的所有主体、人员、ID、时间、来源均为合成 fixture，不含真实公司名、域名、
个人信息或密钥。运行期不会发起外部网络请求，也不会连接任何外部数据库。

## 它做什么

- 按业务键回放 JSONL 变更事件，生成 `[valid_from, valid_to)` 的期望 SCD2 版本链。
- 稳定处理迟到事件、完全重复事件、同 event_id 内容冲突、同生效时刻多事件、
  幂等 no-op 事件和属性回退。
- 诊断实际维表的区间重叠、空洞、重复版本、多当前行、无当前行、当前标记错误、
  属性漂移、孤立实际行、缺失期望行和当前快照漂移。
- 可选的时态父子层级校验：契约声明父业务键属性后，按半开有效期检查真正同时
  成立的循环（区分必然/可能）、悬空父节点、多版本父指针歧义，并对超预算区间
  给出未判定发现而非静默通过。
- 在期望与实际的全部边界时刻及指定 `--now` 上执行 as-of 对账。
- 生成逐条可解释、必须人工复核的修复动作；只写 JSON / Markdown，不执行修复 SQL。
- 把契约、输入哈希、事件账本、期望/实际版本、发现、as-of 检查与计划写入 SQLite。
- 输出确定性的 JSON、Markdown、自包含 HTML 报告，并提供仅限本机回环地址的只读 HTTP 服务。

## 不做什么

- 不是湖仓快照时间旅行、表维护、冷热分层或血缘分析工具。
- 不连接生产数据库，不下发 DDL/DML，不是运维执行平台。
- 不是 CDC 采集器、调度器、分布式计算框架。
- 不是通用 BI 看板、CRUD 后台或报表系统。

## 快速开始

要求 Python 3.10+，仅使用标准库。

```bash
# 1. 一条命令跑通完整演示
PYTHONPATH=src python -m dimhist demo --workspace .dimhist-work --reset

# 2. 一条命令跑测试
python -m unittest discover -s tests -v

# 3. 启动只读本地服务（先运行 demo）
PYTHONPATH=src python -m dimhist serve --workspace .dimhist-work --host 127.0.0.1 --port 5017
```

演示的固定基准输出为：

```text
DEMO OK workspace=.dimhist-work events=23 expected_versions=19 actual_versions=20 findings=12 asof_checks=33 asof_mismatches=14 plan_actions=12 report=reports/demo_report.html
```

`demo` 会把内置 fixture 复制到工作区，并验证生成的汇总与
`fixtures/expected/demo_summary.json` 完全一致。也可用 `--now` 指定其他
UTC 秒精度对账时刻；非默认时刻不比对静态基准摘要。

其他命令：

```bash
# 只创建 workspace/contracts, data/events, data/dimension, ledger, reports, plans
PYTHONPATH=src python -m dimhist init --workspace .dimhist-work

# 对已有工作区输入重新校准
PYTHONPATH=src python -m dimhist run --workspace .dimhist-work --now 2026-01-15T00:00:00Z
```

`--reset` 只删除带 `.dimhist-workspace` 标记的工作区。对没有标记的非空目录会
以 `WS_002` 拒绝，避免误删用户文件。

## 演示产物

```text
.dimhist-work/
  .dimhist-workspace
  contracts/dimension_member.json
  data/events/change_events.jsonl
  data/dimension/member_history.csv
  data/dimension/member_current.csv
  ledger/dimhist.sqlite3
  reports/demo_report.json
  reports/demo_report.md
  reports/demo_report.html
  plans/repair_plan.json
  plans/repair_plan.md
```

关键台账表：`meta`、`artifacts`、`events`、`expected_versions`、
`actual_versions`、`current_snapshot`、`findings`、`asof_checks`、`plan_actions`。

## Fixture 场景

内置会员维 `dim_member` 使用业务键 `member_id`，跟踪 `segment`、`region`、
`status`。fixture 覆盖：

| 业务键 | 场景 |
| --- | --- |
| `M-1001` | 正常变更、物理迟到事件、属性回退、开放当前版本 |
| `M-1002` | 完全重复事件、同 ID 内容冲突、同生效时刻多事件、no-op |
| `M-1003` | 实际区间重叠、历史空洞 |
| `M-1004` | 重复版本、区间重叠、多个当前行 |
| `M-1005` | 全部区间关闭、无当前行 |
| `M-1006` | 期望存在、实际行缺失 |
| `M-1007` | 实际孤立行、无对应变更事件 |
| `M-1008` | 同起点版本属性漂移 |

## 只读 API

服务只允许监听 `127.0.0.1`、`localhost` 或 `::1`，默认端口 `5017`。任何其他
host 在绑定前即被拒绝。所有接口只读；写方法返回 `405`。

| 路径 | 内容 |
| --- | --- |
| `/`、`/report` | 自包含 HTML 报告 |
| `/reports/demo_report.{html,json,md}` | 三个已生成报告文件 |
| `/api/health` | 服务与报告版本状态 |
| `/api/report` | 完整报告 JSON |
| `/api/summary` | 运行汇总 |
| `/api/findings?code=&severity=&natural_key=` | 过滤后的发现 |
| `/api/asof?status=&at=&natural_key=` | as-of 检查 |
| `/api/plan` | dry-run 修复计划 |
| `/api/events?status=&natural_key=` | 事件台账视图 |
| `/api/members/{member_id}` | 单个业务键的期望、实际、发现和 as-of 记录 |

## 关键语义

- 时间统一规范化为 UTC ISO8601 秒精度，例如 `2024-06-01T00:00:00Z`。
- 所有区间均为半开区间：`valid_from <= t < valid_to`；空 `valid_to` 表示开放。
- 同 event_id 且规范化内容完全相同的重复行去重计数，不进入回放。
- 同 event_id 但内容不同的行保留首行并产生 `EVT_DUP_CONFLICT`，后续冲突行保留在
  事件台账但退出回放。
- 同业务键同生效时刻的事件按 `sequence`、`event_id`、原始行号稳定排序后归并；
  若同一属性出现不同值，产生 `EVT_SAME_TIME_CONFLICT`，最终状态取排序末位。
- 相对上一状态没有属性变化的事件标记为 `noop`，不新建版本，但仍留在事件台账。
- as-of 在每个业务键的期望/实际边界以及 `--now` 抽样核验。

## 文档

- [设计说明](docs/DESIGN.md)
- [发现码字典](docs/FINDING_CODES.md)
- [契约与输入格式](docs/CONTRACT.md)
- [离线与合规说明](docs/COMPLIANCE.md)

## 测试与约束

```bash
python -m unittest discover -s tests -v
```

测试覆盖契约错误码、JSONL 原始行保真、去重与同刻归并、SCD2 回放、结构诊断、
as-of 边界、计划确定性、台账与报告哈希、HTML 转义、只读 HTTP 和 CLI 端到端流程。
代码仅依赖 Python 标准库，运行期不访问网络，不执行修复 SQL，不修改输入文件。

## 安全提示

修复计划是证据和建议，不是可执行脚本。任何实际修复都应在项目之外经过数据所有者
复核、变更审批、备份和回滚演练后实施。
