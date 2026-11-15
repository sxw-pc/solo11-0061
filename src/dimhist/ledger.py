"""SQLite audit ledger for a deterministic, local calibration run."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .model import (
    ActualVersion,
    AsOfCheck,
    CurrentRow,
    EventRecord,
    ExpectedVersion,
    Finding,
    PlanAction,
    TriageView,
)
from .util import canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (
  kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
  PRIMARY KEY (kind, path)
);
CREATE TABLE IF NOT EXISTS events (
  position INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL, natural_key TEXT NOT NULL, effective_at TEXT NOT NULL,
  sequence INTEGER NOT NULL, payload_json TEXT NOT NULL, raw_line INTEGER NOT NULL,
  raw_sha256 TEXT NOT NULL, status TEXT NOT NULL, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expected_versions (
  natural_key TEXT NOT NULL, version_seq INTEGER NOT NULL,
  attrs_json TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
  source_event_ids_json TEXT NOT NULL,
  PRIMARY KEY (natural_key, version_seq)
);
CREATE TABLE IF NOT EXISTS actual_versions (
  row_no INTEGER NOT NULL, member_sk TEXT NOT NULL, natural_key TEXT NOT NULL,
  attrs_json TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
  is_current INTEGER NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_snapshot (
  row_no INTEGER NOT NULL, natural_key TEXT NOT NULL, attrs_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY, code TEXT NOT NULL, severity TEXT NOT NULL,
  phase TEXT NOT NULL, natural_key TEXT NOT NULL, interval_start TEXT,
  interval_end TEXT, message TEXT NOT NULL, details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asof_checks (
  check_id TEXT PRIMARY KEY, natural_key TEXT NOT NULL, at TEXT NOT NULL,
  status TEXT NOT NULL, expected_version_seq INTEGER,
  actual_member_sks_json TEXT NOT NULL, expected_attrs_json TEXT,
  actual_attrs_json TEXT, differences_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_actions (
  action_id TEXT PRIMARY KEY, kind TEXT NOT NULL, natural_key TEXT NOT NULL,
  interval_start TEXT, interval_end TEXT, finding_ids_json TEXT NOT NULL,
  rationale TEXT NOT NULL, risk TEXT NOT NULL, review_required INTEGER NOT NULL,
  details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triage_groups (
  group_id TEXT PRIMARY KEY, root_finding_id TEXT NOT NULL,
  natural_key TEXT NOT NULL, untrustworthy_rows_json TEXT NOT NULL,
  derived_json TEXT NOT NULL, proof_rows_json TEXT NOT NULL
);
"""

