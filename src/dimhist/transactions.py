"""Atomic cross-member transactions: commit manifests, quarantine, ordering.

A local event may carry ``transaction_id`` and ``transaction_seq`` (its order
inside the transaction). A separate commit manifest declares, per transaction,
the member ``event_id`` set, the business ``effective_at`` moment and optional
``depends_on`` dependencies. Only complete and conflict-free transactions enter
the expected history as a whole; any integrity failure quarantines the entire
group and never a valid subset of it. Feasible-order analysis per business
moment lives in ``ordering.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io_utils import read_jsonl
from .model import (
    EventRecord,
    Finding,
    TransactionEffect,
    TransactionPreparation,
    TransactionRecord,
)
from .ordering import TxOrderWork, resolve_ordering
from .util import DimhistError, normalize_time

QUARANTINE_PRIORITY = (
    "member_conflict",
    "missing_member",
    "undeclared_member",
    "missing_seq",
    "duplicate_seq",
)


@dataclass(frozen=True)
class CommitDeclaration:
    transaction_id: str
    effective_at: str
    members: tuple[str, ...]
    depends_on: tuple[str, ...]
    manifest_line: int


def load_commit_manifest(path: Path) -> list[CommitDeclaration]:
    raw_lines, _ = read_jsonl(path)
    declarations: list[CommitDeclaration] = []
    seen: set[str] = set()
    for raw in raw_lines:
        try:
            document = json.loads(raw.text)
        except json.JSONDecodeError as exc:
            raise DimhistError(
                "INP_002",
                f"commit manifest line {raw.line_no}: invalid JSON ({exc.msg})",
            ) from exc
        declarations.append(_parse_declaration(document, raw.line_no, seen))
    return declarations


def prepare_transactions(
    parsed: list[EventRecord],
    active: list[EventRecord],
    declarations: list[CommitDeclaration],
) -> TransactionPreparation:
    by_id = {event.event_id: event for event in active}
    conflict_ids = {
        event.event_id for event in parsed if event.status == "duplicate_conflict"
    }
    events_by_tx: dict[str, list[EventRecord]] = {}
    for event in active:
        if event.transaction_id is not None:
            events_by_tx.setdefault(event.transaction_id, []).append(event)

    declared = {decl.transaction_id: decl for decl in declarations}
    works: dict[str, TxOrderWork] = {}
    quarantined: dict[str, tuple[str, dict[str, Any]]] = {}

    for decl in declarations:
        reasons = _validate_declaration(decl, by_id, conflict_ids, events_by_tx)
        if reasons:
            primary = next(r for r in QUARANTINE_PRIORITY if r in reasons)
            quarantined[decl.transaction_id] = (primary, reasons[primary])
        else:
            works[decl.transaction_id] = _build_work(decl, by_id)

    for tx_id in sorted(set(events_by_tx) - set(declared)):
        quarantined[tx_id] = ("missing_commit", {})

    resolve_ordering(declared, works, quarantined)

    records: list[TransactionRecord] = []
    findings: list[Finding] = []
    for tx_id, (reason, details) in quarantined.items():
        decl = declared.get(tx_id)
        if decl is not None:
            moment, line = decl.effective_at, decl.manifest_line
            members, deps = decl.members, decl.depends_on
        else:
            carrier = events_by_tx[tx_id]
            moment = min(event.effective_at for event in carrier)
            line = 0
            members = tuple(sorted(event.event_id for event in carrier))
            deps = ()
        records.append(
            TransactionRecord(
                transaction_id=tx_id,
                effective_at=moment,
                member_event_ids=members,
                depends_on=deps,
                status="quarantined",
                quarantine_reason=reason,
                manifest_line=line,
                details=details,
            )
        )
        for event in events_by_tx.get(tx_id, []):
            event.status = "quarantined"
        findings.append(_quarantine_finding(tx_id, moment, reason, details))

    effects: list[TransactionEffect] = []
    for tx_id, work in works.items():
        decl = work.declaration
        records.append(
            TransactionRecord(
                transaction_id=tx_id,
                effective_at=decl.effective_at,
                member_event_ids=decl.members,
                depends_on=decl.depends_on,
                status="committed",
                order_classification=work.classification,
                manifest_line=decl.manifest_line,
                details={"fold_order": work.order_index},
            )
        )
        effects.append(
            TransactionEffect(
                transaction_id=tx_id,
                effective_at=decl.effective_at,
                order_index=work.order_index,
                key_events=work.key_events,
                key_payloads=work.key_payloads,
            )
        )
    effects.sort(key=lambda effect: (effect.effective_at, effect.order_index))
    records.sort(key=lambda record: record.transaction_id)
    stats = {
        "transactions_declared": len(declarations),
        "transactions_committed": len(works),
        "transactions_quarantined": len(quarantined),
        "transaction_events": sum(
            1 for event in active if event.transaction_id is not None
        ),
    }
    return TransactionPreparation(
        records=records, effects=effects, findings=findings, stats=stats
    )


def _parse_declaration(
    document: Any, line_no: int, seen: set[str]
) -> CommitDeclaration:
    if not isinstance(document, dict):
        raise DimhistError(
            "INP_003", f"commit manifest line {line_no}: entry must be an object"
        )
    tx_id = _manifest_text(document, "transaction_id", line_no)
    if tx_id in seen:
        raise DimhistError(
            "INP_003",
            f"commit manifest line {line_no}: duplicate transaction_id {tx_id}",
        )
    seen.add(tx_id)
    effective_at = normalize_time(
        document.get("effective_at"),
        code="INP_004",
        field=f"commit manifest line {line_no}: effective_at",
    )
    members = _manifest_text_list(document, "members", line_no, required=True)
    depends_on = _manifest_text_list(document, "depends_on", line_no, required=False)
    return CommitDeclaration(
        transaction_id=tx_id,
        effective_at=effective_at,
        members=tuple(members),
        depends_on=tuple(depends_on),
        manifest_line=line_no,
    )


def _validate_declaration(
    decl: CommitDeclaration,
    by_id: dict[str, EventRecord],
    conflict_ids: set[str],
    events_by_tx: dict[str, list[EventRecord]],
) -> dict[str, dict[str, Any]]:
    reasons: dict[str, dict[str, Any]] = {}
    member_set = set(decl.members)
    missing = [event_id for event_id in decl.members if event_id not in by_id]
    mismatched = [
        event_id
        for event_id in decl.members
        if event_id in by_id and by_id[event_id].transaction_id != decl.transaction_id
    ]
    conflicted = sorted(member_set & conflict_ids)
    undeclared = sorted(
        event.event_id
        for event in events_by_tx.get(decl.transaction_id, [])
        if event.event_id not in member_set
    )
    present = [
        by_id[event_id]
        for event_id in decl.members
        if event_id in by_id
        and by_id[event_id].transaction_id == decl.transaction_id
    ]
    missing_seq = sorted(
        event.event_id for event in present if event.transaction_seq is None
    )
    seqs = [
        event.transaction_seq for event in present if event.transaction_seq is not None
    ]
    duplicate_seqs = sorted({seq for seq in seqs if seqs.count(seq) > 1})
    if conflicted:
        reasons["member_conflict"] = {"conflicting_event_ids": conflicted}
    if missing or mismatched:
        reasons["missing_member"] = {
            "missing_event_ids": missing,
            "mismatched_event_ids": mismatched,
        }
    if undeclared:
        reasons["undeclared_member"] = {"undeclared_event_ids": undeclared}
    if missing_seq:
        reasons["missing_seq"] = {"event_ids": missing_seq}
    if duplicate_seqs:
        reasons["duplicate_seq"] = {"sequences": duplicate_seqs}
    return reasons


def _build_work(
    decl: CommitDeclaration, by_id: dict[str, EventRecord]
) -> TxOrderWork:
    key_events: dict[str, list[EventRecord]] = {}
    for event_id in decl.members:
        event = by_id[event_id]
        key_events.setdefault(event.natural_key, []).append(event)
    key_payloads: dict[str, dict[str, Any]] = {}
    for key, events in key_events.items():
        events.sort(key=lambda event: (event.transaction_seq, event.event_id))
        folded: dict[str, Any] = {}
        for event in events:
            folded.update(event.payload)
        key_payloads[key] = folded
    return TxOrderWork(
        declaration=decl, key_events=key_events, key_payloads=key_payloads
    )


def _quarantine_finding(
    tx_id: str, moment: str, reason: str, details: dict[str, Any]
) -> Finding:
    return Finding(
        code="EVT_TX_QUARANTINED",
        severity="high",
        phase="event",
        natural_key="",
        start=moment,
        message=(
            f"transaction {tx_id} was quarantined ({reason}); "
            "no member events were replayed"
        ),
        details={"transaction_id": tx_id, "reason": reason, **details},
    )


def _manifest_text(document: dict[str, Any], field: str, line_no: int) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DimhistError(
            "INP_003",
            f"commit manifest line {line_no}: {field} must be a non-empty string",
        )
    return value.strip()


def _manifest_text_list(
    document: dict[str, Any], field: str, line_no: int, required: bool
) -> list[str]:
    value = document.get(field)
    if value is None and not required:
        return []
    if not isinstance(value, list) or not value:
        raise DimhistError(
            "INP_003",
            f"commit manifest line {line_no}: {field} must be a non-empty list",
        )
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DimhistError(
                "INP_003",
                f"commit manifest line {line_no}: {field} entries must be "
                "non-empty strings",
            )
        items.append(item.strip())
    if len(set(items)) != len(items):
        raise DimhistError(
            "INP_003",
            f"commit manifest line {line_no}: {field} contains duplicates",
        )
    return items
