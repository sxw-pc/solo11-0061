"""Event ingestion and deterministic replay into expected SCD2 versions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Contract
from .io_utils import read_jsonl
from .model import (
    EventRecord,
    ExpectedVersion,
    Finding,
    IngestResult,
    TransactionEffect,
)
from .normalize import deduplicate, parse_event
from .transactions import load_commit_manifest, prepare_transactions
from .util import canonical_json


def ingest_and_replay(
    contract: Contract, path: Path, transaction_path: Path | None = None
) -> IngestResult:
    raw_lines, _ = read_jsonl(path)
    parsed = [parse_event(contract, raw) for raw in raw_lines]
    events, active, findings, stats = deduplicate(contract, parsed)
    declarations = (
        load_commit_manifest(transaction_path)
        if transaction_path is not None
        else []
    )
    preparation = prepare_transactions(parsed, active, declarations)
    findings.extend(preparation.findings)
    stats.update(preparation.stats)
    versions, replay_findings, replay_stats = _replay(
        contract, active, preparation.effects
    )
    findings.extend(replay_findings)
    stats.update(replay_stats)
    stats["event_findings"] = len(findings)
    return IngestResult(
        events=sorted(events, key=lambda event: event.raw_line),
        expected_versions=versions,
        findings=findings,
        stats=stats,
        transactions=preparation.records,
    )


def _replay(
    contract: Contract,
    active: list[EventRecord],
    effects: list[TransactionEffect],
) -> tuple[list[ExpectedVersion], list[Finding], dict[str, int]]:
    groups: dict[str, dict[str, dict[str, list[Any]]]] = {}
    for event in active:
        if event.transaction_id is not None:
            continue
        moments = groups.setdefault(event.natural_key, {})
        moments.setdefault(event.effective_at, {"events": [], "txs": []})[
            "events"
        ].append(event)
    for effect in effects:
        for key in effect.key_events:
            moments = groups.setdefault(key, {})
            moments.setdefault(effect.effective_at, {"events": [], "txs": []})[
                "txs"
            ].append(effect)

    versions: list[ExpectedVersion] = []
    findings: list[Finding] = []
    for natural_key in sorted(groups):
        _finish_key(contract, natural_key, groups[natural_key], versions, findings)

    stats = {
        "applied_events": sum(1 for event in active if event.status == "applied"),
        "noop_events": sum(1 for event in active if event.status == "noop"),
        "superseded_same_time": sum(
            1 for event in active if event.status == "superseded_same_time"
        ),
        "quarantined_events": sum(
            1 for event in active if event.status == "quarantined"
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
    moments: dict[str, dict[str, list[Any]]],
    versions: list[ExpectedVersion],
    findings: list[Finding],
) -> None:
    state: dict[str, Any] | None = None
    key_versions: list[ExpectedVersion] = []
    for effective_at in sorted(moments):
        group = moments[effective_at]
        plain = sorted(
            group["events"],
            key=lambda event: (event.sequence, event.event_id, event.raw_line),
        )
        txs = sorted(group["txs"], key=lambda effect: effect.order_index)
        fold_events = plain + [
            member for tx in txs for member in tx.key_events[natural_key]
        ]
        prior = dict(state) if state is not None else None
        merged = (
            dict(state)
            if state is not None
            else {name: None for name in contract.tracked_attributes}
        )
        conflicts = _same_time_conflicts(contract, plain)
        for event in plain:
            for name in contract.tracked_attributes:
                if name in event.payload:
                    merged[name] = event.payload[name]
        for tx in txs:
            for name in contract.tracked_attributes:
                if name in tx.key_payloads[natural_key]:
                    merged[name] = tx.key_payloads[natural_key][name]
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
                        "event_ids": [event.event_id for event in plain],
                        "conflicting_attributes": conflicts,
                        "selected_event_id": plain[-1].event_id,
                    },
                )
            )
        no_change = prior is not None and all(
            merged[name] == prior.get(name) for name in contract.tracked_attributes
        )
        if no_change:
            for event in fold_events:
                event.status = "noop"
            state = merged
            continue
        fold_events[-1].status = "applied"
        for event in fold_events[:-1]:
            event.status = "superseded_same_time"
        key_versions.append(
            ExpectedVersion(
                natural_key=natural_key,
                version_seq=len(key_versions) + 1,
                attrs={name: merged[name] for name in contract.tracked_attributes},
                valid_from=effective_at,
                valid_to=None,
                source_event_ids=tuple(event.event_id for event in fold_events),
                source_transaction_ids=tuple(tx.transaction_id for tx in txs),
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
