"""Audit ledger persistence and deterministic report rendering."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.ledger import Ledger
from dimhist.pipeline import run_pipeline
from dimhist.render import render_html
from dimhist.report import (
    build_plan_document,
    build_report,
    build_summary,
    write_reports,
)
from dimhist.util import sha256_file


class LedgerReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        support.install_fixtures(self.workspace)
        self.result = run_pipeline(self.workspace, now=support.NOW)
        self.paths = write_reports(self.workspace, self.result)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ledger_contains_all_audit_tables(self) -> None:
        with Ledger(self.workspace / "ledger" / "dimhist.sqlite3") as ledger:
            expected_counts = {
                "events": 23,
                "expected_versions": 19,
                "actual_versions": 20,
                "current_snapshot": 7,
                "findings": 12,
                "asof_checks": 33,
                "plan_actions": 12,
                "artifacts": 4,
            }
            for table, count in expected_counts.items():
                self.assertEqual(ledger.count(table), count, table)
            meta = {row["key"]: row["value"] for row in ledger.rows("meta")}
            self.assertEqual(meta["generated_at"], support.NOW)
            self.assertEqual(meta["table"], "dim_member")
            event = ledger.rows("events", "position")[0]
            self.assertIn("EVT-1001", event["raw_json"])
            self.assertEqual(len(event["raw_sha256"]), 64)

    def test_artifacts_capture_real_input_hashes(self) -> None:
        with Ledger(self.workspace / "ledger" / "dimhist.sqlite3") as ledger:
            artifacts = {
                row["kind"]: row for row in ledger.rows("artifacts", "kind")
            }
        self.assertEqual(
            artifacts["contract"]["sha256"],
            sha256_file(support.CONTRACT_PATH),
        )
        self.assertEqual(
            artifacts["event_log"]["sha256"],
            sha256_file(support.FIXTURES / "events" / "change_events.jsonl"),
        )
        self.assertEqual(
            artifacts["actual_history"]["path"], "data/dimension/member_history.csv"
        )

    def test_report_json_matches_expected_summary(self) -> None:
        report = json.loads(self.paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(report["summary"], support.expected_summary())
        self.assertEqual(report["generated_at"], support.NOW)
        self.assertTrue(report["read_only"])
        self.assertEqual(len(report["inputs"]), 4)
        self.assertEqual(report["contract"]["sha256"], self.result.contract.sha256)
        self.assertEqual(build_summary(self.result), support.expected_summary())

    def test_markdown_and_plan_are_text_outputs(self) -> None:
        markdown = self.paths["markdown"].read_text(encoding="utf-8")
        plan_markdown = self.paths["plan_markdown"].read_text(encoding="utf-8")
        plan_json = json.loads(self.paths["plan_json"].read_text(encoding="utf-8"))
        self.assertIn("DIM_OVERLAP", markdown)
        self.assertIn("Dry-run repair plan", markdown)
        self.assertIn("not executable SQL", plan_markdown)
        self.assertEqual(plan_json["mode"], "dry_run_only")
        self.assertEqual(len(plan_json["actions"]), 12)
        self.assertEqual(plan_json, build_plan_document(self.result))

    def test_html_is_local_and_escapes_dynamic_text(self) -> None:
        html_text = self.paths["html"].read_text(encoding="utf-8")
        self.assertIn("/api/summary", html_text)
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)
        report = build_report(self.result)
        hostile = copy.deepcopy(report)
        hostile["findings"][0]["message"] = "<script>alert(1)</script>"
        rendered = render_html(hostile)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)

    def test_repeated_run_produces_byte_identical_json(self) -> None:
        first = self.paths["json"].read_bytes()
        first_plan = self.paths["plan_json"].read_bytes()
        repeated = run_pipeline(self.workspace, now=support.NOW)
        write_reports(self.workspace, repeated)
        self.assertEqual(first, self.paths["json"].read_bytes())
        self.assertEqual(first_plan, self.paths["plan_json"].read_bytes())


if __name__ == "__main__":
    unittest.main()