TABLE_NAMES = {
    "meta",
    "artifacts",
    "events",
    "expected_versions",
    "actual_versions",
    "current_snapshot",
    "findings",
    "asof_checks",
    "plan_actions",
    "triage_groups",
}


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row

    def reset(self) -> None:
        for table in (
            "meta",
            "artifacts",
            "events",
            "expected_versions",
            "actual_versions",
            "current_snapshot",
            "findings",
            "asof_checks",
            "plan_actions",
            "triage_groups",
        ):
            self.connection.execute(f"DROP TABLE IF EXISTS {table}")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def add_artifact(self, kind: str, path: str, digest: str) -> None:
        self.connection.execute(
            "INSERT INTO artifacts(kind, path, sha256) VALUES (?, ?, ?) "
            "ON CONFLICT(kind, path) DO UPDATE SET sha256=excluded.sha256",
            (kind, path, digest),
        )

    def add_events(self, events: Iterable[EventRecord]) -> None:
        self.connection.executemany(
            "INSERT INTO events(event_id, natural_key, effective_at, sequence, "
            "payload_json, raw_line, raw_sha256, status, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    event.event_id,
                    event.natural_key,
                    event.effective_at,
                    event.sequence,
                    event.payload_json,
                    event.raw_line,
                    event.raw_sha256,
                    event.status,
                    event.raw_text,
                )
                for event in events
            ],
        )

    def add_expected(self, versions: Iterable[ExpectedVersion]) -> None:
        self.connection.executemany(
            "INSERT INTO expected_versions(natural_key, version_seq, attrs_json, "
            "valid_from, valid_to, source_event_ids_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    version.natural_key,
                    version.version_seq,
                    canonical_json(version.attrs),
                    version.valid_from,
                    version.valid_to,
                    canonical_json(list(version.source_event_ids)),
                )
                for version in versions
            ],
        )

    def add_actual(self, versions: Iterable[ActualVersion]) -> None:
        self.connection.executemany(
            "INSERT INTO actual_versions(row_no, member_sk, natural_key, attrs_json, "
            "valid_from, valid_to, is_current, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    version.row_no,
                    version.member_sk,
                    version.natural_key,
                    canonical_json(version.attrs),
                    version.valid_from,
                    version.valid_to,
                    int(version.is_current),
                    version.source,
                    version.updated_at,
                )
                for version in versions
            ],
        )

    def add_current_snapshot(self, rows: Iterable[CurrentRow]) -> None:
        self.connection.executemany(
            "INSERT INTO current_snapshot(row_no, natural_key, attrs_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [
                (row.row_no, row.natural_key, canonical_json(row.attrs), row.updated_at)
                for row in rows
            ],
        )

    def add_findings(self, findings: Iterable[Finding]) -> None:
        self.connection.executemany(
            "INSERT INTO findings(finding_id, code, severity, phase, natural_key, "
            "interval_start, interval_end, message, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item.finding_id,
                    item.code,
                    item.severity,
                    item.phase,
                    item.natural_key,
                    item.start,
                    item.end,
                    item.message,
                    canonical_json(item.details),
                )
                for item in findings
            ],
        )

    def add_asof(self, checks: Iterable[AsOfCheck]) -> None:
        self.connection.executemany(
            "INSERT INTO asof_checks(check_id, natural_key, at, status, "
            "expected_version_seq, actual_member_sks_json, expected_attrs_json, "
            "actual_attrs_json, differences_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    check.check_id,
                    check.natural_key,
                    check.at,
                    check.status,
                    check.expected_version_seq,
                    canonical_json(list(check.actual_member_sks)),
                    canonical_json(check.expected_attrs)
                    if check.expected_attrs is not None
                    else None,
                    canonical_json(check.actual_attrs)
                    if check.actual_attrs is not None
                    else None,
                    canonical_json(check.differences),
                )
                for check in checks
            ],
        )

    def add_plan(self, actions: Iterable[PlanAction]) -> None:
        self.connection.executemany(
            "INSERT INTO plan_actions(action_id, kind, natural_key, interval_start, "
            "interval_end, finding_ids_json, rationale, risk, review_required, "
            "details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    action.action_id,
                    action.kind,
                    action.natural_key,
                    action.start,
                    action.end,
                    canonical_json(list(action.finding_ids)),
                    action.rationale,
                    action.risk,
                    int(action.review_required),
                    canonical_json(action.details),
                )
                for action in actions
            ],
        )

    def add_triage(self, view: TriageView) -> None:
        self.connection.executemany(
            "INSERT INTO triage_groups(group_id, root_finding_id, natural_key, "
            "untrustworthy_rows_json, derived_json, proof_rows_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    group.group_id,
                    group.root_finding_id,
                    group.natural_key,
                    canonical_json(list(group.untrustworthy_rows)),
                    canonical_json(
                        [item.to_dict() for item in group.derived]
                    ),
                    canonical_json(list(group.proof_rows)),
                )
                for group in view.groups
            ],
        )

    def commit(self) -> None:
        self.connection.commit()

    def count(self, table: str) -> int:
        if table not in TABLE_NAMES:
            raise ValueError(f"unknown ledger table: {table}")
        row = self.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    def rows(self, table: str, order_by: str = "") -> list[dict[str, Any]]:
        if table not in TABLE_NAMES:
            raise ValueError(f"unknown ledger table: {table}")
        suffix = f" ORDER BY {order_by}" if order_by else ""
        result = self.connection.execute(f"SELECT * FROM {table}{suffix}").fetchall()
        return [dict(row) for row in result]

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
