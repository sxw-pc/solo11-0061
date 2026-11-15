# 设计说明

## 1. 目标与边界

`dimhist` 的核心问题是：同一业务键在本地变更事件日志中的期望历史，与仓内实际
SCD2 维表历史之间是否一致；若不一致，差异发生在什么区间、由什么证据支撑、应如何
生成可解释和可回滚的人工修复计划。

设计边界：

- 纯本地、完全离线、标准库实现；
- 确定性优先于性能，fixture 规模优先于分布式吞吐；
- 只读原始输入，修复计划永远不执行；
- 所有结论都保留来源行号、哈希、区间和稳定 ID；
- 发现、检查、动作和哈希一律稳定排序。

## 2. 模块结构

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 子命令、fixture 安装、用户错误出口 |
| `config.py` | JSON 契约加载、静态校验、契约哈希 |
| `io_utils.py` | JSONL/CSV 读取，保留行号、原始文本和哈希 |
| `normalize.py` | 事件字段解析、event_id 去重和内容冲突识别 |
| `transactions.py` | 提交清单解析、事务完整性校验、隔离编排 |
| `ordering.py` | 事务间依赖图、循环检测、可行次序与结果唯一性判定 |
| `replay.py` | 同刻归并、事务原子归并、no-op 判定、期望 SCD2 版本链生成 |
| `model.py` | 事件、版本、发现、检查、动作的 dataclass |
| `actual.py` | 实际历史与当前快照加载，非法区间初步发现 |
| `diagnose.py` | 业务键级诊断编排、属性漂移、当前快照一致性 |
| `structural.py` | 重叠、空洞、重复版本、当前状态等结构检查 |
| `asof.py` | 半开区间 as-of 抽样对账 |
| `planner.py` | 发现到 dry-run 动作的稳定映射 |
| `ledger.py` | SQLite 审计台账 schema 与写入 |
| `pipeline.py` | init、reset、端到端运行编排 |
| `report.py` | 汇总、报告文档和文件写出 |
| `render.py` | 纯 Markdown/HTML 渲染，HTML 转义 |
| `server.py` | 回环地址只读 HTTP API 与静态报告 |
| `util.py` | UTC 时间、哈希、规范 JSON、错误类型、host 校验 |

每个模块保持单一职责，任一 `.py` 文件不超过 300 行。

## 3. 端到端流程

```text
contract JSON
    │  static validation + sha256
    ▼
JSONL events ── parse ── dedupe ── stable replay ──► expected versions
                                                       │
actual history CSV ── parse ── structural checks ─────┤
current CSV ── parse ── snapshot consistency ─────────┤
                                                       ▼
                                              findings (stable IDs)
                                                       │
                             ┌─────────────────────────┼──────────────┐
                             ▼                         ▼              ▼
                      as-of checks              dry-run plan     SQLite ledger
                             │                         │              │
                             └──────────────► JSON / Markdown / HTML reports
```

`run_pipeline` 的输出是 `RunResult`，报告层和 HTTP 层只读取这一结果及其持久化产物。

## 4. 事件规范化与去重

每个非空 JSONL 行保留：

- 原始 1-based 行号；
- 原始 JSON 文本；
- 该行 UTF-8 字节的 sha256；
- 解析后的业务键、生效时间、序列和 payload。

event_id 是唯一性判定的主键：

1. 首次出现的事件参与回放。
2. 后续相同 event_id：
   - 若业务键、生效时间（规范化后）、sequence 和 payload 的规范 JSON 完全一致，
     标记 `duplicate_exact`，去重计数；
   - 否则标记 `duplicate_conflict`，产生 `EVT_DUP_CONFLICT`，保留在台账但不回放。
3. 首行始终是事实上的保留行；details 中记录保留行号、冲突行号和差异字段。

这保证重复内容不会重复生成版本，同时内容冲突不会被静默吞掉。

## 5. 同刻归并与 no-op

回放排序键：

```text
(natural_key, effective_at, sequence, event_id, raw_line)
```

同一业务键、同一 `effective_at` 的行构成一个归并组：

- 组内按上述稳定顺序依次把 payload 中出现的属性合并到当前状态；
- 只有 payload 中出现的跟踪属性会被更新，因此支持部分字段事件；
- 若同一属性在组内出现多个不同规范值，产生 `EVT_SAME_TIME_CONFLICT`；
- 归并后的最终状态与组前状态完全相同则整组标记 `noop`，不生成版本；
- 否则生成一个新版本，最后一个事件标记 `applied`，之前的事件标记
  `superseded_same_time`；版本记录组内全部 event_id 作为来源。

迟到的物理行不改变按生效时间排序的事实，因此会正确插入已有事件之间。属性回退
（例如 Gold 回到 Silver）是合法业务状态变化，不会被特殊删除。

## 5.1 跨成员事务的原子回放

事件可携带 `transaction_id` 与事务内序号 `transaction_seq`；提交清单
（`transaction_commits.jsonl`）声明每个事务的成员 event_id 集合、事务生效时刻
和 `depends_on` 事务间依赖。事务回放分三步：

1. **完整性校验（整组隔离）**。清单声明的成员集合必须与事件日志中携带该
   `transaction_id` 的事件完全一致，成员必须都带不重复的事务内序号，且任何成员
   的 event_id 不得存在内容冲突。缺成员、未声明成员、序号缺失/重复、成员内容
   冲突都会把整个事务隔离（`EVT_TX_QUARANTINED`），成员事件标记 `quarantined`
   并全部退出回放——绝不先应用有效子集。携带事务 ID 但没有清单声明的事件同样
   被隔离（`missing_commit`）。
