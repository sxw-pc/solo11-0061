"""Root-cause review grouping (triage) for dimension diagnostics.

This module builds a read-only review view over the raw findings. A finding
is marked as a *derived symptom* of a root cause only when a counterfactual
proof shows the violation completely depends on rows already deemed
untrustworthy (non-positive intervals): re-running the scoped per-key
diagnostics without those rows makes the violation disappear. Violations
that still hold on the remaining legitimate rows stay independent — sharing
a business key, an error code or evidence rows is never enough to merge.

The view never deletes findings, never rewrites history and never executes
repairs; it only organizes evidence for human review.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .config import Contract
from .diagnose import diagnose_key, snapshot_drift_for_key
from .model import (
    ActualVersion,
    CurrentRow,
    DerivedFinding,
    ExpectedVersion,
    Finding,
    TriageGroup,
    TriageView,
)
from .util import canonical_json

ROOT_CAUSE_CODE = "DIM_INVALID_INTERVAL"
SINGLE_REMOVAL = "counterfactual_single_removal"
JOINT_REMOVAL = "counterfactual_joint_removal"


def triage_findings(
    contract: Contract,
    expected: list[ExpectedVersion],
    actual: list[ActualVersion],
    current_rows: list[CurrentRow],
    findings: list[Finding],
) -> TriageView:
    """Group findings by root-cause evidence using counterfactual proofs."""
    rows_by_key: dict[str, list[ActualVersion]] = defaultdict(list)
    for row in actual:
        rows_by_key[row.natural_key].append(row)
    expected_by_key: dict[str, list[ExpectedVersion]] = defaultdict(list)
    for version in expected:
        expected_by_key[version.natural_key].append(version)
    snapshot_by_key: dict[str, list[CurrentRow]] = defaultdict(list)
    for row in current_rows:
        snapshot_by_key[row.natural_key].append(row)

    untrusted_by_key: dict[str, list[int]] = defaultdict(list)
    root_finding_by_row: dict[int, str] = {}
    for finding in findings:
        if finding.code == ROOT_CAUSE_CODE:
            row_no = int(finding.details["row_no"])
            untrusted_by_key[finding.natural_key].append(row_no)
            root_finding_by_row[row_no] = finding.finding_id

    candidates_by_key: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        if (
            finding.phase == "diagnose"
            and finding.code != ROOT_CAUSE_CODE
            and finding.natural_key in untrusted_by_key
        ):
            candidates_by_key[finding.natural_key].append(finding)

    derived_by_id: dict[str, tuple[DerivedFinding, int]] = {}
    for key in sorted(untrusted_by_key):
        untrusted = sorted(untrusted_by_key[key])
        base_rows = sorted(
            rows_by_key[key],
            key=lambda item: (item.valid_from, item.member_sk, item.row_no),
        )
        scope = _KeyScope(
            contract,
            key,
            base_rows,
            expected_by_key.get(key, []),
            snapshot_by_key.get(key, []),
        )
        joint_violations = scope.violations_without(tuple(untrusted))
        for finding in candidates_by_key.get(key, []):
            violation = _violation_key(finding)
            if violation in joint_violations:
                continue  # still holds on legitimate rows: keep independent
            hit = next(
                (
                    row_no
                    for row_no in untrusted
                    if violation not in scope.violations_without((row_no,))
                ),
                None,
            )
            if hit is not None:
                removed = (hit,)
                method = SINGLE_REMOVAL
                root_row = hit
            else:
                removed = tuple(untrusted)
                method = JOINT_REMOVAL
                root_row = untrusted[0]
            remaining = tuple(
                row.row_no for row in base_rows if row.row_no not in removed
            )
            derived_by_id[finding.finding_id] = (
                DerivedFinding(
                    finding_id=finding.finding_id,
                    code=finding.code,
                    method=method,
                    removed_rows=removed,
                    remaining_rows=remaining,
                ),
                root_row,
            )

    groups: list[TriageGroup] = []
    for key in sorted(untrusted_by_key):
        for row_no in sorted(untrusted_by_key[key]):
            derived = tuple(
                item
                for finding_id, (item, root_row) in sorted(derived_by_id.items())
                if root_row == row_no
            )
            groups.append(
                TriageGroup(
                    group_id="",
                    root_finding_id=root_finding_by_row[row_no],
                    root_code=ROOT_CAUSE_CODE,
                    natural_key=key,
                    untrustworthy_rows=(row_no,),
                    derived=derived,
                    proof_rows=tuple(
                        sorted(row.row_no for row in rows_by_key[key])
                    ),
                )
            )
    groups.sort(key=lambda group: group.root_finding_id)
    for index, group in enumerate(groups, start=1):
        group.group_id = f"TRI-{index:04d}"

    unattributed = tuple(
        finding.finding_id
        for finding in findings
        if finding.code != ROOT_CAUSE_CODE
        and finding.finding_id not in derived_by_id
    )
    return TriageView(
        groups=tuple(groups),
        unattributed=unattributed,
        findings_total=len(findings),
    )


class _KeyScope:
    """Scoped counterfactual re-diagnosis for one natural key.

    Only the rows of this key are re-checked; event replay, as-of sampling,
    planning and other keys are never re-run. Single-removal results are
    cached so each untrustworthy row is removed at most once.
    """

    def __init__(
        self,
        contract: Contract,
        key: str,
        base_rows: list[ActualVersion],
        expected_rows: list[ExpectedVersion],
        snapshot_rows: list[CurrentRow],
    ):
        self.contract = contract
        self.key = key
        self.base_rows = base_rows
        self.expected_rows = expected_rows
        self.snapshot_rows = snapshot_rows
        self._cache: dict[tuple[int, ...], set[tuple[Any, ...]]] = {}

    def violations_without(self, removed: tuple[int, ...]) -> set[tuple[Any, ...]]:
        if removed not in self._cache:
            remaining = [
                row for row in self.base_rows if row.row_no not in removed
            ]
            findings = diagnose_key(
                self.contract, self.key, self.expected_rows, remaining
            )
            snapshot = snapshot_drift_for_key(
                self.contract,
                self.key,
                [row for row in remaining if row.valid_to is None],
                self.snapshot_rows,
            )
            if snapshot is not None:
                findings.append(snapshot)
            self._cache[removed] = {_violation_key(item) for item in findings}
        return self._cache[removed]


def _violation_key(finding: Finding) -> tuple[Any, ...]:
    """Identity of the violation itself, independent of evidence row lists.

    Two findings share a violation key iff they report the same violation;
    evidence details (member SK lists, row sets) may shrink when untrustworthy
    rows are removed without the violation itself disappearing.
    """
    code = finding.code
    key = finding.natural_key
    details = finding.details
    if code == "DIM_CURRENT_FLAG":
        return (code, key, details.get("row_no"))
    if code == "DIM_DUPLICATE_VERSION":
        return (
            code,
            key,
            finding.start,
            canonical_json(details.get("attributes", {})),
        )
    if code == "DIM_ATTR_DRIFT":
        return (code, key, finding.start, details.get("expected_version_seq"))
    if code == "DIM_OVERLAP":
        return (
            code,
            key,
            finding.start,
            finding.end,
            tuple(sorted(details.get("row_nos", []))),
        )
    return (code, key, finding.start, finding.end)
