"""Half-open interval as-of reconciliation between expected and actual rows."""

from __future__ import annotations

from collections import defaultdict

from .config import Contract
from .model import ActualVersion, AsOfCheck, ExpectedVersion
from .util import parse_time


def reconcile_asof(
    contract: Contract,
    expected: list[ExpectedVersion],
    actual: list[ActualVersion],
    now: str,
) -> list[AsOfCheck]:
    parse_time(now, code="CFG_002", field="now")
    by_key_expected: dict[str, list[ExpectedVersion]] = defaultdict(list)
    by_key_actual: dict[str, list[ActualVersion]] = defaultdict(list)
    for version in expected:
        if version.valid_from <= now:
            by_key_expected[version.natural_key].append(version)
    for version in actual:
        if _valid_interval(version) and version.valid_from <= now:
            by_key_actual[version.natural_key].append(version)

    checks: list[AsOfCheck] = []
    keys = sorted(set(by_key_expected) | set(by_key_actual))
    for key in keys:
        instants = {now}
        for version in by_key_expected.get(key, []):
            instants.add(version.valid_from)
            if version.valid_to is not None and version.valid_to <= now:
                instants.add(version.valid_to)
        for version in by_key_actual.get(key, []):
            instants.add(version.valid_from)
            if version.valid_to is not None and version.valid_to <= now:
                instants.add(version.valid_to)
        for at in sorted(instants):
            check = _check_at(contract, key, at, by_key_expected.get(key, []), by_key_actual.get(key, []))
            if check is not None:
                checks.append(check)
    return checks


def _check_at(
    contract: Contract,
    key: str,
    at: str,
    expected_rows: list[ExpectedVersion],
    actual_rows: list[ActualVersion],
) -> AsOfCheck | None:
    expected_matches = [row for row in expected_rows if _contains(row.valid_from, row.valid_to, at)]
    actual_matches = [row for row in actual_rows if _contains(row.valid_from, row.valid_to, at)]
    if not expected_matches and not actual_matches:
        return None
    expected = expected_matches[0] if expected_matches else None
    actual = actual_matches[0] if actual_matches else None
    differences: dict[str, dict[str, object]] = {}
    if expected is not None and actual is not None:
        differences = {
            name: {"expected": expected.attrs.get(name), "actual": actual.attrs.get(name)}
            for name in contract.tracked_attributes
            if expected.attrs.get(name) != actual.attrs.get(name)
        }
    if len(expected_matches) > 1:
        status = "multiple_expected"
    elif len(actual_matches) > 1:
        status = "multiple_actual"
    elif expected is None:
        status = "orphan_actual"
    elif actual is None:
        status = "missing_actual"
    elif differences:
        status = "attribute_drift"
    else:
        status = "match"
    return AsOfCheck(
        natural_key=key,
        at=at,
        status=status,
        expected_version_seq=expected.version_seq if expected else None,
        actual_member_sks=tuple(sorted(row.member_sk for row in actual_matches)),
        expected_attrs=dict(expected.attrs) if expected else None,
        actual_attrs=dict(actual.attrs) if actual else None,
        differences=differences,
    )


def _contains(start: str, end: str | None, at: str) -> bool:
    return start <= at and (end is None or at < end)


def _valid_interval(version: ActualVersion) -> bool:
    return version.valid_to is None or version.valid_from < version.valid_to


def finalize_asof(checks: list[AsOfCheck]) -> list[AsOfCheck]:
    ordered = sorted(checks, key=lambda item: (item.natural_key, item.at, item.status))
    for index, check in enumerate(ordered, start=1):
        check.check_id = f"ASOF-{index:04d}"
    return ordered


def asof_stats(checks: list[AsOfCheck]) -> dict[str, object]:
    by_status: dict[str, int] = {}
    for check in checks:
        by_status[check.status] = by_status.get(check.status, 0) + 1
    ordered = dict(sorted(by_status.items()))
    mismatches = sum(
        count for status, count in ordered.items() if status != "match"
    )
    return {
        "checks": len(checks),
        "mismatches": mismatches,
        "by_status": ordered,
    }
