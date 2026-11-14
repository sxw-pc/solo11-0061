"""Shared test helpers; imports the src layout without installation."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dimhist.pipeline import init_workspace, run_pipeline  # noqa: E402
from dimhist.report import write_reports  # noqa: E402

FIXTURES = ROOT / "fixtures"
CONTRACT_PATH = ROOT / "contracts" / "dimension_member.json"
NOW = "2026-01-15T00:00:00Z"


def fixture_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def write_contract(directory: Path, **overrides: Any) -> Path:
    document = fixture_contract()
    document.update(overrides)
    path = directory / "contract.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def install_fixtures(workspace: Path) -> None:
    init_workspace(workspace)
    pairs = (
        (CONTRACT_PATH, workspace / "contracts" / "dimension_member.json"),
        (FIXTURES / "events" / "change_events.jsonl", workspace / "data/events/change_events.jsonl"),
        (FIXTURES / "dimension" / "member_history.csv", workspace / "data/dimension/member_history.csv"),
        (FIXTURES / "dimension" / "member_current.csv", workspace / "data/dimension/member_current.csv"),
    )
    for source, target in pairs:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def fixture_run(workspace: Path, reports: bool = False):
    install_fixtures(workspace)
    result = run_pipeline(workspace, now=NOW)
    if reports:
        write_reports(workspace, result)
    return result


def write_events(path: Path, events: list[dict[str, Any]]) -> Path:
    lines = [json.dumps(event, separators=(",", ":"), sort_keys=True) for event in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def event(
    event_id: str,
    key: str,
    at: str,
    sequence: int,
    segment: str = "Bronze",
    region: str = "APAC",
    status: str = "Active",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "member_id": key,
        "effective_at": at,
        "sequence": sequence,
        "payload": {"segment": segment, "region": region, "status": status},
    }


def expected_summary() -> dict[str, Any]:
    path = FIXTURES / "expected" / "demo_summary.json"
    return json.loads(path.read_text(encoding="utf-8"))
