"""Event ingestion and deterministic replay into expected SCD2 versions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Contract
from .io_utils import read_jsonl
from .model import EventRecord, ExpectedVersion, Finding, IngestResult
from .normalize import deduplicate, parse_event
from .util import canonical_json


def ingest_and_replay(contract: Contract, path: Path) -> IngestResult:
    raw_lines, _ = read_jsonl(path)
    parsed = [parse_event(contract, raw) for raw in raw_lines]
    events, active, findings, stats = deduplicate(contract, parsed)
    versions, replay_findings, replay_stats = _replay(contract, active)
    findings.extend(replay_findings)
    stats.update(replay_stats)
    stats["event_findings"] = len(findings)
    return IngestResult(
        events=sorted(events, key=lambda event: event.raw_line),
        expected_versions=versions,
        findings=findings,
        stats=stats,
    )


def _replay(
    contract: Contract, active: list[EventRecord]
) -> tuple[list[ExpectedVersion], list[Finding], dict[str, int]]:
    ordered = sorted(
        active,
        key=lambda event: (
            event.natural_key,
            event.effective_at,
            event.sequence,
            event.event_id,
            event.raw_line,
        ),
    )
    versions: list[ExpectedVersion] = []
    findings: list[Finding] = []
    current_key: str | None = None
    groups: dict[str, list[EventRecord]] = {}
    for event in ordered:
        if event.natural_key != current_key:
            if current_key is not None:
                _finish_key(contract, current_key, groups, versions, findings)
            current_key = event.natural_key
            groups = {}
        groups.setdefault(event.effective_at, []).append(event)
    if current_key is not None:
        _finish_key(contract, current_key, groups, versions, findings)

    stats = {
        "applied_events": sum(1 for event in active if event.status == "applied"),
        "noop_events": sum(1 for event in active if event.status == "noop"),
        "superseded_same_time": sum(
            1 for event in active if event.status == "superseded_same_time"
        ),
        "same_time_conflict_groups": sum(
            1 for finding in findings if finding.code == "EVT_SAME_TIME_CONFLICT"
        ),
        "expected_versions": len(versions),
    }
    return versions, findings, stats


def _finish_key(
    contract: Contract,
    natural_key: str,
    groups: dict[str, list[EventRecord]],
    versions: list[ExpectedVersion],
    findings: list[Finding],
) -> None:
    state: dict[str, Any] | None = None
    key_versions: list[ExpectedVersion] = []
    for effective_at in sorted(groups):
        group = groups[effective_at]
        prior = dict(state) if state is not None else None
        merged = (
            dict(state)
            if state is not None
            else {name: None for name in contract.tracked_attributes}
        )
        conflicts = _same_time_conflicts(contract, group)
        for event in group:
            for name in contract.tracked_attributes:
                if name in event.payload:
                    merged[name] = event.payload[name]
        if conflicts:
            findings.append(
                Finding(
                    code="EVT_SAME_TIME_CONFLICT",
                    severity="medium",
                    phase="event",
                    natural_key=natural_key,
                    start=effective_at,
                    message=(
                        f"same-time events disagree for {natural_key} at "
                        f"{effective_at}; stable ordering selected the final state"
                    ),
                    details={
                        "effective_at": effective_at,
                        "event_ids": [event.event_id for event in group],
                        "conflicting_attributes": conflicts,
                        "selected_event_id": group[-1].event_id,
                    },
                )
            )
        no_change = prior is not None and all(
            merged[name] == prior.get(name) for name in contract.tracked_attributes
        )
        if no_change:
            for event in group:
                event.status = "noop"
            state = merged
            continue
        group[-1].status = "applied"
        for event in group[:-1]:
            event.status = "superseded_same_time"
        key_versions.append(
            ExpectedVersion(
                natural_key=natural_key,
                version_seq=len(key_versions) + 1,
                attrs={name: merged[name] for name in contract.tracked_attributes},
                valid_from=effective_at,
                valid_to=None,
                source_event_ids=tuple(event.event_id for event in group),
            )
        )
        state = merged
    for index, version in enumerate(key_versions):
        version.valid_to = (
            key_versions[index + 1].valid_from if index + 1 < len(key_versions) else None
        )
    versions.extend(key_versions)


def _same_time_conflicts(
    contract: Contract, group: list[EventRecord]
) -> dict[str, Any]:
    conflicts: dict[str, Any] = {}
    for name in contract.tracked_attributes:
        seen: list[dict[str, Any]] = []
        canonical_seen: set[str] = set()
        for event in group:
            if name not in event.payload:
                continue
            value = event.payload[name]
            canonical = canonical_json(value)
            if canonical not in canonical_seen:
                canonical_seen.add(canonical)
                seen.append({"line": event.raw_line, "value": value})
        if len(seen) > 1:
            conflicts[name] = seen
    return conflicts
