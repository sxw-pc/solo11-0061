# 合规与约束说明

本文件逐项说明项目对硬约束的落实方式，以及可复核的证据。

## 1. 完全离线

- 运行期代码不导入 `requests`、`urllib.request`、`http.client`、云 SDK 或任何数据库
  驱动；HTTP 服务使用标准库 `ThreadingHTTPServer`，只在本机回环地址监听。
- 产品代码不访问外部 URL、对象存储、消息队列、生产数据库或外部模型服务。
- 测试中的 HTTP 请求全部发往测试进程自己绑定在 `127.0.0.1` 的临时端口。
- HTML 报告无外部脚本、样式、字体或图片引用。
- 项目运行不读取系统时钟；所有时间来自 fixture 或显式 `--now`。

复核方式：

```bash
grep -RInE 'requests|urllib\.request|http\.client|flask|fastapi|pandas|sqlalchemy|boto|yaml' src || true
```

`src` 中应无第三方库导入。服务端自身使用标准库 HTTP 实现，不在此扫描模式内。

## 2. 仅标准库

使用 Python 3.10+ 标准库：`argparse`、`csv`、`json`、`sqlite3`、`hashlib`、
`http.server`、`dataclasses`、`datetime`、`pathlib`、`unittest`、`tempfile`、
`shutil`、`urllib.parse`、`html`、`threading`（测试）等。

没有 `requirements.txt`，没有虚拟环境假设，没有安装步骤。

## 3. 本地服务安全

- `validate_loopback_host` 只接受 `127.0.0.1`、`localhost`、`::1`。
- 其他 host 在 socket 绑定前以 `SRV_001` 拒绝并退出。
- 默认 host `127.0.0.1`、端口 `5017`。
- 只实现 GET/HEAD；POST/PUT/PATCH/DELETE 返回 405。
- 静态文件只允许三个固定报告文件名；`/reports/` 下任意其他路径返回 404。
- 动态 HTML 全部经过 `html.escape`，防止 fixture 或发现消息注入。
- 服务只读取已生成报告，不触发流水线、不读取输入、不写文件。

## 4. 一条命令演示与测试

```bash
PYTHONPATH=src python -m dimhist demo --workspace .dimhist-work --reset
python -m unittest discover -s tests -v
PYTHONPATH=src python -m dimhist serve --workspace .dimhist-work --host 127.0.0.1 --port 5017
```

demo 自动完成 fixture 安装、契约校验、摄取、回放、诊断、as-of、计划、SQLite 台账、
JSON/Markdown/HTML 报告，并将汇总与静态基准比对。

## 5. 代码规模

当前约束：

- 全部 `.py`（含测试）总行数在 2500–5000；
- 任一 `.py` 文件不超过 300 行；
- 模块职责单一，不使用压缩单行、复制粘贴或大量空行凑数。

复核方式：

```bash
find . -path './.dimhist-work' -prune -o -name '*.py' -print0 \
  | xargs -0 wc -l | tail -1
find src tests -name '*.py' -print0 | xargs -0 wc -l | sort -n | tail
```

最终实现约 4700 行 Python，最大模块低于 300 行。文档与 fixture 不计入代码行数。

## 6. 确定性

同一输入、同一契约、同一 `--now`：

- JSON 使用规范键排序、固定缩进和换行；
- 发现、检查、计划、事件和版本在写出前稳定排序；
- ID 由排序后的序号生成，不使用随机 UUID；
- 不读取当前系统时间，不写入进程 ID、绝对临时路径或主机名；
- HTML/Markdown 表格按稳定列表顺序生成；
- SQLite 按固定 schema 和固定插入顺序重建；
- demo 的 fixture 复制内容固定。

已验证在干净工作区重复执行 demo 会生成字节一致的 SQLite、JSON、Markdown 和 HTML。

## 7. 修复计划安全边界

- 计划只包含文本与 JSON，不含可执行 SQL 文件；
- 每个动作 `execution_mode=dry_run_only`、`review_required=true`；
- pipeline 不连接目标数据仓库，不执行 DDL/DML；
- 原始输入文件从不被写入；
- 计划动作仅引用本地 finding ID、区间、业务键和证据；
- README 与计划尾部均明确提示须人工复核、审批、备份和回滚演练。

SQLite 台账自身使用本地 SQL 维护审计表，这不属于对源数据库的修复执行。

## 8. 数据与隐私

- 所有业务键、代理键、来源标签、组织/人员概念均为占位合成值；
- 无真实公司名、真实域名、真实邮箱、电话号码、身份证号、地址或个人信息；
- 无密钥、令牌、密码或证书；
- 时间均为虚构的 2024–2026 年 UTC 时间；
- 来源列统一为 `synthetic_seed`；
- 不包含真实生产 SQL、连接串或目标地址。

## 9. 可审计性

- 契约、事件日志、实际历史、当前快照四个输入均记录 sha256；
- 事件保留原始行号、原始 JSON 文本、行哈希、payload 和最终状态；
- 期望版本记录来源 event_id；
- 每条发现保留业务键、半开区间、消息、severity 和结构化 details；
- 每条 as-of 检查保留期望版本序号、实际代理键、属性差异；
- 每条计划动作关联 finding ID 和证据；
- SQLite 报告产物可离线查询和归档。

## 10. 测试覆盖

`unittest` 共覆盖：

- 契约全部校验错误码与文件哈希；
- JSONL 行号/原文/哈希保真；
- 完全重复、冲突重复、同刻归并、event_id 排序、no-op；
- 迟到事件、属性回退、期望区间连续性；
- 重叠、空洞、重复版本、多当前行、无当前行、漂移、孤立、缺失、非法区间；
- as-of 半开边界、空洞、多匹配、漂移、孤立；
- 计划映射、稳定 ID、dry-run 标记；
- 修复预演：先拆后改的依赖推导、声明顺序不覆盖依赖、矛盾/循环/未知引用拒绝、
  过期指纹前提、非法参数、复合动作回滚恢复原始多重集、端到端证据保留，
  以及预演对台账与源文件无副作用；
- SQLite 表计数、原始事件文本、输入哈希；
- 报告摘要基准、Markdown/HTML 输出、HTML 转义、无外部 URL；
- 字节级重复运行一致性；
- 回环 host 白名单、HTTP 端点、HEAD、过滤、405、404、路径穿越；
- `init`、`demo`、重置保护、自定义 `--now`、CLI 无堆栈错误、外部 host 拒绝。

运行结果以最终 `python -m unittest discover -s tests -v` 为准。

## 11. 已知边界

- 不实现通用类型转换引擎：`attribute_types` 做契约静态校验，当前 fixture 属性值
  按原始字符串比较；
- 不自动判断业务修复的正确方向，所有动作都需要人工复核；
- 当前快照只与开放实际行比较，不直接与期望版本比较；
- 不提供历史快照时间旅行或跨表血缘；
- 本工具面向离线校准与审计证据，不面向高并发或大数据量吞吐。
