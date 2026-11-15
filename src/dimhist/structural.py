"""Row-level SCD2 structural checks and dry-run row repair operations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from .config import Contract
from .model import ActualVersion, Finding
from .util import DimhistError, canonical_json, normalize_time


def diagnose_rows(rows: list[ActualVersion]) -> list[Finding]:
    """Run every structural check on an in-memory collection of actual rows."""
    by_key: dict[str, list[ActualVersion]] = defaultdict(list)
    for row in rows:
        by_key[row.natural_key].append(row)
    findings: list[Finding] = []
    for key in sorted(by_key):
        ordered = sorted(
            by_key[key],
            key=lambda item: (item.valid_from, item.member_sk, item.row_no),
        )
        findings.extend(diagnose_overlaps(key, ordered))
        findings.extend(diagnose_gaps(key, ordered))
        findings.extend(diagnose_duplicate_versions(key, ordered))
        findings.extend(diagnose_current_state(key, ordered))
    return findings


def diagnose_overlaps(key: str, rows: list[ActualVersion]) -> list[Finding]:
    findings: list[Finding] = []
    valid_rows = [
        row for row in rows if row.valid_to is None or row.valid_from < row.valid_to
    ]
    for index, left in enumerate(valid_rows):
        for right in valid_rows[index + 1 :]:
            start = max(left.valid_from, right.valid_from)
            end = _min_end(left.valid_to, right.valid_to)
            if end is None or start < end:
                findings.append(
                    Finding(
                        code="DIM_OVERLAP",
                        severity="high",
                        phase="diagnose",
                        natural_key=key,
                        start=start,
                        end=end,
                        message=f"actual intervals overlap for {key}",
                        details={
                            "member_sks": sorted([left.member_sk, right.member_sk]),
                            "row_nos": sorted([left.row_no, right.row_no]),
                        },
                    )
                )
    return findings


def diagnose_gaps(key: str, rows: list[ActualVersion]) -> list[Finding]:
    valid_rows = sorted(
        [
            row
            for row in rows
            if row.valid_to is None or row.valid_from < row.valid_to
        ],
        key=lambda item: (item.valid_from, item.valid_to is None, item.row_no),
    )
    findings: list[Finding] = []
    if not valid_rows:
        return findings
    coverage_start = valid_rows[0].valid_from
    coverage_end = valid_rows[0].valid_to
    for row in valid_rows[1:]:
        if coverage_end is not None and row.valid_from > coverage_end:
            findings.append(
                Finding(
                    code="DIM_GAP",
                    severity="medium",
                    phase="diagnose",
                    natural_key=key,
                    start=coverage_end,
                    end=row.valid_from,
                    message=f"actual history has a gap for {key}",
                    details={
                        "before_row_no": _row_ending_at(valid_rows, coverage_end),
                        "after_row_no": row.row_no,
                        "coverage_from": coverage_start,
                    },
                )
            )
        coverage_end = _max_end(coverage_end, row.valid_to)
        if coverage_end is None:
            break
    return findings


def diagnose_duplicate_versions(key: str, rows: list[ActualVersion]) -> list[Finding]:
    groups: dict[tuple[Any, ...], list[ActualVersion]] = defaultdict(list)
    for row in rows:
        groups[(row.valid_from, tuple(sorted(row.attrs.items())))].append(row)
    findings: list[Finding] = []
    for (valid_from, _), members in sorted(groups.items()):
        if len(members) > 1:
            findings.append(
                Finding(
                    code="DIM_DUPLICATE_VERSION",
                    severity="medium",
                    phase="diagnose",
                    natural_key=key,
                    start=valid_from,
                    end=None,
                    message=f"duplicate actual version detected for {key}",
                    details={
                        "member_sks": sorted(row.member_sk for row in members),
                        "row_nos": sorted(row.row_no for row in members),
                        "attributes": dict(members[0].attrs),
                    },
                )
            )
    return findings


def diagnose_current_state(key: str, rows: list[ActualVersion]) -> list[Finding]:
    findings: list[Finding] = []
    current_rows = [row for row in rows if row.is_current]
    if len(current_rows) > 1:
        findings.append(
            Finding(
                code="DIM_MULTI_CURRENT",
                severity="high",
                phase="diagnose",
                natural_key=key,
                message=f"multiple current rows declared for {key}",
                details={
                    "member_sks": sorted(row.member_sk for row in current_rows),
                    "row_nos": sorted(row.row_no for row in current_rows),
                },
            )
        )
    open_rows = [row for row in rows if row.valid_to is None]
    if rows and not open_rows:
        findings.append(
            Finding(
                code="DIM_NO_CURRENT",
                severity="medium",
                phase="diagnose",
                natural_key=key,
                message=f"actual history has no open current row for {key}",
                details={
                    "last_valid_to": max(
                        (row.valid_to for row in rows if row.valid_to is not None),
                        default=None,
                    )
                },
            )
        )
    for row in rows:
        if row.is_current != (row.valid_to is None):
            findings.append(
                Finding(
                    code="DIM_CURRENT_FLAG",
                    severity="low",
                    phase="diagnose",
                    natural_key=key,
                    start=row.valid_from,
                    end=row.valid_to,
                    message=f"is_current disagrees with valid_to on row {row.row_no}",
                    details={
                        "member_sk": row.member_sk,
                        "row_no": row.row_no,
                        "is_current": row.is_current,
                        "valid_to": row.valid_to,
                    },
                )
            )
    return findings


def split_row(
    row: ActualVersion,
    at: Any,
    member_sk: str | None,
    new_row_no: int,
    action_id: str,
) -> tuple[ActualVersion, ActualVersion]:
    """Split one row into [valid_from, at) and [at, valid_to) on copies."""
    moment = normalize_time(at, code="SIM_006", field=f"step {action_id} at")
    if not (row.valid_from < moment and (row.valid_to is None or moment < row.valid_to)):
        raise DimhistError(
            "SIM_006",
            f"step {action_id} split point {moment} is not inside "
            f"[{row.valid_from}, {row.valid_to})",
        )
    if member_sk is not None and (not isinstance(member_sk, str) or not member_sk.strip()):
        raise DimhistError("SIM_006", f"step {action_id} member_sk must be non-empty")
    left = replace(row, valid_to=moment, is_current=False)
    right = replace(
        row, row_no=new_row_no, valid_from=moment,
        member_sk=member_sk or f"{row.member_sk}~{action_id}",
    )
    return left, right


def updated_row(
    contract: Contract,
    row: ActualVersion,
    params: dict[str, Any],
    action_id: str,
) -> ActualVersion:
    """Return a copy of row with explicit attribute and interval adjustments."""
    changes = params.get("set", {})
    if not isinstance(changes, dict):
        raise DimhistError("SIM_006", f"step {action_id} set must be an object")
    attrs = dict(row.attrs)
    for name, value in changes.items():
        if name not in contract.tracked_attributes:
            raise DimhistError(
                "SIM_006", f"step {action_id} cannot set untracked attribute {name!r}"
            )
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise DimhistError(
                "SIM_006", f"step {action_id} attribute {name!r} must be a JSON scalar"
            )
        attrs[name] = value
    valid_from, valid_to, is_current = row.valid_from, row.valid_to, row.is_current
    if "valid_from" in params:
        valid_from = normalize_time(params["valid_from"], code="SIM_006", field="valid_from")
    if "valid_to" in params:
        raw_to = params["valid_to"]
        valid_to = None if raw_to is None else normalize_time(
            raw_to, code="SIM_006", field="valid_to"
        )
    if "is_current" in params:
        if not isinstance(params["is_current"], bool):
            raise DimhistError("SIM_006", f"step {action_id} is_current must be boolean")
        is_current = params["is_current"]
    if not changes and not {"valid_from", "valid_to", "is_current"} & set(params):
        raise DimhistError("SIM_006", f"step {action_id} changes nothing")
    if valid_to is not None and valid_to <= valid_from:
        raise DimhistError(
            "SIM_006", f"step {action_id} would create a non-positive interval"
        )
    return replace(
        row, attrs=attrs,
        valid_from=valid_from, valid_to=valid_to, is_current=is_current,
    )


def merged_row(rows: list[ActualVersion], keeper: ActualVersion, action_id: str) -> ActualVersion:
    """Collapse duplicate version rows into the keeper's content on a copy."""
    signature = _duplicate_signature(rows[0])
    if any(_duplicate_signature(row) != signature for row in rows[1:]):
        raise DimhistError(
            "SIM_006", f"step {action_id} targets are not duplicate versions"
        )
    if keeper.valid_to is not None and keeper.valid_to <= keeper.valid_from:
        raise DimhistError("SIM_006", f"step {action_id} keeps a non-positive interval")
    return replace(keeper)


def _duplicate_signature(row: ActualVersion) -> str:
    return canonical_json([row.natural_key, row.valid_from, dict(sorted(row.attrs.items()))])


def _min_end(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _max_end(left: str | None, right: str | None) -> str | None:
    if left is None or right is None:
        return None
    return max(left, right)


def _row_ending_at(rows: list[ActualVersion], end: str) -> int | None:
    for row in rows:
        if row.valid_to == end:
            return row.row_no
    return None
