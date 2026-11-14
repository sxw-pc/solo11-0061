"""Build and persist stable JSON, Markdown and HTML calibration outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .asof import asof_stats
from .pipeline import RunResult
from .planner import plan_stats
from .render import render_html, render_markdown, render_plan_markdown
from .util import deep_plain, write_json, write_text


def build_summary(result: RunResult) -> dict[str, Any]:
    findings_by_code: dict[str, int] = {}
    findings_by_severity: dict[str, int] = {}
    for finding in result.findings:
        findings_by_code[finding.code] = findings_by_code.get(finding.code, 0) + 1
        findings_by_severity[finding.severity] = (
            findings_by_severity.get(finding.severity, 0) + 1
        )
    expected_members = {version.natural_key for version in result.expected}
    actual_members = {version.natural_key for version in result.actual}
    stats = result.event_stats
    asof = asof_stats(result.asof_checks)
    plan = plan_stats(result.plan)
    return {
        "status": "CALIBRATED",
        "now": result.now,
        "table": result.contract.table,
        "event_rows": stats["event_rows"],
        "unique_event_ids": stats["unique_event_ids"],
        "duplicate_exact": stats["duplicate_exact"],
        "duplicate_conflict": stats["duplicate_conflict"],
        "applied_events": stats["applied_events"],
        "noop_events": stats["noop_events"],
        "superseded_same_time": stats["superseded_same_time"],
        "same_time_conflict_groups": stats["same_time_conflict_groups"],
        "expected_versions": len(result.expected),
        "actual_versions": len(result.actual),
        "current_snapshot_rows": len(result.current_rows),
        "expected_members": len(expected_members),
        "actual_members": len(actual_members),
        "missing_expected_members": len(expected_members - actual_members),
        "orphan_actual_members": len(actual_members - expected_members),
        "findings_total": len(result.findings),
        "findings_by_code": dict(sorted(findings_by_code.items())),
        "findings_by_severity": dict(sorted(findings_by_severity.items())),
        "asof_checks": asof["checks"],
        "asof_mismatches": asof["mismatches"],
        "asof_by_status": asof["by_status"],
        "plan_actions": plan["actions"],
        "plan_review_required": plan["review_required"],
        "plan_by_kind": plan["by_kind"],
        "plan_by_risk": plan["by_risk"],
    }


def build_report(result: RunResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": result.now,
        "read_only": True,
        "contract": result.contract.to_dict(include_hash=True),
        "inputs": result.inputs,
        "summary": build_summary(result),
        "event_ledger": [event.to_dict() for event in result.events],
        "expected_history": [version.to_dict() for version in result.expected],
        "actual_history": [version.to_dict() for version in result.actual],
        "current_snapshot": [row.to_dict() for row in result.current_rows],
        "findings": [finding.to_dict() for finding in result.findings],
        "asof_checks": [check.to_dict() for check in result.asof_checks],
        "plan": [action.to_dict() for action in result.plan],
    }


def build_plan_document(result: RunResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": result.now,
        "mode": "dry_run_only",
        "table": result.contract.table,
        "contract_sha256": result.contract.sha256,
        "actions": [deep_plain(action.to_dict()) for action in result.plan],
    }


def write_reports(
    workspace: Path, result: RunResult
) -> dict[str, Path]:
    report = build_report(result)
    plan_document = build_plan_document(result)
    reports_dir = workspace / "reports"
    plans_dir = workspace / "plans"
    paths = {
        "json": reports_dir / "demo_report.json",
        "markdown": reports_dir / "demo_report.md",
        "html": reports_dir / "demo_report.html",
        "plan_json": plans_dir / "repair_plan.json",
        "plan_markdown": plans_dir / "repair_plan.md",
    }
    write_json(paths["json"], report)
    write_text(paths["markdown"], render_markdown(report))
    write_text(paths["html"], render_html(report))
    write_json(paths["plan_json"], plan_document)
    write_text(paths["plan_markdown"], render_plan_markdown(plan_document))
    return paths
