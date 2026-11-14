"""Contract loading and static validation for the SCD2 dimension."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import DimhistError, sha256_bytes

ALLOWED_TYPES = {"string", "integer", "boolean", "decimal"}
SAME_TIME_POLICIES = {"sequence_then_event_id_then_line"}
DELETE_SEMANTICS = {"status_only", "forbid"}
TIME_FORMATS = {"iso8601_utc_seconds"}
REQUIRED_KEYS = (
    "schema_version",
    "table",
    "natural_key",
    "tracked_attributes",
    "attribute_types",
    "effective_at_field",
    "event_id_field",
    "sequence_field",
    "payload_field",
    "same_time_policy",
    "time_format",
    "delete_semantics",
)


@dataclass(frozen=True)
class Contract:
    schema_version: int
    table: str
    natural_key: str
    tracked_attributes: tuple[str, ...]
    attribute_types: dict[str, str]
    effective_at_field: str
    event_id_field: str
    sequence_field: str
    payload_field: str
    same_time_policy: str
    time_format: str
    delete_semantics: str
    sha256: str
    source_path: str = ""

    def attrs_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {name: payload.get(name) for name in self.tracked_attributes}

    def to_dict(self, include_hash: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "table": self.table,
            "natural_key": self.natural_key,
            "tracked_attributes": list(self.tracked_attributes),
            "attribute_types": dict(sorted(self.attribute_types.items())),
            "effective_at_field": self.effective_at_field,
            "event_id_field": self.event_id_field,
            "sequence_field": self.sequence_field,
            "payload_field": self.payload_field,
            "same_time_policy": self.same_time_policy,
            "time_format": self.time_format,
            "delete_semantics": self.delete_semantics,
        }
        if include_hash:
            data["sha256"] = self.sha256
        return data


def load_contract(path: Path) -> Contract:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DimhistError("CFG_001", f"contract file is not readable: {path}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DimhistError("CFG_001", "contract must be UTF-8 encoded") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DimhistError("CFG_001", f"contract is not valid JSON: {exc.msg}") from exc
    contract = parse_contract(document, sha256_bytes(raw))
    return Contract(**{**contract.__dict__, "source_path": str(path)})


def parse_contract(document: Any, digest: str) -> Contract:
    if not isinstance(document, dict):
        raise DimhistError("CFG_001", "contract root must be a JSON object")
    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        raise DimhistError("CFG_002", f"missing required field(s): {', '.join(missing)}")
    if document["schema_version"] != 1:
        raise DimhistError("CFG_002", "schema_version must be 1")

    table = _non_empty_string(document["table"], "table")
    natural_key = _non_empty_string(document["natural_key"], "natural_key")
    attrs_raw = document["tracked_attributes"]
    if not isinstance(attrs_raw, list) or not attrs_raw:
        raise DimhistError("CFG_002", "tracked_attributes must be a non-empty list")
    attrs = [_non_empty_string(item, "tracked_attributes[]") for item in attrs_raw]
    if len(set(attrs)) != len(attrs):
        raise DimhistError("CFG_003", "tracked_attributes contains duplicates")
    if natural_key in attrs:
        raise DimhistError("CFG_003", "natural_key cannot be a tracked attribute")

    types = document["attribute_types"]
    if not isinstance(types, dict):
        raise DimhistError("CFG_004", "attribute_types must be an object")
    if set(types) != set(attrs):
        raise DimhistError(
            "CFG_004", "attribute_types must map exactly the tracked_attributes"
        )
    for name, kind in types.items():
        if kind not in ALLOWED_TYPES:
            raise DimhistError(
                "CFG_004", f"unknown attribute type {kind!r} for {name}"
            )

    fields = {}
    for key in (
        "effective_at_field",
        "event_id_field",
        "sequence_field",
        "payload_field",
    ):
        fields[key] = _non_empty_string(document[key], key)
    if len(set(fields.values())) != len(fields) or natural_key in fields.values():
        raise DimhistError("CFG_008", "contract field names must be distinct")

    policy = document["same_time_policy"]
    if policy not in SAME_TIME_POLICIES:
        raise DimhistError("CFG_005", f"unsupported same_time_policy: {policy!r}")
    time_format = document["time_format"]
    if time_format not in TIME_FORMATS:
        raise DimhistError("CFG_007", f"unsupported time_format: {time_format!r}")
    delete_semantics = document["delete_semantics"]
    if delete_semantics not in DELETE_SEMANTICS:
        raise DimhistError(
            "CFG_006", f"unsupported delete_semantics: {delete_semantics!r}"
        )

    return Contract(
        schema_version=1,
        table=table,
        natural_key=natural_key,
        tracked_attributes=tuple(attrs),
        attribute_types={name: str(types[name]) for name in attrs},
        effective_at_field=fields["effective_at_field"],
        event_id_field=fields["event_id_field"],
        sequence_field=fields["sequence_field"],
        payload_field=fields["payload_field"],
        same_time_policy=str(policy),
        time_format=str(time_format),
        delete_semantics=str(delete_semantics),
        sha256=digest,
    )


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DimhistError("CFG_002", f"{field} must be a non-empty string")
    return value.strip()