2. **每个业务时刻的全局可行次序**。对通过校验的事务，按生效时刻分组，用
   `depends_on` 构成组内约束图。依赖指向未知事务、被隔离事务或更晚业务时刻
   时，本事务以 `unmet_dependency` 隔离并级联；互相依赖的最小事务集（循环）
   以 `dependency_cycle` 隔离，`details.cycle` 给出该最小集合。随后在不使用
   字典序掩盖歧义的前提下判定结果唯一性：对每个被多个事务写入的
   （成员，属性），取约束偏序下"可能成为最后写入者"的极大事务集合，若它们的
   候选值不一致，则不同可行次序产生不同结果，属于真正冲突，相关事务以
   `order_conflict` 隔离。通过判定的事务标记为 `unique`（只有一个可行次序）
   或 `equivalent`（多个可行次序但结果已证明相同，即可交换）；只有此时才用
   transaction_id 字典序在等价次序中选取确定性的折叠顺序。
3. **原子归并**。已提交事务在其生效时刻作为一个整体进入对应
   （业务键，时刻）归并组：组内先按旧语义归并普通事件，再按可行次序依次套用
   各事务的折叠效果（成员事件按事务内序号折叠，部分属性更新继承前态）。每个
   （业务键，时刻）仍只生成一个版本，版本的 `source_event_ids` 记录全部成员
   事件，`source_transaction_ids` 记录参与事务，隔离原因进入台账
   `transactions` 表、findings 与报告。事务被隔离时其所有成员的前态不受
   影响——其他事务与普通事件照常回放。

无事务字段、无提交清单时，上述流程不产生任何记录，回放语义与旧版完全一致。

## 6. 期望 SCD2 版本链

每个业务键的版本从 1 递增，按 `valid_from` 排序，`valid_to` 取下一个版本的
`valid_from`，最后一条为 `None`。因此版本链天然连续、无重叠，且全部采用半开区间。
`noop` 不产生版本，所以相邻版本之间不会出现零长度区间。

## 7. 实际历史诊断

实际 CSV 每行成为一个 `ActualVersion`。诊断分两层：

- 行/键级结构检查在 `structural.py`：重叠、空洞、重复版本、多当前行、无当前行、
  `is_current` 与 `valid_to` 不一致；
- 语义检查在 `diagnose.py`：缺失期望键、孤立实际键、同起点版本属性漂移。

定义细节：

- **重叠**：任意两条合法区间的交集非空；
- **空洞**：按起点排序合并覆盖后，下一条起点严格晚于当前覆盖终点；
- **重复版本**：同业务键、同起点、同跟踪属性签名的多行；
- **多当前行**：`is_current=true` 多于一行；
- **无当前行**：该键存在实际行，但没有任何 `valid_to` 为空的行；
- **属性漂移**：期望版本存在同起点实际版本，但没有任何同起点行属性完全相同；
- **当前快照漂移**：开放实际行的属性多重集与 `member_current.csv` 的属性多重集
  不一致。

非法区间（`valid_to <= valid_from`）单独产生发现，结构检查会跳过该行，避免制造
二次噪声。

## 8. as-of 对账

对每个业务键，抽样时刻包括：

- 期望和实际的所有 `valid_from`；
- 所有不晚于 `--now` 的有限 `valid_to`；
- `--now` 本身。

对时刻 `t`，期望匹配集合和实际匹配集合均遵循 `valid_from <= t < valid_to`
（空末端视为无穷）。状态为：

- `match`：各一条且属性相同；
- `attribute_drift`：各一条但属性不同；
- `missing_actual`：期望有、实际无；
- `orphan_actual`：实际有、期望无；
- `multiple_actual`：实际匹配多条；
- `multiple_expected`：期望匹配多条（正常回放不应出现，用于防御）。

两端都无匹配的时刻不输出检查行。

## 9. 计划

每个发现映射为一个动作，动作包含：

- 稳定 `ACT-xxxx` ID；
- 动作类型、业务键、区间、关联 finding ID；
- 理由、风险、是否需人工复核；
- `execution_mode=dry_run_only` 与来源证据。

计划不包含可执行 SQL。动作按 finding ID 排序，属性、键和计数均稳定排序。

## 10. 审计与确定性

- 输入哈希：契约、事件日志、实际历史、当前快照的 sha256 写入 `artifacts`，
  同时进入报告 `inputs`。
- SQLite 表使用确定字段与固定插入顺序。
- JSON 使用 `sort_keys=True`、稳定缩进和换行；HTML/Markdown 按稳定列表渲染。
- 不读取系统时钟；所有时间来自输入或显式 `--now`。
- 不使用随机数、临时 UUID 或进程相关信息作为输出。
- 相同输入、配置和 `--now` 在干净工作区产生字节一致的 JSON、Markdown、HTML、
  计划和 SQLite 台账。

## 11. 服务安全

- host 白名单只接受三个回环值，绑定前拒绝其他值；
- 只实现 GET/HEAD，写方法返回 405；
- 静态文件只有三个报告文件名的白名单，不做任意路径映射；
- HTML 对全部动态文本做转义；
- 无外部 URL、脚本、字体或资源引用；
- 服务端只从已生成报告读取，不访问输入文件、不触发流水线。

## 12. 取舍

- 使用线性扫描而不是索引结构：fixture 小、审计可读性优先；
- 当前快照只与实际开放行对账，不直接与期望对账，避免把业务差异重复归类；
- 属性漂移按“同起点”保守识别，结构性错位由重叠/空洞和 as-of 对账承担；
- 所有计划动作都需要人工复核，不尝试自动生成最终修复语句。
