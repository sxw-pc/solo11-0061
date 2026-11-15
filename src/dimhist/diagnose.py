"""Structural and semantic diagnostics for actual SCD2 dimension data."""

from __future__ import annotations

from collections import defaultdict

from .config import Contract
from .model import ActualVersion, CurrentRow, ExpectedVersion, Finding
from .structural import (
    diagnose_current_state,
    diagnose_duplicate_versions,
    diagnose_gaps,
    diagnose_overlaps,
)
from .util import canonical_json


def diagnose_actual(
    contract: Contract,
    expected: list[ExpectedVersion],
    actual: list[ActualVersion],
) -> list[Finding]:
    by_key_actual: dict[str, list[ActualVersion]] = defaultdict(list)
    by_key_expected: dict[str, list[ExpectedVersion]] = defaultdict(list)
    for version in actual:
        by_key_actual[version.natural_key].append(version)
    for version in expected:
        by_key_expected[version.natural_key].append(version)

    findings: list[Finding] = []
    for key in sorted(set(by_key_actual) | set(by_key_expected)):
        rows = sorted(
            by_key_actual.get(key, []),
            key=lambda item: (item.valid_from, item.member_sk, item.row_no),
        )
        expected_rows = sorted(
            by_key_expected.get(key, []),
            key=lambda item: (item.valid_from, item.version_seq),
        )
        findings.extend(diagnose_key(contract, key, expected_rows, rows))
    return findings


def diagnose_key(
    contract: Contract,
    key: str,
    expected_rows: list[ExpectedVersion],
    rows: list[ActualVersion],
) -> list[Finding]:
    """Run every per-key history diagnostic for one natural key.

    This is the scoped unit reused by the triage counterfactual proofs: it
    touches only the rows of ``key`` and never triggers event replay, as-of
    sampling or any other pipeline stage.
    """
    findings: list[Finding] = []
    findings.extend(diagnose_overlaps(key, rows))
    findings.extend(diagnose_gaps(key, rows))
    findings.extend(diagnose_duplicate_versions(key, rows))
    findings.extend(diagnose_current_state(key, rows))
    if not rows and expected_rows:
        findings.append(
            Finding(
                code="DIM_MISSING_EXPECTED",
                severity="high",
                phase="diagnose",
                natural_key=key,
                message=f"expected member {key} has no actual version rows",
                details={
                    "expected_versions": len(expected_rows),
                    "expected_from": expected_rows[0].valid_from,
                },
            )
        )
    if rows and not expected_rows:
        findings.append(
            Finding(
                code="DIM_ORPHAN_ACTUAL",
                severity="medium",
                phase="diagnose",
                natural_key=key,
                message=f"actual member {key} has no matching change events",
                details={
                    "actual_rows": len(rows),
                    "member_sks": [row.member_sk for row in rows],
                    "row_nos": [row.row_no for row in rows],
                },
            )
        )
    findings.extend(_diagnose_attribute_drift(contract, key, expected_rows, rows))
    return findings


def diagnose_current_snapshot(
    contract: Contract,
    actual: list[ActualVersion],
    current_rows: list[CurrentRow],
) -> list[Finding]:
    actual_open: dict[str, list[ActualVersion]] = defaultdict(list)
    snapshot: dict[str, list[CurrentRow]] = defaultdict(list)
    for row in actual:
        if row.valid_to is None:
            actual_open[row.natural_key].append(row)
    for row in current_rows:
        snapshot[row.natural_key].append(row)
    findings: list[Finding] = []
    for key in sorted(set(actual_open) | set(snapshot)):
        finding = snapshot_drift_for_key(
            contract, key, actual_open.get(key, []), snapshot.get(key, [])
        )
        if finding is not None:
            findings.append(finding)
    return findings


def snapshot_drift_for_key(
    contract: Contract,
    key: str,
    open_rows: list[ActualVersion],
    snapshot_rows: list[CurrentRow],
) -> Finding | None:
    """Compare open actual rows of one key with its current snapshot rows."""
    actual_signature = sorted(
        canonical_json(
            {name: row.attrs[name] for name in contract.tracked_attributes}
        )
        for row in open_rows
    )
    snapshot_signature = sorted(
        canonical_json(
            {name: row.attrs[name] for name in contract.tracked_attributes}
        )
        for row in snapshot_rows
    )
    if actual_signature == snapshot_signature:
        return None
    return Finding(
        code="DIM_CURRENT_SNAPSHOT_DRIFT",
        severity="medium",
        phase="diagnose",
        natural_key=key,
        message=f"current snapshot differs from open actual rows for {key}",
        details={
            "open_actual_rows": [row.member_sk for row in open_rows],
            "open_actual_row_nos": [row.row_no for row in open_rows],
            "snapshot_rows": [row.row_no for row in snapshot_rows],
        },
    )


def _diagnose_attribute_drift(
    contract: Contract,
    key: str,
    expected_rows: list[ExpectedVersion],
    actual_rows: list[ActualVersion],
) -> list[Finding]:
    findings: list[Finding] = []
    for expected in expected_rows:
        aligned = [row for row in actual_rows if row.valid_from == expected.valid_from]
        if not aligned:
            continue
        # Rows with non-positive intervals are untrustworthy evidence: they
        # must not alibi legitimate rows ("prove" the history correct), only
        # trustworthy rows can suppress a drift finding.
        trustworthy = [row for row in aligned if _valid_interval(row)]
        if any(row.attrs == expected.attrs for row in trustworthy):
            continue
        mismatched = [row for row in aligned if row.attrs != expected.attrs]
        if not mismatched:
            continue
        actual = next(
            (row for row in trustworthy if row.attrs != expected.attrs),
            mismatched[0],
        )
        differences = {
            name: {
                "expected": expected.attrs.get(name),
                "actual": actual.attrs.get(name),
            }
            for name in contract.tracked_attributes
            if expected.attrs.get(name) != actual.attrs.get(name)
        }
        findings.append(
            Finding(
                code="DIM_ATTR_DRIFT",
                severity="medium",
                phase="diagnose",
                natural_key=key,
                start=expected.valid_from,
                end=expected.valid_to,
                message=f"actual attributes differ from expected for {key}",
                details={
                    "expected_version_seq": expected.version_seq,
                    "member_sks": [row.member_sk for row in aligned],
                    "row_nos": [row.row_no for row in aligned],
                    "evidence_row_no": actual.row_no,
                    "differences": differences,
                },
            )
        )
    return findings


def _valid_interval(row: ActualVersion) -> bool:
    return row.valid_to is None or row.valid_from < row.valid_to
