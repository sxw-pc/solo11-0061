# 发现码与错误码字典

所有码均为稳定接口。码的含义变化需要提升项目 schema 版本；消息文本可读，但程序
不应依赖消息文本做分支。

## 1. 事件发现

| 码 | 严重度 | 触发条件 | 计划动作 |
| --- | --- | --- | --- |
| `EVT_DUP_CONFLICT` | high | 同一 event_id 出现内容不同的行；首行保留，后续行退出回放 | `REVIEW_EVENT_CONFLICT` |
| `EVT_SAME_TIME_CONFLICT` | medium | 同一业务键、同一生效时刻的归并组内，同一属性出现多个不同值 | `REVIEW_EVENT_ORDER` |
| `EVT_TX_QUARANTINED` | high | 事务不完整、成员内容冲突、依赖不可满足、循环依赖或次序结果真正冲突；整组隔离，成员事件全部退出回放 | `REVIEW_TRANSACTION` |

前两者的对象都保留在 SQLite `events` 表，并在 `details` 中记录行号和证据。
`EVT_TX_QUARANTINED` 的 `details.reason` 取值：`member_conflict`（成员 event_id
内容冲突）、`missing_member`（清单声明的成员缺失或 transaction_id 不符）、
`undeclared_member`（事件携带事务 ID 但未在清单声明）、`missing_seq`（成员缺
事务内序号）、`duplicate_seq`（事务内序号重复）、`missing_commit`（事件携带
事务 ID 但无提交清单声明）、`unmet_dependency`（依赖未知、指向更晚业务时刻或
指向被隔离事务）、`dependency_cycle`（循环依赖，`details.cycle` 为最小互相依赖
事务集）、`order_conflict`（多个可行次序产生不同结果，`details.conflicts` 记录
成员、属性与各候选值）。被隔离事务的成员事件在 `events` 表中标记 `quarantined`，
事务级证据写入 `transactions` 表。

## 2. 实际维表发现

| 码 | 严重度 | 触发条件 | 计划动作 |
| --- | --- | --- | --- |
| `DIM_OVERLAP` | high | 同一业务键两条合法实际区间存在正长度交集 | `RESOLVE_OVERLAP` |
| `DIM_GAP` | medium | 合并实际覆盖后，相邻版本之间出现正长度空洞 | `BACKFILL_GAP` |
| `DIM_DUPLICATE_VERSION` | medium | 同业务键、同 `valid_from`、同跟踪属性出现多行 | `MERGE_DUPLICATE_VERSION` |
| `DIM_MULTI_CURRENT` | high | 同业务键 `is_current=true` 多于一行 | `CLOSE_EXTRA_CURRENT` |
| `DIM_NO_CURRENT` | medium | 该键有实际行，但没有任何 `valid_to` 为空的行 | `CREATE_CURRENT_VERSION` |
| `DIM_CURRENT_FLAG` | low | 单行 `is_current` 与 `valid_to` 是否为空不一致 | `FIX_CURRENT_FLAG` |
| `DIM_ATTR_DRIFT` | medium | 期望与实际存在同起点版本，但没有同起点实际行属性和期望完全相同 | `REWRITE_VERSION_ATTRIBUTES` |
| `DIM_ORPHAN_ACTUAL` | medium | 实际业务键不在期望历史中 | `RETIRE_ORPHAN` |
| `DIM_MISSING_EXPECTED` | high | 期望业务键在实际历史中完全没有行 | `CREATE_MISSING_VERSIONS` |
| `DIM_INVALID_INTERVAL` | medium | `valid_to <= valid_from` | `REPAIR_INTERVAL` |
| `DIM_CURRENT_SNAPSHOT_DRIFT` | medium | 开放实际行的属性多重集与当前快照 CSV 不一致 | `REBUILD_CURRENT_SNAPSHOT` |

严重度用于报告排序与风险提示，不改变退出码。校准运行存在发现时仍可成功完成，
因为输出是证据而非失败判定。

## 3. as-of 状态

as-of 状态保存在 `asof_checks.status`，不是 `findings` 中的发现码。

| 状态 | 含义 |
| --- | --- |
| `match` | 期望与实际各一条，跟踪属性完全一致 |
| `attribute_drift` | 各一条，但跟踪属性有差异 |
| `missing_actual` | 该时刻有期望版本，无实际版本 |
| `orphan_actual` | 该时刻有实际版本，无期望版本 |
| `multiple_actual` | 该时刻匹配多条实际版本 |
| `multiple_expected` | 该时刻匹配多条期望版本（防御性状态） |

