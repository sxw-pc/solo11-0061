"""Deterministic file readers that retain hashes and source locations."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from .util import DimhistError, sha256_bytes


@dataclass(frozen=True)
class RawLine:
    line_no: int
    text: str
    sha256: str


@dataclass(frozen=True)
class CsvTable:
    rows: list[tuple[int, dict[str, str]]]
    sha256: str
    fieldnames: tuple[str, ...]


def read_jsonl(path: Path) -> tuple[list[RawLine], str]:
    data = _read_bytes(path)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DimhistError("INP_002", f"event log must be UTF-8: {path}") from exc
    lines: list[RawLine] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        lines.append(RawLine(line_no, raw, sha256_bytes(raw.encode("utf-8"))))
    if not lines:
        raise DimhistError("INP_002", f"event log is empty: {path}")
    return lines, sha256_bytes(data)


def read_csv_rows(path: Path, required: tuple[str, ...]) -> CsvTable:
    data = _read_bytes(path)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DimhistError("INP_002", f"CSV must be UTF-8: {path}") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise DimhistError("INP_002", f"CSV has no header: {path}")
    headers = tuple(name.strip() for name in reader.fieldnames if name is not None)
    if len(set(headers)) != len(headers):
        raise DimhistError("INP_002", f"CSV has duplicate headers: {path}")
    missing = [name for name in required if name not in headers]
    if missing:
        raise DimhistError(
            "INP_002", f"CSV is missing column(s): {', '.join(missing)}"
        )
    rows: list[tuple[int, dict[str, str]]] = []
    for row in reader:
        normalized: dict[str, str] = {}
        for key, value in row.items():
            if key is None or not isinstance(value, str):
                raise DimhistError(
                    "INP_002", f"CSV line {reader.line_num}: malformed row width"
                )
            normalized[key.strip()] = value
        if not any(value.strip() for value in normalized.values()):
            continue
        rows.append((reader.line_num, normalized))
    return CsvTable(rows=rows, sha256=sha256_bytes(data), fieldnames=headers)


def csv_optional(row: dict[str, str], name: str) -> str:
    value = row.get(name, "")
    return value.strip()


def required_cell(row: dict[str, str], name: str, line_no: int) -> str:
    value = row.get(name, "")
    if not value.strip():
        raise DimhistError(
            "INP_003", f"CSV line {line_no}: column {name!r} must not be empty"
        )
    return value.strip()


def parse_bool(value: str, field: str, line_no: int) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise DimhistError(
        "INP_003", f"CSV line {line_no}: {field} must be true or false"
    )


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DimhistError("INP_001", f"input file is not readable: {path}") from exc


def row_attrs(
    row: dict[str, str], attributes: tuple[str, ...], line_no: int
) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in attributes:
        raw = row.get(name)
        if raw is None:
            raise DimhistError(
                "INP_002", f"CSV line {line_no}: missing attribute column {name!r}"
            )
        values[name] = raw.strip()
    return values


