"""Expected-history replay behavior over the bundled fixture."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.pipeline import run_pipeline


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        support.install_fixtures(self.workspace)
        self.result = run_pipeline(self.workspace, now=support.NOW)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_late_event_is_reordered_by_effective_time(self) -> None:
        versions = [
            version
            for version in self.result.expected
            if version.natural_key == "M-1001"
        ]
        self.assertEqual(len(versions), 6)
        self.assertEqual(
            [version.valid_from for version in versions],
            [
                "2024-01-01T00:00:00Z",
                "2024-04-01T00:00:00Z",
                "2024-05-01T00:00:00Z",
                "2024-06-01T00:00:00Z",
                "2024-07-01T00:00:00Z",
                "2024-08-01T00:00:00Z",
            ],
        )
        self.assertEqual(versions[2].attrs["region"], "APAC")
        self.assertEqual(versions[2].source_event_ids, ("EVT-1004",))
        self.assertEqual(versions[3].attrs["region"], "EMEA")

    def test_attribute_rollback_and_open_interval_are_preserved(self) -> None:
        versions = [
            version
            for version in self.result.expected
            if version.natural_key == "M-1001"
        ]
        self.assertEqual(versions[-1].attrs["segment"], "Silver")
        self.assertEqual(versions[-1].attrs["status"], "Inactive")
        self.assertIsNone(versions[-1].valid_to)
        self.assertEqual(versions[4].valid_to, versions[5].valid_from)

    def test_same_time_group_reaches_one_final_version(self) -> None:
        versions = [
            version
            for version in self.result.expected
            if version.natural_key == "M-1002"
        ]
        self.assertEqual(len(versions), 3)
        self.assertEqual(versions[-1].attrs["segment"], "Gold")
        self.assertEqual(versions[-1].valid_from, "2024-03-01T00:00:00Z")
        self.assertEqual(
            versions[-1].source_event_ids, ("EVT-2003", "EVT-2004")
        )
        statuses = {
            event.event_id: event.status
            for event in self.result.events
            if event.event_id in {"EVT-2003", "EVT-2004", "EVT-2005"}
        }
        self.assertEqual(statuses["EVT-2003"], "superseded_same_time")
        self.assertEqual(statuses["EVT-2004"], "applied")
        self.assertEqual(statuses["EVT-2005"], "noop")

    def test_event_stats_match_fixture_shape(self) -> None:
        stats = self.result.event_stats
        self.assertEqual(stats["event_rows"], 23)
        self.assertEqual(stats["unique_event_ids"], 21)
        self.assertEqual(stats["duplicate_exact"], 1)
        self.assertEqual(stats["duplicate_conflict"], 1)
        self.assertEqual(stats["noop_events"], 1)
        self.assertEqual(stats["expected_versions"], 19)
        self.assertEqual(stats["same_time_conflict_groups"], 1)

    def test_intervals_are_sorted_and_contiguous_per_key(self) -> None:
        keys = sorted({version.natural_key for version in self.result.expected})
        for key in keys:
            versions = sorted(
                (
                    version
                    for version in self.result.expected
                    if version.natural_key == key
                ),
                key=lambda item: item.valid_from,
            )
            self.assertEqual(
                [version.version_seq for version in versions],
                list(range(1, len(versions) + 1)),
            )
            for left, right in zip(versions, versions[1:]):
                self.assertEqual(left.valid_to, right.valid_from)

    def test_repeat_run_has_identical_expected_history(self) -> None:
        repeated = run_pipeline(self.workspace, now=support.NOW)
        first = [version.to_dict() for version in self.result.expected]
        second = [version.to_dict() for version in repeated.expected]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
