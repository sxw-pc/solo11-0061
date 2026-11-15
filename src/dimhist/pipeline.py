"""End-to-end orchestration for initialization and calibration runs."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .actual import load_actual_history, load_current_snapshot
from .asof import asof_stats, finalize_asof, reconcile_asof
from .config import Contract, load_contract
from .diagnose import diagnose_actual, diagnose_current_snapshot
from .ledger import Ledger
from .model import (
    ActualVersion,
    AsOfCheck,
    CurrentRow,
    EventRecord,
    ExpectedVersion,
    Finding,
    PlanAction,
)
from .planner import build_plan, build_simulation_document
from .replay import ingest_and_replay
from .simulate import simulate_repairs
from .util import (
    DimhistError,
    canonical_json,
    normalize_time,
    relative_display,
    sha256_file,
)

WORKSPACE_MARKER = ".dimhist-workspace"
WORKSPACE_DIRS = (
    "contracts",
    "data/events",
    "data/dimension",
    "ledger",
    "reports",
    "plans",
)


@dataclass
class RunResult:
    contract: Contract
    now: str
    events: list[EventRecord]
    expected: list[ExpectedVersion]
    actual: list[ActualVersion]
    current_rows: list[CurrentRow]
    findings: list[Finding]
    asof_checks: list[AsOfCheck]
    plan: list[PlanAction]
    event_stats: dict[str, int]
    inputs: list[dict[str, str]]
    workspace: Path


def init_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for relative in WORKSPACE_DIRS:
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    marker = workspace / WORKSPACE_MARKER
    if not marker.exists():
        marker.write_text("dimhist-workspace-v1\n", encoding="utf-8", newline="\n")


def reset_workspace(workspace: Path) -> None:
    resolved = workspace.resolve()
    if resolved.parent == resolved:
        raise DimhistError("WS_001", "refusing to reset a filesystem root")
    if workspace.exists():
        if not workspace.is_dir():
            raise DimhistError("WS_001", f"workspace is not a directory: {workspace}")
        entries = list(workspace.iterdir())
        has_marker = (workspace / WORKSPACE_MARKER).is_file()
        if entries and not has_marker:
            raise DimhistError(
                "WS_002",
                "refusing to reset a non-empty directory without the DimHist marker",
            )
        shutil.rmtree(workspace)
    init_workspace(workspace)


def run_pipeline(
    workspace: Path,
    now: str,
    contract_path: Path | None = None,
    event_path: Path | None = None,
    history_path: Path | None = None,
    current_path: Path | None = None,
) -> RunResult:
    contract_path = contract_path or workspace / "contracts" / "dimension_member.json"
    event_path = event_path or workspace / "data" / "events" / "change_events.jsonl"
    history_path = history_path or workspace / "data" / "dimension" / "member_history.csv"
    current_path = current_path or workspace / "data" / "dimension" / "member_current.csv"
    normalized_now = normalize_time(now, code="NOW_001", field="now")

    contract = load_contract(contract_path)
    replay = ingest_and_replay(contract, event_path)
    actual, findings = load_actual_history(contract, history_path)
    current_rows, current_sha = load_current_snapshot(contract, current_path)
    findings.extend(replay.findings)
    findings.extend(diagnose_actual(contract, replay.expected_versions, actual))
    findings.extend(
        diagnose_current_snapshot(contract, actual, current_rows)
    )
    findings = finalize_findings(findings)
    checks = finalize_asof(
        reconcile_asof(contract, replay.expected_versions, actual, normalized_now)
    )
    plan = build_plan(findings)

    inputs = [
        _input(workspace, "contract", contract_path, contract.sha256),
        _input(workspace, "event_log", event_path, sha256_file(event_path)),
        _input(workspace, "actual_history", history_path, sha256_file(history_path)),
        _input(workspace, "current_snapshot", current_path, current_sha),
    ]

    ledger_path = workspace / "ledger" / "dimhist.sqlite3"
    with Ledger(ledger_path) as ledger:
        ledger.reset()
        ledger.set_meta("schema_version", "1")
        ledger.set_meta("generated_at", normalized_now)
        ledger.set_meta("table", contract.table)
        ledger.set_meta("contract_sha256", contract.sha256)
        ledger.set_meta("asof_now", normalized_now)
        for item in inputs:
            ledger.add_artifact(item["kind"], item["path"], item["sha256"])
        ledger.add_events(replay.events)
        ledger.add_expected(replay.expected_versions)
        ledger.add_actual(actual)
        ledger.add_current_snapshot(current_rows)
        ledger.add_findings(findings)
        ledger.add_asof(checks)
        ledger.add_plan(plan)
        ledger.set_meta("finding_count", str(len(findings)))
        ledger.set_meta("asof_mismatch_count", str(asof_stats(checks)["mismatches"]))
        ledger.commit()

    return RunResult(
        contract=contract,
        now=normalized_now,
        events=replay.events,
        expected=replay.expected_versions,
        actual=actual,
        current_rows=current_rows,
        findings=findings,
        asof_checks=checks,
        plan=plan,
        event_stats=replay.stats,
        inputs=inputs,
        workspace=workspace,
    )


def run_plan_simulation(result: RunResult, request: dict[str, Any]) -> dict[str, Any]:
    """Preview selected repair actions in memory; sources and ledger stay untouched."""
    if isinstance(request, dict) and isinstance(request.get("steps"), list):
        known = {action.action_id for action in result.plan}
        for raw in request["steps"]:
            linked = raw.get("plan_action") if isinstance(raw, dict) else None
            if linked is not None and linked not in known:
                raise DimhistError("SIM_001", f"unknown plan action: {linked}")
    outcome = simulate_repairs(
        result.contract, result.expected, result.actual, result.now, request
    )
    return build_simulation_document(
        result.contract, result.now, outcome, result.plan, result.findings
    )


def finalize_findings(findings: list[Finding]) -> list[Finding]:
    phase_order = {"event": 0, "diagnose": 1}
    ordered = sorted(
        findings,
        key=lambda item: (
            phase_order.get(item.phase, 9),
            item.code,
            item.natural_key,
            item.start or "",
            item.end or "",
            item.message,
            canonical_json(item.details),
        ),
    )
    for index, finding in enumerate(ordered, start=1):
        finding.finding_id = f"FND-{index:04d}"
    return ordered


def _input(workspace: Path, kind: str, path: Path, digest: str) -> dict[str, str]:
    return {
        "kind": kind,
        "path": relative_display(path, workspace),
        "sha256": digest,
    }
