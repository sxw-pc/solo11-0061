"""Pure renderers for Markdown and HTML; no network or template packages."""

from __future__ import annotations

import html
import json
from typing import Any


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 维史校准台 · SCD2 Calibration Report",
        "",
        f"- Generated at (UTC): `{report['generated_at']}`",
        f"- Table: `{report['contract']['table']}`",
        f"- Contract SHA-256: `{report['contract']['sha256']}`",
        f"- Mode: read-only / dry-run",
        "",
        "## Run summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in (
        "event_rows",
        "unique_event_ids",
        "duplicate_exact",
        "duplicate_conflict",
        "noop_events",
        "expected_versions",
        "actual_versions",
        "current_snapshot_rows",
        "findings_total",
        "asof_checks",
        "asof_mismatches",
        "plan_actions",
    ):
        lines.append(f"| `{key}` | {summary[key]} |")
    lines.extend(["", "## Findings", ""])
    if report["findings"]:
        lines.append("| ID | Code | Severity | Key | Interval | Message |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in report["findings"]:
            lines.append(
                "| {id} | `{code}` | {sev} | `{key}` | {interval} | {msg} |".format(
                    id=item["finding_id"],
                    code=_md(item["code"]),
                    sev=_md(item["severity"]),
                    key=_md(item["natural_key"] or "-"),
                    interval=_md(_interval(item)),
                    msg=_md(item["message"]),
                )
            )
    else:
        lines.append("_No findings._")
    lines.extend(["", "## 复核分组 · Review triage", ""])
    lines.extend(_triage_lines(report["triage"]))
    lines.extend(["", "## As-of reconciliation", ""])
    lines.append(
        "Status counts: `"
        + json.dumps(summary["asof_by_status"], ensure_ascii=False, sort_keys=True)
        + "`"
    )
    mismatches = [
        item for item in report["asof_checks"] if item["status"] != "match"
    ]
    if mismatches:
        lines.extend(
            [
                "",
                "| Check | Key | At | Status | Expected seq | Actual SKs |",
                "| --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for item in mismatches:
            lines.append(
                "| {id} | `{key}` | `{at}` | `{status}` | {seq} | {sks} |".format(
                    id=item["check_id"],
                    key=_md(item["natural_key"]),
                    at=item["at"],
                    status=item["status"],
                    seq=item["expected_version_seq"]
                    if item["expected_version_seq"] is not None
                    else "-",
                    sks=_md(", ".join(item["actual_member_sks"]) or "-"),
                )
            )
    else:
        lines.extend(["", "_All sampled instants match._"])
    lines.extend(["", "## Dry-run repair plan", ""])
    if report["plan"]:
        lines.extend(
            [
                "| Action | Kind | Key | Risk | Review | Rationale |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for action in report["plan"]:
            lines.append(
                "| {id} | `{kind}` | `{key}` | {risk} | {review} | {why} |".format(
                    id=action["action_id"],
                    kind=_md(action["kind"]),
                    key=_md(action["natural_key"] or "-"),
                    risk=action["risk"],
                    review="yes" if action["review_required"] else "no",
                    why=_md(action["rationale"]),
                )
            )
    else:
        lines.append("_No repair actions proposed._")
    lines.extend(["", "## Expected history", ""])
    lines.extend(_history_table(report["expected_history"], expected=True))
    lines.extend(["", "## Actual history", ""])
    lines.extend(_history_table(report["actual_history"], expected=False))
    lines.extend(
        [
            "",
            "> This report is evidence for a dry-run plan. It does not execute DDL, "
            "DML or any database connection.",
            "",
        ]
    )
    return "\n".join(lines)


def render_html(report: dict[str, Any]) -> str:
    summary = report["summary"]
    contract = report["contract"]
    cards = "".join(
        f'<div class="card"><span>{html.escape(label)}</span><strong>{summary[key]}</strong></div>'
        for key, label in (
            ("findings_total", "Findings"),
            ("asof_mismatches", "As-of mismatches"),
            ("plan_actions", "Plan actions"),
            ("expected_versions", "Expected versions"),
        )
    )
    finding_rows = "".join(
        "<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(item["finding_id"]),
            html.escape(item["code"]),
            html.escape(item["severity"]),
            html.escape(item["natural_key"]),
            html.escape(_interval(item)),
            html.escape(item["message"]),
        )
        for item in report["findings"]
    ) or '<tr><td colspan="6" class="empty">No findings.</td></tr>'
    plan_rows = "".join(
        "<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(action["action_id"]),
            html.escape(action["kind"]),
            html.escape(action["natural_key"]),
            html.escape(action["risk"]),
            html.escape(action["rationale"]),
        )
        for action in report["plan"]
    ) or '<tr><td colspan="5" class="empty">No repair actions.</td></tr>'
    asof_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
            html.escape(item["check_id"]),
            html.escape(item["natural_key"]),
            html.escape(item["at"]),
            html.escape(item["status"]),
            html.escape(", ".join(item["actual_member_sks"])),
        )
        for item in report["asof_checks"]
        if item["status"] != "match"
    ) or '<tr><td colspan="5" class="empty">All sampled instants match.</td></tr>'
    triage = report["triage"]
    triage_rows = "".join(
        "<tr><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(group["group_id"]),
            html.escape(group["root_finding_id"]),
            html.escape(group["natural_key"]),
            html.escape(", ".join(str(row) for row in group["untrustworthy_rows"])),
            html.escape(
                ", ".join(
                    item["finding_id"] for item in group["derived_findings"]
                )
                or "-"
            ),
            html.escape(", ".join(str(row) for row in group["proof_rows"])),
        )
        for group in triage["groups"]
    ) or '<tr><td colspan="6" class="empty">No root-cause groups identified.</td></tr>'
    events = "".join(
        "<tr><td>{}</td><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(event["raw_line"])),
            html.escape(event["event_id"]),
            html.escape(event["effective_at"]),
            html.escape(event["status"]),
            html.escape(event["natural_key"]),
        )
        for event in report["event_ledger"]
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DimHist · SCD2 Calibration Report</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f5ef; --fg:#172026; --muted:#5c6b73; --line:#d6d0c4; --accent:#0b6e69; --card:#fffdf8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#12181b; --fg:#e9eee9; --muted:#a8b3ad; --line:#354247; --accent:#7fd1c9; --card:#1a2327; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 "Segoe UI",system-ui,sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:32px 20px 64px; }}
h1,h2 {{ line-height:1.2; }} h1 {{ font-size:30px; margin:0 0 4px; }} h2 {{ margin-top:34px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
.meta,.muted {{ color:var(--muted); }} .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:22px 0; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }} .card span {{ display:block; color:var(--muted); font-size:13px; }} .card strong {{ font-size:28px; font-variant-numeric:tabular-nums; }}
table {{ width:100%; border-collapse:collapse; font-size:13.5px; background:var(--card); }} th,td {{ border:1px solid var(--line); padding:7px 9px; text-align:left; vertical-align:top; }} th {{ color:var(--muted); font-weight:600; }} code {{ color:var(--accent); }}
.empty {{ color:var(--muted); text-align:center; }} nav a {{ color:var(--accent); margin-right:14px; }} footer {{ margin-top:38px; color:var(--muted); font-size:12.5px; }}
</style>
</head>
<body><main>
<h1>维史校准台 · SCD2 Calibration Report</h1>
<div class="meta">Generated at {html.escape(report['generated_at'])} · Table {html.escape(contract['table'])} · Contract SHA-256 <code>{html.escape(contract['sha256'])}</code></div>
<nav><a href="/api/summary">/api/summary</a><a href="/api/findings">/api/findings</a><a href="/api/asof">/api/asof</a><a href="/api/plan">/api/plan</a><a href="/api/events">/api/events</a></nav>
<div class="cards">{cards}</div>
<h2>Run summary</h2>
<table><tbody>
<tr><th>Event rows</th><td>{summary['event_rows']}</td><th>Unique event IDs</th><td>{summary['unique_event_ids']}</td></tr>
<tr><th>Exact duplicates</th><td>{summary['duplicate_exact']}</td><th>Conflicting duplicates</th><td>{summary['duplicate_conflict']}</td></tr>
<tr><th>No-op events</th><td>{summary['noop_events']}</td><th>Actual versions</th><td>{summary['actual_versions']}</td></tr>
<tr><th>As-of checks</th><td>{summary['asof_checks']}</td><th>As-of mismatches</th><td>{summary['asof_mismatches']}</td></tr>
</tbody></table>
<h2>Findings</h2>
<table><thead><tr><th>ID</th><th>Code</th><th>Severity</th><th>Key</th><th>Interval</th><th>Message</th></tr></thead><tbody>{finding_rows}</tbody></table>
<h2>复核分组 · Review triage</h2>
<p class="muted">Review grouping only: no finding is removed, no history is rewritten, no repair is executed. Root causes: {triage['root_cause_count']} · derived symptoms: {triage['derived_count']} · unattributed: {triage['unattributed_count']}.</p>
<table><thead><tr><th>Group</th><th>Root finding</th><th>Key</th><th>Untrustworthy rows</th><th>Derived findings</th><th>Proof rows</th></tr></thead><tbody>{triage_rows}</tbody></table>
<h2>As-of mismatches</h2>
<table><thead><tr><th>Check</th><th>Key</th><th>At</th><th>Status</th><th>Actual SKs</th></tr></thead><tbody>{asof_rows}</tbody></table>
<h2>Dry-run repair plan</h2>
<table><thead><tr><th>Action</th><th>Kind</th><th>Key</th><th>Risk</th><th>Rationale</th></tr></thead><tbody>{plan_rows}</tbody></table>
<h2>Event ledger</h2>
<table><thead><tr><th>Line</th><th>Event</th><th>Effective at</th><th>Status</th><th>Key</th></tr></thead><tbody>{events}</tbody></table>
<footer>Read-only evidence. No SQL is executed and no external service is contacted. Inputs: {len(report['inputs'])} hashed artifact(s).</footer>
</main></body></html>
"""


def render_plan_markdown(document: dict[str, Any]) -> str:
    lines = [
        "# DimHist Dry-Run Repair Plan",
        "",
        f"- Generated at: `{document['generated_at']}`",
        f"- Table: `{document['table']}`",
        f"- Mode: `{document['mode']}`",
        "",
        "| Action | Kind | Key | Interval | Risk | Review | Rationale |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for action in document["actions"]:
        lines.append(
            "| {id} | `{kind}` | `{key}` | {interval} | {risk} | {review} | {why} |".format(
                id=action["action_id"],
                kind=_md(action["kind"]),
                key=_md(action["natural_key"] or "-"),
                interval=_md(_interval(action)),
                risk=action["risk"],
                review="yes" if action["review_required"] else "no",
                why=_md(action["rationale"]),
            )
        )
    lines.extend(
        [
            "",
            "> Every action is advisory. This file is not executable SQL and must not "
            "be applied without review.",
            "",
        ]
    )
    return "\n".join(lines)


def _triage_lines(triage: dict[str, Any]) -> list[str]:
    lines = [
        "> 复核分组仅组织证据：不删除任何发现、不改写历史、不自动修复。"
        " Review grouping only; raw findings and plan references are unchanged.",
        "",
        "Root causes: `{root}` · derived symptoms: `{derived}` · unattributed: "
        "`{unattributed}` · findings total: `{total}`".format(
            root=triage["root_cause_count"],
            derived=triage["derived_count"],
            unattributed=triage["unattributed_count"],
            total=triage["findings_total"],
        ),
        "",
    ]
    if triage["groups"]:
        lines.append("| Group | Root finding | Key | Untrustworthy rows | Derived findings | Proof rows |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for group in triage["groups"]:
            derived = ", ".join(
                item["finding_id"] for item in group["derived_findings"]
            ) or "-"
            lines.append(
                "| {gid} | `{root}` | `{key}` | {rows} | {derived} | {proof} |".format(
                    gid=group["group_id"],
                    root=_md(group["root_finding_id"]),
                    key=_md(group["natural_key"]),
                    rows=", ".join(str(row) for row in group["untrustworthy_rows"]),
                    derived=_md(derived),
                    proof=", ".join(str(row) for row in group["proof_rows"]),
                )
            )
    else:
        lines.append("_No root-cause groups identified._")
    unattributed = ", ".join(triage["unattributed_findings"]) or "-"
    lines.extend(["", f"Unattributed findings: {_md(unattributed)}"])
    return lines


def _history_table(rows: list[dict[str, Any]], expected: bool) -> list[str]:
    if not rows:
        return ["_None._"]
    lines = [
        "| Key | Seq/SK | From | To | Attributes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        ident = row["version_seq"] if expected else row["member_sk"]
        attrs = ", ".join(
            f"{name}={value}" for name, value in sorted(row["attributes"].items())
        )
        lines.append(
            "| `{}` | {} | {} | {} | {} |".format(
                _md(row["natural_key"]),
                ident,
                row["valid_from"],
                row["valid_to"] or "open",
                _md(attrs),
            )
        )
    return lines


def _interval(item: dict[str, Any]) -> str:
    start = item["interval"]["from"]
    end = item["interval"]["to"]
    if start is None and end is None:
        return "n/a"
    return f"[{start or '?'}, {end or 'open'})"


def _md(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
