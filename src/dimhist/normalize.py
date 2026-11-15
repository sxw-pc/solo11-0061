"""Parse JSONL event rows and apply stable event-ID de-duplication."""

from __future__ import annotations

import json
from typing import Any

from .config import Contract
from .io_utils import RawLine
from .model import EventRecord, Finding
from .util import (
    DimhistError,
    canonical_json,
    normalize_time,
    require_positive_sequence,
)


def parse_event(contract: Contract, raw: RawLine) -> EventRecord:
    try:
        document = json.loads(raw.text)
    except json.JSONDecodeError as exc:
        raise DimhistError(
            "INP_002", f"event log line {raw.line_no}: invalid JSON ({exc.msg})"
        ) from exc
    if not isinstance(document, dict):
        raise DimhistError(
            "INP_003", f"event log line {raw.line_no}: event must be an object"
        )
    event_id = _required_text(document, contract.event_id_field, raw.line_no)
    natural_key = _required_text(document, contract.natural_key, raw.line_no)
    effective_raw = _required_text(
        document, contract.effective_at_field, raw.line_no
    )
    effective_at = normalize_time(
        effective_raw,
        code="INP_004",
        field=f"event log line {raw.line_no}: {contract.effective_at_field}",
    )
    if contract.sequence_field not in document:
        raise DimhistError(
            "INP_003", f"event log line {raw.line_no}: missing {contract.sequence_field}"
        )
    try:
        sequence = require_positive_sequence(document[contract.sequence_field])
    except DimhistError as exc:
        raise DimhistError(
            "INP_003",
            f"event log line {raw.line_no}: {contract.sequence_field} must be a "
            "non-negative integer",
        ) from exc
    payload_raw = document.get(contract.payload_field)
    if not isinstance(payload_raw, dict) or not payload_raw:
        raise DimhistError(
            "INP_003",
            f"event log line {raw.line_no}: {contract.payload_field} must be a "
            "non-empty object",
        )
    unknown = sorted(set(payload_raw) - set(contract.tracked_attributes))
    if unknown:
        raise DimhistError(
            "INP_003",
            f"event log line {raw.line_no}: unknown payload field(s): "
            + ", ".join(unknown),
        )
    payload = {
        name: payload_raw[name]
        for name in contract.tracked_attributes
        if name in payload_raw
    }
    transaction_id = _optional_transaction_id(document, raw.line_no)
    transaction_seq = _optional_transaction_seq(document, raw.line_no)
    if transaction_seq is not None and transaction_id is None:
        raise DimhistError(
            "INP_003",
            f"event log line {raw.line_no}: transaction_seq requires transaction_id",
        )
    return EventRecord(
        event_id=event_id,
        natural_key=natural_key,
        effective_at=effective_at,
        sequence=sequence,
        payload=payload,
        payload_json=canonical_json(payload),
        raw_line=raw.line_no,
        raw_sha256=raw.sha256,
        raw_text=raw.text,
        transaction_id=transaction_id,
        transaction_seq=transaction_seq,
    )


def deduplicate(
    contract: Contract, parsed: list[EventRecord]
) -> tuple[list[EventRecord], list[EventRecord], list[Finding], dict[str, int]]:
    first_by_id: dict[str, EventRecord] = {}
    active: list[EventRecord] = []
    findings: list[Finding] = []
    exact = 0
    conflicting = 0
    for event in parsed:
        first = first_by_id.get(event.event_id)
        if first is None:
            first_by_id[event.event_id] = event
            active.append(event)
            continue
        if _identity(event) == _identity(first):
            event.status = "duplicate_exact"
            exact += 1
            continue
        event.status = "duplicate_conflict"
        conflicting += 1
        findings.append(
            Finding(
                code="EVT_DUP_CONFLICT",
                severity="high",
                phase="event",
                natural_key=event.natural_key,
                start=event.effective_at,
                message=(
                    f"event_id {event.event_id} appears with conflicting content; "
                    f"line {first.raw_line} is retained"
                ),
                details={
                    "event_id": event.event_id,
                    "retained_line": first.raw_line,
                    "duplicate_line": event.raw_line,
                    "differing_fields": _differing_fields(first, event),
                },
            )
        )
    stats = {
        "event_rows": len(parsed),
        "unique_event_ids": len(first_by_id),
        "duplicate_exact": exact,
        "duplicate_conflict": conflicting,
    }
    return parsed, active, findings, stats


def _identity(event: EventRecord) -> tuple[Any, ...]:
    return (
        event.natural_key,
        event.effective_at,
        event.sequence,
        canonical_json(event.payload),
        event.transaction_id,
        event.transaction_seq,
    )


def _differing_fields(first: EventRecord, duplicate: EventRecord) -> list[str]:
    fields: list[str] = []
    if first.natural_key != duplicate.natural_key:
        fields.append("natural_key")
    if first.effective_at != duplicate.effective_at:
        fields.append("effective_at")
    if first.sequence != duplicate.sequence:
        fields.append("sequence")
    if first.transaction_id != duplicate.transaction_id:
        fields.append("transaction_id")
    if first.transaction_seq != duplicate.transaction_seq:
        fields.append("transaction_seq")
    names = sorted(set(first.payload) | set(duplicate.payload))
    fields.extend(
        f"payload.{name}"
        for name in names
        if canonical_json(first.payload.get(name))
        != canonical_json(duplicate.payload.get(name))
    )
    return fields


def _required_text(document: dict[str, Any], field: str, line_no: int) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DimhistError(
            "INP_003", f"event log line {line_no}: {field} must be a non-empty string"
        )
    return value.strip()


def _optional_transaction_id(document: dict[str, Any], line_no: int) -> str | None:
    value = document.get("transaction_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DimhistError(
            "INP_003",
            f"event log line {line_no}: transaction_id must be a non-empty string",
        )
    return value.strip()


def _optional_transaction_seq(document: dict[str, Any], line_no: int) -> int | None:
    if "transaction_seq" not in document or document["transaction_seq"] is None:
        return None
    try:
        return require_positive_sequence(document["transaction_seq"])
    except DimhistError as exc:
        raise DimhistError(
            "INP_003",
            f"event log line {line_no}: transaction_seq must be a non-negative "
            "integer",
        ) from exc
