"""Half-open as-of reconciliation checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.asof import asof_stats
from dimhist.pipeline import run_pipeline


class AsOfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        workspace = Path(self.temporary.name) / "workspace"
        support.install_fixtures(workspace)
        self.result = run_pipeline(workspace, now=support.NOW)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _check(self, key: str, at: str):
        return next(
            check
            for check in self.result.asof_checks
            if check.natural_key == key and check.at == at
        )

    def test_fixture_asof_shape(self) -> None:
        stats = asof_stats(self.result.asof_checks)
        self.assertEqual(stats["checks"], 33)
        self.assertEqual(stats["mismatches"], 14)
        self.assertEqual(
            stats["by_status"],
            {
                "attribute_drift": 2,
                "match": 19,
                "missing_actual": 6,
                "multiple_actual": 4,
                "orphan_actual": 2,
            },
        )

    def test_boundary_belongs_to_the_next_version(self) -> None:
        check = self._check("M-1001", "2024-05-01T00:00:00Z")
        self.assertEqual(check.status, "match")
        self.assertEqual(check.expected_version_seq, 3)
        self.assertEqual(check.expected_attrs["region"], "APAC")

    def test_gap_is_missing_at_next_expected_boundary(self) -> None:
        check = self._check("M-1003", "2024-06-01T00:00:00Z")
        self.assertEqual(check.status, "missing_actual")
        self.assertEqual(check.expected_version_seq, 2)
        self.assertEqual(check.actual_member_sks, ())

    def test_no_current_member_is_missing_at_now(self) -> None:
        check = self._check("M-1005", support.NOW)
        self.assertEqual(check.status, "missing_actual")

    def test_overlapping_actual_rows_produce_multiple_matches(self) -> None:
        check = self._check("M-1004", support.NOW)
        self.assertEqual(check.status, "multiple_actual")
        self.assertEqual(
            check.actual_member_sks, ("SK-1004-02", "SK-1004-03")
        )

    def test_attribute_drift_is_visible_at_now(self) -> None:
        check = self._check("M-1008", support.NOW)
        self.assertEqual(check.status, "attribute_drift")
        self.assertEqual(
            check.differences["region"], {"expected": "EMEA", "actual": "APAC"}
        )

    def test_orphan_actual_is_visible(self) -> None:
        check = self._check("M-1007", support.NOW)
        self.assertEqual(check.status, "orphan_actual")

    def test_check_ids_are_sequential_and_stable(self) -> None:
        ids = [check.check_id for check in self.result.asof_checks]
        self.assertEqual(ids[0], "ASOF-0001")
        self.assertEqual(ids[-1], f"ASOF-{len(ids):04d}")
        self.assertEqual(ids, sorted(ids))
        repeated = [check.to_dict() for check in self.result.asof_checks]
        self.assertEqual(repeated[0]["at"], "2024-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
