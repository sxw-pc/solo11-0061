"""Actual-history diagnostics, including structural edge cases."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.actual import load_actual_history
from dimhist.config import load_contract
from dimhist.diagnose import diagnose_current_snapshot
from dimhist.model import ActualVersion, CurrentRow
from dimhist.pipeline import run_pipeline
from dimhist.structural import diagnose_current_state


class DiagnoseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        support.install_fixtures(self.workspace)
        self.result = run_pipeline(self.workspace, now=support.NOW)
        self.contract = load_contract(support.CONTRACT_PATH)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _codes(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for finding in self.result.findings:
            result[finding.code] = result.get(finding.code, 0) + 1
        return result

    def test_fixture_covers_all_structural_codes(self) -> None:
        codes = self._codes()
        self.assertEqual(codes["DIM_OVERLAP"], 3)
        self.assertEqual(codes["DIM_GAP"], 1)
        self.assertEqual(codes["DIM_DUPLICATE_VERSION"], 1)
        self.assertEqual(codes["DIM_MULTI_CURRENT"], 1)
        self.assertEqual(codes["DIM_NO_CURRENT"], 1)
        self.assertEqual(codes["DIM_ATTR_DRIFT"], 1)
        self.assertEqual(codes["DIM_ORPHAN_ACTUAL"], 1)
        self.assertEqual(codes["DIM_MISSING_EXPECTED"], 1)
        self.assertEqual(codes["EVT_DUP_CONFLICT"], 1)
        self.assertEqual(codes["EVT_SAME_TIME_CONFLICT"], 1)

    def test_gap_is_exactly_the_half_open_interval(self) -> None:
        gap = next(item for item in self.result.findings if item.code == "DIM_GAP")
        self.assertEqual(gap.natural_key, "M-1003")
        self.assertEqual(gap.start, "2024-06-01T00:00:00Z")
        self.assertEqual(gap.end, "2024-07-01T00:00:00Z")

    def test_drift_reports_expected_and_actual_values(self) -> None:
        drift = next(
            item for item in self.result.findings if item.code == "DIM_ATTR_DRIFT"
        )
        self.assertEqual(drift.natural_key, "M-1008")
        self.assertEqual(
            drift.details["differences"]["region"],
            {"expected": "EMEA", "actual": "APAC"},
        )

    def test_current_flag_mismatch_is_diagnosed(self) -> None:
        actual = [
            ActualVersion(
                row_no=2,
                member_sk="SK-X",
                natural_key="M-X",
                attrs={"segment": "Bronze", "region": "APAC", "status": "Active"},
                valid_from="2024-01-01T00:00:00Z",
                valid_to=None,
                is_current=False,
                source="test",
                updated_at="",
            )
        ]
        findings = diagnose_current_state("M-X", actual)
        self.assertEqual([finding.code for finding in findings], ["DIM_CURRENT_FLAG"])

    def test_current_snapshot_drift_is_diagnosed(self) -> None:
        actual = [
            ActualVersion(
                row_no=2,
                member_sk="SK-X",
                natural_key="M-X",
                attrs={"segment": "Bronze", "region": "APAC", "status": "Active"},
                valid_from="2024-01-01T00:00:00Z",
                valid_to=None,
                is_current=True,
                source="test",
                updated_at="",
            )
        ]
        snapshot = [
            CurrentRow(
                row_no=2,
                natural_key="M-X",
                attrs={"segment": "Silver", "region": "APAC", "status": "Active"},
                updated_at="",
            )
        ]
        findings = diagnose_current_snapshot(self.contract, actual, snapshot)
        self.assertEqual([finding.code for finding in findings], ["DIM_CURRENT_SNAPSHOT_DRIFT"])

    def test_invalid_interval_is_reported_by_loader(self) -> None:
        path = Path(self.temporary.name) / "bad_history.csv"
        path.write_text(
            "member_sk,member_id,segment,region,status,valid_from,valid_to,"
            "is_current,source,updated_at\n"
            "SK-1,M-X,Bronze,APAC,Active,2024-05-01T00:00:00Z,"
            "2024-04-01T00:00:00Z,false,test,2024-05-01T00:00:00Z\n",
            encoding="utf-8",
        )
        rows, findings = load_actual_history(self.contract, path)
        self.assertEqual(len(rows), 1)
        self.assertEqual([finding.code for finding in findings], ["DIM_INVALID_INTERVAL"])


if __name__ == "__main__":
    unittest.main()
