# 契约与输入格式

## 1. 文件位置

工作区默认输入：

```text
<workspace>/contracts/dimension_member.json
<workspace>/data/events/change_events.jsonl
<workspace>/data/dimension/member_history.csv
<workspace>/data/dimension/member_current.csv
```

`demo` 从项目内置 fixture 复制这些文件；`run` 直接读取工作区中的现有文件。原始
输入只读，不会被改写。所有文件都计算 sha256，写入 SQLite `artifacts` 和报告
`inputs`。

## 2. 维表契约 JSON

内置契约：

```json
{
  "schema_version": 1,
  "table": "dim_member",
  "natural_key": "member_id",
  "tracked_attributes": ["segment", "region", "status"],
  "attribute_types": {
    "segment": "string",
    "region": "string",
    "status": "string"
  },
  "effective_at_field": "effective_at",
  "event_id_field": "event_id",
  "sequence_field": "sequence",
  "payload_field": "payload",
  "same_time_policy": "sequence_then_event_id_then_line",
  "time_format": "iso8601_utc_seconds",
  "delete_semantics": "status_only"
}
```

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `schema_version` | 是 | 必须为整数 `1` |
| `table` | 是 | 非空字符串，仅用于展示和审计 |
| `natural_key` | 是 | 非空字符串，不能同时出现在跟踪属性中 |
| `tracked_attributes` | 是 | 非空字符串数组，不允许重复 |
| `attribute_types` | 是 | 键集合必须与跟踪属性完全一致 |
| `attribute_types.*` | 是 | `string`、`integer`、`boolean` 或 `decimal` |
| `effective_at_field` | 是 | 事件生效时间字段名 |
| `event_id_field` | 是 | 事件稳定 ID 字段名 |
| `sequence_field` | 是 | 同刻排序的非负整数字段名 |
| `payload_field` | 是 | 属性更新对象字段名 |
| `same_time_policy` | 是 | 仅支持 `sequence_then_event_id_then_line` |
| `time_format` | 是 | 仅支持 `iso8601_utc_seconds` |
| `delete_semantics` | 是 | 支持 `status_only` 或 `forbid` |

四个字段名（生效时间、事件 ID、序列、payload）必须互不相同，且不能等于业务键。
契约其余额外字段会被忽略。

`status_only` 表示停用通过跟踪属性（例如 `status=Inactive`）表达，不做物理删除；
`forbid` 声明本契约不做删除语义。删除语义只影响契约合法性，不会自动删除任何行。

## 3. 变更事件 JSONL

每行一个 JSON 对象，不允许空对象 payload：

```json
{"event_id":"EVT-1001","member_id":"M-1001","effective_at":"2024-01-01T00:00:00Z","sequence":1,"payload":{"segment":"Bronze","region":"APAC","status":"Active"}}
```

规则：

- `event_id`、`member_id`、`effective_at` 必须是非空字符串；`sequence` 必须是非负
  整数。
- 时间要求带时区的 ISO8601，并规范化为 UTC 整秒；`Z` 和 `+00:00` 均可。
- `payload` 必须是非空对象，键只能来自 `tracked_attributes`，允许只包含部分属性。
- 空行忽略；每个非空行保留行号、原始文本和行 sha256。
- JSONL 整体 sha256 写入台账与报告。

去重和归并细节见 [DESIGN.md](DESIGN.md) 第 4、5 节。

## 4. 实际历史 CSV

表头必须包含：

```text
member_sk,member_id,segment,region,status,valid_from,valid_to,is_current,source,updated_at
```

其中 `member_id` 和三个属性列由契约决定，其余列名固定。

| 列 | 规则 |
| --- | --- |
| `member_sk` | 非空代理键，用于行级证据 |
| `member_id` | 非空业务键 |
| `segment`,`region`,`status` | 跟踪属性值 |
| `valid_from` | 必填 UTC ISO8601 整秒 |
| `valid_to` | 可空；空表示开放区间；不晚于 `valid_from` 会报 `DIM_INVALID_INTERVAL` |
| `is_current` | `true/false`（也接受 `1/0`、`yes/no`，大小写不敏感） |
| `source` | 合成来源标签 |
| `updated_at` | 可空；非空时必须是 UTC ISO8601 整秒 |

额外列允许存在。表头重复、行宽异常、缺少必需列直接产生 `INP_002`。CSV 文件
sha256 写入台账与报告。

## 5. 当前快照 CSV

表头必须包含业务键和全部跟踪属性，可选 `updated_at`：

```text
member_id,segment,region,status,updated_at
```

该文件用于校验“当前快照”是否与 `valid_to` 为空的开放实际行一致。比较采用属性多重
集，因此多当前行也会被如实比较；不一致产生 `DIM_CURRENT_SNAPSHOT_DRIFT`。

## 6. 时间与排序

- 统一格式：`YYYY-MM-DDTHH:MM:SSZ`。
- 输入允许带偏移（例如 `2024-01-01T08:00:00+08:00`），规范化为 UTC。
- 小数秒会被拒绝，以保证秒精度和稳定排序。
- 事件回放排序：`natural_key, effective_at, sequence, event_id, raw_line`。
- 发现排序：phase、code、业务键、区间、消息和规范 details；随后分配 `FND-xxxx`。
- as-of 排序：业务键、时刻、状态；随后分配 `ASOF-xxxx`。
- 计划动作按 finding ID 排序并分配 `ACT-xxxx`。

## 7. 审计台账

SQLite 文件默认位于 `<workspace>/ledger/dimhist.sqlite3`。表：

| 表 | 内容 |
| --- | --- |
| `meta` | schema 版本、生成时间、表名、契约哈希、as-of now、计数 |
| `artifacts` | 输入 kind、相对路径和 sha256 |
| `events` | 全部非空事件行及状态、payload、原始行号、行哈希、原始 JSON |
| `expected_versions` | 回放得到的期望版本链 |
| `actual_versions` | 实际历史行 |
| `current_snapshot` | 当前快照行 |
| `findings` | 发现及规范 details |
| `asof_checks` | as-of 抽样结果 |
| `plan_actions` | dry-run 动作 |

每张表字段固定、插入顺序固定。台账可由只读 SQL 独立审计，但项目不提供任何修复
SQL 执行入口。

## 8. 报告 JSON 顶层结构

```text
schema_version, generated_at, read_only
contract
inputs[]
summary
event_ledger[]
expected_history[]
actual_history[]
current_snapshot[]
findings[]
asof_checks[]
plan[]
```

`summary` 与 `fixtures/expected/demo_summary.json` 一致；是演示回归的权威快照。
报告 JSON、计划 JSON 和 SQLite 台账共享同一份哈希和发现证据。

## 9. 修改契约时的检查清单

1. 运行 `python -m unittest discover -s tests -v`。
2. 重新运行 demo，确认汇总是否符合预期。
3. 若演示汇总有意变化，更新 `fixtures/expected/demo_summary.json`。
4. 检查 `docs/FINDING_CODES.md` 是否需要同步。
5. 确认未引入第三方依赖、网络访问或真实敏感数据。
