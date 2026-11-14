"""Deterministic, dry-run-only repair planning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.pipeline import run_pipeline
from dimhist.planner import build_plan, plan_stats


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        workspace = Path(self.temporary.name) / "workspace"
        support.install_fixtures(workspace)
        self.result = run_pipeline(workspace, now=support.NOW)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_one_action_per_finding_with_stable_ids(self) -> None:
        self.assertEqual(len(self.result.plan), len(self.result.findings))
        self.assertEqual(
            [action.action_id for action in self.result.plan],
            [f"ACT-{index:04d}" for index in range(1, 13)],
        )
        for action, finding in zip(self.result.plan, self.result.findings):
            self.assertEqual(action.finding_ids, (finding.finding_id,))
            self.assertEqual(action.natural_key, finding.natural_key)

    def test_expected_action_mappings(self) -> None:
        mappings = {
            action.details["source_code"]: action.kind
            for action in self.result.plan
        }
        self.assertEqual(mappings["EVT_DUP_CONFLICT"], "REVIEW_EVENT_CONFLICT")
        self.assertEqual(mappings["EVT_SAME_TIME_CONFLICT"], "REVIEW_EVENT_ORDER")
        self.assertEqual(mappings["DIM_OVERLAP"], "RESOLVE_OVERLAP")
        self.assertEqual(mappings["DIM_MISSING_EXPECTED"], "CREATE_MISSING_VERSIONS")
        self.assertEqual(mappings["DIM_ATTR_DRIFT"], "REWRITE_VERSION_ATTRIBUTES")

    def test_every_action_is_reviewed_and_dry_run(self) -> None:
        for action in self.result.plan:
            self.assertTrue(action.review_required)
            self.assertEqual(action.details["execution_mode"], "dry_run_only")
            self.assertIn(action.risk, {"low", "medium", "high"})
            self.assertNotIn("sql", action.details)

    def test_plan_stats_are_sorted(self) -> None:
        stats = plan_stats(self.result.plan)
        self.assertEqual(stats["actions"], 12)
        self.assertEqual(stats["review_required"], 12)
        self.assertEqual(stats["by_kind"], dict(sorted(stats["by_kind"].items())))
        self.assertEqual(stats["by_risk"], {"high": 6, "low": 2, "medium": 4})

    def test_build_plan_is_deterministic_for_finalized_findings(self) -> None:
        first = [action.to_dict() for action in build_plan(self.result.findings)]
        second = [
            action.to_dict()
            for action in build_plan(list(reversed(self.result.findings)))
        ]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
