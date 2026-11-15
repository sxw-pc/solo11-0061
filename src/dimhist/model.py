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
        }


@dataclass
class ExpectedVersion:
    natural_key: str
    version_seq: int
    attrs: dict[str, Any]
    valid_from: str
    valid_to: str | None
    source_event_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "natural_key": self.natural_key,
            "version_seq": self.version_seq,
            "attributes": dict(sorted(self.attrs.items())),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "source_event_ids": list(self.source_event_ids),
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


@dataclass
class DerivedFinding:
    """A finding proven to vanish once untrustworthy rows are removed."""

    finding_id: str
    code: str
    method: str
    removed_rows: tuple[int, ...]
    remaining_rows: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "code": self.code,
            "proof": {
                "method": self.method,
                "removed_rows": list(self.removed_rows),
                "remaining_rows": list(self.remaining_rows),
                "explanation": (
                    "violation absent after removing the untrustworthy row(s); "
                    "kept as a derived symptom, not deleted"
                ),
            },
        }


@dataclass
class TriageGroup:
    """One root cause plus the findings proven to be its derived symptoms."""

    group_id: str
    root_finding_id: str
    root_code: str
    natural_key: str
    untrustworthy_rows: tuple[int, ...]
    derived: tuple[DerivedFinding, ...]
    proof_rows: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "root_finding_id": self.root_finding_id,
            "root_code": self.root_code,
            "natural_key": self.natural_key,
            "untrustworthy_rows": list(self.untrustworthy_rows),
            "derived_findings": [item.to_dict() for item in self.derived],
            "proof_rows": list(self.proof_rows),
        }


@dataclass
class TriageView:
    """Review grouping over the raw findings; never rewrites history."""

    groups: tuple[TriageGroup, ...]
    unattributed: tuple[str, ...]
    findings_total: int

    def to_dict(self) -> dict[str, Any]:
        derived_count = sum(len(group.derived) for group in self.groups)
        root_count = len(self.groups)
        return {
            "kind": "review_grouping",
            "read_only": True,
            "mutates_history": False,
            "auto_repair": False,
            "note": (
                "Review grouping only: raw findings, counts and plan references "
                "are unchanged; no history is rewritten and no repair is executed."
            ),
            "findings_total": self.findings_total,
            "root_cause_count": root_count,
            "derived_count": derived_count,
            "unattributed_count": len(self.unattributed),
            "partition_complete": (
                root_count + derived_count + len(self.unattributed)
                == self.findings_total
            ),
            "groups": [group.to_dict() for group in self.groups],
            "unattributed_findings": list(self.unattributed),
        }