两端均无匹配的抽样时刻不写入检查表。

## 4. 事件状态

| 状态 | 含义 |
| --- | --- |
| `applied` | 该归并组生成了期望版本，且该事件是稳定排序末位 |
| `superseded_same_time` | 同刻归并组的前序事件，其 payload 已参与合并 |
| `noop` | 归并组最终状态与组前状态相同，没有生成版本 |
| `duplicate_exact` | 与首条同 event_id 事件内容完全相同，去重 |
| `duplicate_conflict` | 与首条同 event_id 事件内容不同，保留但不回放 |
| `quarantined` | 所属事务被整组隔离，事件不参与回放 |

## 4.1 事务状态

事务记录在 SQLite `transactions` 表与报告 `transactions` 段：

| 状态 | 含义 |
| --- | --- |
| `committed` | 事务完整且无冲突，已按全局可行次序整体进入期望历史 |
| `quarantined` | 事务被整组隔离，`quarantine_reason` 给出隔离原因 |

已提交事务的 `order_classification`：`unique` 表示该业务时刻只有一个可行次序；
`equivalent` 表示存在多个可行次序但已证明结果相同（可交换）。多个次序产生不同
结果属于真正冲突，事务被隔离（`order_conflict`），不会用 event_id 字典序掩盖。

## 5. 契约错误码

| 码 | 含义 |
| --- | --- |
| `CFG_001` | 契约文件不存在、不可读、非 UTF-8 或 JSON 非法 |
| `CFG_002` | 缺少必填字段、字段为空或基础类型/版本非法 |
| `CFG_003` | 跟踪属性重复，或业务键同时被列为跟踪属性 |
| `CFG_004` | 属性类型未知，或 `attribute_types` 与跟踪属性集合不一致 |
| `CFG_005` | `same_time_policy` 不支持 |
| `CFG_006` | `delete_semantics` 不支持 |
| `CFG_007` | `time_format` 不支持 |
| `CFG_008` | 契约字段名冲突或与业务键重名 |

## 6. 输入与运行错误码

| 码 | 含义 |
| --- | --- |
| `INP_001` | 输入文件不可读或不存在 |
| `INP_002` | JSONL/CSV 解析失败、CSV 表头缺失/重复/行宽异常或文件为空 |
| `INP_003` | 行级必填字段、类型、payload 或布尔值非法 |
| `INP_004` | 时间不是带时区的 ISO8601，或不是整秒精度 |
| `NOW_001` | `--now` 不是合法的 UTC 秒精度时间 |
| `WS_001` | 工作区路径不可重置（文件系统根、不是目录等） |
| `WS_002` | 无 DimHist 标记的非空目录拒绝被 `--reset` 删除 |
| `FIX_001` | 内置 fixture 或期望摘要文件缺失/损坏 |
| `DEMO_001` | 默认演示摘要与 `fixtures/expected/demo_summary.json` 不一致 |
| `IO_001` | CLI 执行期间本地文件系统操作失败 |
| `SRV_001` | host 不是 `127.0.0.1`、`localhost`、`::1`，或端口范围非法 |
| `SRV_002` | 报告不存在/损坏，或端口绑定失败 |
| `SRV_004` | 请求的静态文件不在允许集合或不存在 |

CLI 对预期错误统一输出一行：

```text
ERROR <CODE>: <message>
```

并返回退出码 2；不会把 Python 堆栈打印给最终用户。

## 7. 计划动作码

`REVIEW_EVENT_CONFLICT`、`REVIEW_EVENT_ORDER`、`REVIEW_TRANSACTION`、
`RESOLVE_OVERLAP`、`BACKFILL_GAP`、`MERGE_DUPLICATE_VERSION`、`CLOSE_EXTRA_CURRENT`、
`CREATE_CURRENT_VERSION`、`REWRITE_VERSION_ATTRIBUTES`、`RETIRE_ORPHAN`、
`CREATE_MISSING_VERSIONS`、`REPAIR_INTERVAL`、`FIX_CURRENT_FLAG`、
`REBUILD_CURRENT_SNAPSHOT`、`MANUAL_REVIEW`。

所有动作的 `details.execution_mode` 均为 `dry_run_only`，且
`review_required=true`。
