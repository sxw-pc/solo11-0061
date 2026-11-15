"""Typed records shared by ingestion, diagnosis, planning and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EventRecord:
    event_id: str
    natural_key: str
    effective_at: str
    sequence: int
    payload: dict[str, Any]
    payload_json: str
    raw_line: int
    raw_sha256: str
    status: str = "ingested"
    raw_text: str = ""
    transaction_id: str | None = None
    transaction_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "natural_key": self.natural_key,
            "effective_at": self.effective_at,
            "sequence": self.sequence,
            "payload": dict(sorted(self.payload.items())),
            "payload_json": self.payload_json,
            "raw_line": self.raw_line,
            "raw_sha256": self.raw_sha256,
            "status": self.status,
            "transaction_id": self.transaction_id,
            "transaction_seq": self.transaction_seq,
        }


@dataclass
class ExpectedVersion:
    natural_key: str
    version_seq: int
    attrs: dict[str, Any]
    valid_from: str
    valid_to: str | None
    source_event_ids: tuple[str, ...] = ()
    source_transaction_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "natural_key": self.natural_key,
            "version_seq": self.version_seq,
            "attributes": dict(sorted(self.attrs.items())),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "source_event_ids": list(self.source_event_ids),
            "source_transaction_ids": list(self.source_transaction_ids),
        }


@dataclass
class ActualVersion:
    row_no: int
    member_sk: str
    natural_key: str
    attrs: dict[str, Any]
    valid_from: str
    valid_to: str | None
    is_current: bool
    source: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_no": self.row_no,
            "member_sk": self.member_sk,
            "natural_key": self.natural_key,
            "attributes": dict(sorted(self.attrs.items())),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "is_current": self.is_current,
            "source": self.source,
            "updated_at": self.updated_at,
        }


@dataclass
class CurrentRow:
    row_no: int
    natural_key: str
    attrs: dict[str, Any]
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_no": self.row_no,
            "natural_key": self.natural_key,
            "attributes": dict(sorted(self.attrs.items())),
            "updated_at": self.updated_at,
        }


@dataclass
class TransactionRecord:
    transaction_id: str
    effective_at: str
    member_event_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    status: str
    quarantine_reason: str | None = None
    order_classification: str | None = None
    manifest_line: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "effective_at": self.effective_at,
            "member_event_ids": list(self.member_event_ids),
            "depends_on": list(self.depends_on),
            "status": self.status,
            "quarantine_reason": self.quarantine_reason,
            "order_classification": self.order_classification,
            "manifest_line": self.manifest_line,
            "details": self.details,
        }


@dataclass
class TransactionEffect:
    """A committed transaction folded into per-member replay units."""

    transaction_id: str
    effective_at: str
    order_index: int
    key_events: dict[str, list[EventRecord]]
    key_payloads: dict[str, dict[str, Any]]


@dataclass
class TransactionPreparation:
    records: list[TransactionRecord]
    effects: list[TransactionEffect]
    findings: list[Finding]
    stats: dict[str, int]


@dataclass
class Finding:
    code: str
    severity: str
    phase: str
    message: str
    natural_key: str = ""
    start: str | None = None
    end: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    finding_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "code": self.code,
            "severity": self.severity,
            "phase": self.phase,
            "natural_key": self.natural_key,
            "interval": {"from": self.start, "to": self.end},
            "message": self.message,
            "details": self.details,
        }


@dataclass
class AsOfCheck:
    natural_key: str
    at: str
    status: str
    expected_version_seq: int | None = None
    actual_member_sks: tuple[str, ...] = ()
    expected_attrs: dict[str, Any] | None = None
    actual_attrs: dict[str, Any] | None = None
    differences: dict[str, Any] = field(default_factory=dict)
    check_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "natural_key": self.natural_key,
            "at": self.at,
            "status": self.status,
            "expected_version_seq": self.expected_version_seq,
            "actual_member_sks": list(self.actual_member_sks),
            "expected_attributes": self.expected_attrs,
            "actual_attributes": self.actual_attrs,
            "differences": self.differences,
        }


@dataclass
class PlanAction:
    kind: str
    natural_key: str
    start: str | None
    end: str | None
    finding_ids: tuple[str, ...]
    rationale: str
    risk: str
    review_required: bool
    details: dict[str, Any] = field(default_factory=dict)
    action_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "natural_key": self.natural_key,
            "interval": {"from": self.start, "to": self.end},
            "finding_ids": list(self.finding_ids),
            "rationale": self.rationale,
            "risk": self.risk,
            "review_required": self.review_required,
            "details": self.details,
        }


@dataclass
class IngestResult:
    events: list[EventRecord]
    expected_versions: list[ExpectedVersion]
    findings: list[Finding]
    stats: dict[str, int]
    transactions: list[TransactionRecord] = field(default_factory=list)
