"""Small, shared utilities with stable error handling."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
UTC = timezone.utc


class DimhistError(Exception):
    """An expected, user-facing failure carrying a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


def parse_time(value: Any, code: str = "INP_004", field: str = "time") -> datetime:
    """Parse an ISO8601 timestamp with an explicit offset into UTC.

    Seconds precision is required so that every persisted value is stable.
    """
    if not isinstance(value, str) or not TIME_PATTERN.match(value):
        raise DimhistError(code, f"{field} must be ISO8601 with a UTC offset")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DimhistError(code, f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise DimhistError(code, f"{field} must include a timezone")
    if parsed.microsecond != 0:
        raise DimhistError(code, f"{field} must use whole-second precision")
    return parsed.astimezone(UTC)


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_time(value: Any, code: str = "INP_004", field: str = "time") -> str:
    return format_time(parse_time(value, code=code, field=field))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def deep_plain(value: Any) -> Any:
    """Return a JSON-safe copy with stable primitive types."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: Any) -> None:
    write_text(path, pretty_json(value))


def relative_display(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def sorted_counts(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def validate_loopback_host(host: str) -> str:
    if not is_loopback_host(host):
        raise DimhistError(
            "SRV_001", f"host {host!r} is not allowed; use 127.0.0.1, localhost or ::1"
        )
    return host


def require_positive_sequence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DimhistError("INP_003", "sequence must be a non-negative integer")
    return value
