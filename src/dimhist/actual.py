"""Load the warehouse-side dimension history and current snapshot."""

from __future__ import annotations

from pathlib import Path

from .config import Contract
from .io_utils import (
    csv_optional,
    parse_bool,
    read_csv_rows,
    required_cell,
    row_attrs,
)
from .model import ActualVersion, CurrentRow, Finding
from .util import normalize_time

HISTORY_REQUIRED = (
    "member_sk",
    "valid_from",
    "valid_to",
    "is_current",
    "source",
    "updated_at",
)
def load_actual_history(
    contract: Contract, path: Path
) -> tuple[list[ActualVersion], list[Finding]]:
    required = HISTORY_REQUIRED + (contract.natural_key,) + contract.tracked_attributes
    table = read_csv_rows(path, required)
    versions: list[ActualVersion] = []
    findings: list[Finding] = []
    for row_no, row in table.rows:
        member_sk = required_cell(row, "member_sk", row_no)
        natural_key = required_cell(row, contract.natural_key, row_no)
        attrs = row_attrs(row, contract.tracked_attributes, row_no)
        valid_from = normalize_time(
            required_cell(row, "valid_from", row_no),
            code="INP_004",
            field=f"CSV line {row_no}: valid_from",
        )
        valid_to_raw = csv_optional(row, "valid_to")
        valid_to = (
            normalize_time(
                valid_to_raw,
                code="INP_004",
                field=f"CSV line {row_no}: valid_to",
            )
            if valid_to_raw
            else None
        )
        is_current = parse_bool(row.get("is_current", ""), "is_current", row_no)
        updated_raw = csv_optional(row, "updated_at")
        updated_at = (
            normalize_time(
                updated_raw,
                code="INP_004",
                field=f"CSV line {row_no}: updated_at",
            )
            if updated_raw
            else ""
        )
        version = ActualVersion(
            row_no=row_no,
            member_sk=member_sk,
            natural_key=natural_key,
            attrs=attrs,
            valid_from=valid_from,
            valid_to=valid_to,
            is_current=is_current,
            source=csv_optional(row, "source"),
            updated_at=updated_at,
        )
        versions.append(version)
        if valid_to is not None and valid_to <= valid_from:
            findings.append(
                Finding(
                    code="DIM_INVALID_INTERVAL",
                    severity="medium",
                    phase="diagnose",
                    natural_key=natural_key,
                    start=valid_from,
                    end=valid_to,
                    message=(
                        f"actual row {row_no} has non-positive interval "
                        f"[{valid_from}, {valid_to})"
                    ),
                    details={"member_sk": member_sk, "row_no": row_no},
                )
            )
    return versions, findings


def load_current_snapshot(
    contract: Contract, path: Path
) -> tuple[list[CurrentRow], str]:
    required = (contract.natural_key,) + contract.tracked_attributes
    table = read_csv_rows(path, required)
    rows: list[CurrentRow] = []
    for row_no, row in table.rows:
        natural_key = required_cell(row, contract.natural_key, row_no)
        attrs = row_attrs(row, contract.tracked_attributes, row_no)
        updated_raw = csv_optional(row, "updated_at")
        updated_at = (
            normalize_time(
                updated_raw,
                code="INP_004",
                field=f"CSV line {row_no}: updated_at",
            )
            if updated_raw
            else ""
        )
        rows.append(
            CurrentRow(
                row_no=row_no,
                natural_key=natural_key,
                attrs=attrs,
                updated_at=updated_at,
            )
        )
    return rows, table.sha256
