"""Root-cause review grouping (triage) over the raw diagnostic findings.

The triage view must only mark a finding as a derived symptom when a
counterfactual proof shows the violation disappears once untrustworthy
(non-positive interval) rows are removed. Violations that still hold on the
remaining legitimate rows must stay independent ("移除不可信证据后该违约仍
成立" — the violation survives removal of untrustworthy evidence).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support as support

from dimhist import triage
from dimhist.ledger import Ledger
from dimhist.pipeline import run_pipeline
from dimhist.render import render_html, render_markdown
from dimhist.report import build_report, write_reports
from dimhist.triage import triage_findings

HISTORY_HEADER = (
    "member_sk,member_id,segment,region,status,valid_from,valid_to,"
    "is_current,source,updated_at"
)
CURRENT_HEADER = "member_id,segment,region,status,updated_at"
STAMP = "2024-01-01T00:00:00Z"


def _history(rows: list[tuple[str, ...]]) -> str:
    lines = [HISTORY_HEADER]
    for sk, key, segment, region, status, valid_from, valid_to, is_current in rows:
        lines.append(
            ",".join(
                [sk, key, segment, region, status, valid_from, valid_to,
                 is_current, "test", STAMP]
            )
        )
    return "\n".join(lines) + "\n"


def _current(rows: list[tuple[str, ...]]) -> str:
    lines = [CURRENT_HEADER]
    for key, segment, region, status in rows:
        lines.append(",".join([key, segment, region, status, STAMP]))
    return "\n".join(lines) + "\n"


class TriageTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(
        self,
        events: list[dict],
        history_rows: list[tuple[str, ...]],
        current_rows: list[tuple[str, ...]],
    ):
        support.install_fixtures(self.workspace)
        support.write_events(
            self.workspace / "data/events/change_events.jsonl", events
        )
        (self.workspace / "data/dimension/member_history.csv").write_text(
            _history(history_rows), encoding="utf-8"
        )
        (self.workspace / "data/dimension/member_current.csv").write_text(
            _current(current_rows), encoding="utf-8"
        )
        return run_pipeline(self.workspace, now=support.NOW)

    @staticmethod
    def _finding(result, code: str, key: str = ""):
        for finding in result.findings:
            if finding.code == code and (not key or finding.natural_key == key):
                return finding
        raise AssertionError(f"finding {code} for {key!r} not found")

    @staticmethod
    def _codes(result, key: str) -> list[str]:
        return sorted(
            finding.code for finding in result.findings
            if finding.natural_key == key
        )

    @staticmethod
    def _group_for(view, root_finding_id: str):
        for group in view.groups:
            if group.root_finding_id == root_finding_id:
                return group
        raise AssertionError(f"group rooted at {root_finding_id} not found")

    @staticmethod
    def _derived_ids(group) -> set[str]:
        return {item.finding_id for item in group.derived}


class CrossIntervalPropagationTests(TriageTestBase):
    """跨区间传播: one broken row inflates key-level current-state findings."""

    def _scenario(self):
        events = [
            support.event("EVT-1", "M-X", "2024-01-01T00:00:00Z", 1,
                          segment="Bronze"),
            support.event("EVT-2", "M-X", "2024-06-01T00:00:00Z", 2,
                          segment="Silver"),
        ]
        history = [
            ("SK-A", "M-X", "Bronze", "APAC", "Active",
             "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z", "false"),
            ("SK-B", "M-X", "Gold", "APAC", "Active",
             "2024-06-01T00:00:00Z", "2024-05-01T00:00:00Z", "true"),
            ("SK-C", "M-X", "Silver", "APAC", "Active",
             "2024-06-01T00:00:00Z", "", "true"),
        ]
        current = [("M-X", "Silver", "APAC", "Active")]
        return self._run(events, history, current)

    def test_raw_findings_are_all_kept(self) -> None:
        result = self._scenario()
        self.assertEqual(
            self._codes(result, "M-X"),
            ["DIM_CURRENT_FLAG", "DIM_INVALID_INTERVAL", "DIM_MULTI_CURRENT"],
        )
        self.assertEqual(len(result.plan), len(result.findings))

    def test_flag_and_multi_current_are_derived_symptoms(self) -> None:
        result = self._scenario()
        view = result.triage
        self.assertEqual(len(view.groups), 1)
        root = self._finding(result, "DIM_INVALID_INTERVAL", "M-X")
        group = self._group_for(view, root.finding_id)
        self.assertEqual(group.untrustworthy_rows, (3,))
        self.assertEqual(group.proof_rows, (2, 3, 4))
        flag = self._finding(result, "DIM_CURRENT_FLAG", "M-X")
        multi = self._finding(result, "DIM_MULTI_CURRENT", "M-X")
        self.assertEqual(
            self._derived_ids(group), {flag.finding_id, multi.finding_id}
        )
        for item in group.derived:
            self.assertEqual(item.method, "counterfactual_single_removal")
            self.assertEqual(item.removed_rows, (3,))
            self.assertEqual(item.remaining_rows, (2, 4))
        self.assertEqual(view.unattributed, ())
        summary = view.to_dict()
        self.assertEqual(summary["findings_total"], 3)
        self.assertTrue(summary["partition_complete"])
        self.assertFalse(summary["mutates_history"])
        self.assertFalse(summary["auto_repair"])

    def test_plan_references_still_resolve(self) -> None:
        result = self._scenario()
        finding_ids = {finding.finding_id for finding in result.findings}
        for action in result.plan:
            self.assertEqual(len(action.finding_ids), 1)
            self.assertIn(action.finding_ids[0], finding_ids)


class DuplicateMultiplicityTests(TriageTestBase):
    """重复行多重性: removing one bad row must not swallow real duplicates."""

    def _scenario(self):
        events = [
            support.event("EVT-1", "M-X", "2024-01-01T00:00:00Z", 1,
                          segment="Bronze"),
            support.event("EVT-2", "M-Y", "2024-02-01T00:00:00Z", 1,
                          segment="Silver"),
        ]
        history = [
            ("SK-A", "M-X", "Bronze", "APAC", "Active",
             "2024-01-01T00:00:00Z", "", "true"),
            ("SK-B", "M-X", "Bronze", "APAC", "Active",
             "2024-01-01T00:00:00Z", "", "true"),
            ("SK-C", "M-X", "Bronze", "APAC", "Active",
             "2024-01-01T00:00:00Z", "2023-12-01T00:00:00Z", "false"),
            ("SK-D", "M-Y", "Silver", "APAC", "Active",
             "2024-02-01T00:00:00Z", "", "true"),
            ("SK-E", "M-Y", "Silver", "APAC", "Active",
             "2024-02-01T00:00:00Z", "2024-01-01T00:00:00Z", "false"),
        ]
        current = [
            ("M-X", "Bronze", "APAC", "Active"),
            ("M-Y", "Silver", "APAC", "Active"),
        ]
        return self._run(events, history, current)

    def test_legitimate_duplicate_survives_untrustworthy_removal(self) -> None:
        result = self._scenario()
        view = result.triage
        duplicate = self._finding(result, "DIM_DUPLICATE_VERSION", "M-X")
        # Multiplicity evidence is preserved: three rows share the signature.
        self.assertEqual(duplicate.details["row_nos"], [2, 3, 4])
        derived = {
            item.finding_id
            for group in view.groups
            for item in group.derived
        }
        # Counterexample guard: the duplicate, the multi-current, the overlap
        # and the snapshot drift all still hold without row 4, so none of
        # them may be swallowed into the root-cause group.
        for code in (
            "DIM_DUPLICATE_VERSION",
            "DIM_MULTI_CURRENT",
            "DIM_OVERLAP",
            "DIM_CURRENT_SNAPSHOT_DRIFT",
        ):
            finding = self._finding(result, code, "M-X")
            self.assertNotIn(finding.finding_id, derived, code)
            self.assertIn(finding.finding_id, view.unattributed, code)
        root_x = self._finding(result, "DIM_INVALID_INTERVAL", "M-X")
        self.assertEqual(self._derived_ids(self._group_for(view, root_x.finding_id)), set())

    def test_pure_untrustworthy_duplicate_is_derived(self) -> None:
        result = self._scenario()
        view = result.triage
        root_y = self._finding(result, "DIM_INVALID_INTERVAL", "M-Y")
        group = self._group_for(view, root_y.finding_id)
        duplicate = self._finding(result, "DIM_DUPLICATE_VERSION", "M-Y")
        self.assertEqual(duplicate.details["row_nos"], [5, 6])
        self.assertEqual(self._derived_ids(group), {duplicate.finding_id})
        item = group.derived[0]
        self.assertEqual(item.method, "counterfactual_single_removal")
        self.assertEqual(item.removed_rows, (6,))
        self.assertEqual(item.remaining_rows, (5,))


class MixedValidInvalidHistoryTests(TriageTestBase):
    """混合合法/非法历史: invalid rows must neither alibi nor mask legit ones."""

    def _scenario(self):
        events = [
            support.event("EVT-1", "M-X", "2024-01-01T00:00:00Z", 1,
                          segment="Bronze", region="APAC"),
            support.event("EVT-2", "M-X", "2024-06-01T00:00:00Z", 2,
                          segment="Silver", region="APAC"),
        ]
        history = [
            ("SK-A", "M-X", "Bronze", "APAC", "Active",
             "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z", "false"),
            ("SK-B", "M-X", "Bronze", "APAC", "Active",
             "2024-03-01T00:00:00Z", "2024-09-01T00:00:00Z", "false"),
            # Invalid interval whose attributes match the expected v2: it
            # must not "prove" the legitimate history correct.
            ("SK-C", "M-X", "Silver", "APAC", "Active",
             "2024-06-01T00:00:00Z", "2024-05-01T00:00:00Z", "false"),
            ("SK-D", "M-X", "Silver", "EMEA", "Active",
             "2024-06-01T00:00:00Z", "", "true"),
        ]
        current = [("M-X", "Silver", "EMEA", "Active")]
        return self._run(events, history, current)

    def test_invalid_row_attributes_do_not_alibi_legitimate_drift(self) -> None:
        result = self._scenario()
        drift = self._finding(result, "DIM_ATTR_DRIFT", "M-X")
        # The drift evidence comes from the legitimate row 5, not row 4.
        self.assertEqual(
            drift.details["differences"]["region"],
            {"expected": "APAC", "actual": "EMEA"},
        )
        self.assertEqual(drift.details["evidence_row_no"], 5)
        self.assertEqual(drift.details["row_nos"], [4, 5])

    def test_legit_overlap_and_drift_stay_independent(self) -> None:
        result = self._scenario()
        view = result.triage
        overlap = self._finding(result, "DIM_OVERLAP", "M-X")
        self.assertEqual(overlap.details["row_nos"], [2, 3])
        self.assertEqual(overlap.start, "2024-03-01T00:00:00Z")
        self.assertEqual(overlap.end, "2024-06-01T00:00:00Z")
        drift = self._finding(result, "DIM_ATTR_DRIFT", "M-X")
        derived = {
            item.finding_id
            for group in view.groups
            for item in group.derived
        }
        self.assertEqual(derived, set())
        self.assertIn(overlap.finding_id, view.unattributed)
        self.assertIn(drift.finding_id, view.unattributed)
        root = self._finding(result, "DIM_INVALID_INTERVAL", "M-X")
        group = self._group_for(view, root.finding_id)
        self.assertEqual(group.proof_rows, (2, 3, 4, 5))


class JointRemovalTests(TriageTestBase):
    """A violation explained only by the combined untrustworthy rows."""

    def _scenario(self):
        events = [
            support.event("EVT-1", "M-Z", "2024-03-01T00:00:00Z", 1,
                          segment="Bronze"),
        ]
        history = [
            ("SK-P", "M-Z", "Bronze", "APAC", "Active",
             "2024-01-01T00:00:00Z", "2023-12-01T00:00:00Z", "true"),
            ("SK-Q", "M-Z", "Bronze", "APAC", "Active",
             "2024-02-01T00:00:00Z", "2024-01-01T00:00:00Z", "true"),
            ("SK-R", "M-Z", "Bronze", "APAC", "Active",
             "2024-03-01T00:00:00Z", "", "true"),
        ]
        current = [("M-Z", "Bronze", "APAC", "Active")]
        return self._run(events, history, current)

    def test_multi_current_needs_joint_removal(self) -> None:
        result = self._scenario()
        view = result.triage
        self.assertEqual(len(view.groups), 2)
        flags = [
            finding
            for finding in result.findings
            if finding.code == "DIM_CURRENT_FLAG"
        ]
        self.assertEqual(len(flags), 2)
        multi = self._finding(result, "DIM_MULTI_CURRENT", "M-Z")
        roots = [
            self._finding(result, "DIM_INVALID_INTERVAL", "M-Z"),
        ]
        invalid = [
            finding
            for finding in result.findings
            if finding.code == "DIM_INVALID_INTERVAL"
        ]
        self.assertEqual(len(invalid), 2)
        first, second = sorted(invalid, key=lambda item: item.details["row_no"])
        group_first = self._group_for(view, first.finding_id)
        group_second = self._group_for(view, second.finding_id)
        flag_first = next(
            item for item in flags if item.details["row_no"] == 2
        )
        flag_second = next(
            item for item in flags if item.details["row_no"] == 3
        )
        self.assertEqual(
            self._derived_ids(group_first),
            {flag_first.finding_id, multi.finding_id},
        )
        self.assertEqual(
            self._derived_ids(group_second), {flag_second.finding_id}
        )
        joint = next(
            item for item in group_first.derived
            if item.finding_id == multi.finding_id
        )
        self.assertEqual(joint.method, "counterfactual_joint_removal")
        self.assertEqual(joint.removed_rows, (2, 3))
        self.assertEqual(joint.remaining_rows, (4,))
        self.assertEqual(roots[0].finding_id, first.finding_id)
        self.assertTrue(view.to_dict()["partition_complete"])


class TriageViewContractTests(TriageTestBase):
    """Verifiability, scoped re-diagnosis and report integration."""

    def _scenario(self):
        events = [
            support.event("EVT-1", "M-X", "2024-01-01T00:00:00Z", 1,
                          segment="Bronze"),
            support.event("EVT-2", "M-X", "2024-06-01T00:00:00Z", 2,
                          segment="Silver"),
            support.event("EVT-3", "M-CLEAN", "2024-01-01T00:00:00Z", 1,
                          segment="Bronze"),
        ]
        history = [
            ("SK-A", "M-X", "Bronze", "APAC", "Active",
             "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z", "false"),
            ("SK-B", "M-X", "Gold", "APAC", "Active",
             "2024-06-01T00:00:00Z", "2024-05-01T00:00:00Z", "true"),
            ("SK-C", "M-X", "Silver", "APAC", "Active",
             "2024-06-01T00:00:00Z", "", "true"),
            ("SK-L1", "M-CLEAN", "Bronze", "APAC", "Active",
             "2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z", "false"),
            ("SK-L2", "M-CLEAN", "Bronze", "APAC", "Active",
             "2024-03-01T00:00:00Z", "", "true"),
        ]
        current = [
            ("M-X", "Silver", "APAC", "Active"),
            ("M-CLEAN", "Bronze", "APAC", "Active"),
        ]
        return self._run(events, history, current)

    def test_every_findings_is_accounted_for_exactly_once(self) -> None:
        result = self._scenario()
        view_dict = result.triage.to_dict()
        root_ids = {group["root_finding_id"] for group in view_dict["groups"]}
        derived_ids = {
            item["finding_id"]
            for group in view_dict["groups"]
            for item in group["derived_findings"]
        }
        unattributed = set(view_dict["unattributed_findings"])
        all_ids = {finding.finding_id for finding in result.findings}
        self.assertFalse(root_ids & derived_ids)
        self.assertFalse(root_ids & unattributed)
        self.assertFalse(derived_ids & unattributed)
        self.assertEqual(root_ids | derived_ids | unattributed, all_ids)
        self.assertEqual(view_dict["findings_total"], len(result.findings))
        self.assertTrue(view_dict["partition_complete"])
        self.assertEqual(view_dict["kind"], "review_grouping")
        # The clean key's overlap is real and stays unattributed.
        clean_overlap = self._finding(result, "DIM_OVERLAP", "M-CLEAN")
        self.assertIn(clean_overlap.finding_id, unattributed)

    def test_rediagnosis_is_scoped_to_rows_of_affected_keys(self) -> None:
        result = self._scenario()
        calls: list[str] = []
        real_diagnose_key = triage.diagnose_key

        def spy(contract, key, expected_rows, rows):
            calls.append(key)
            return real_diagnose_key(contract, key, expected_rows, rows)

        with mock.patch.object(triage, "diagnose_key", side_effect=spy):
            view = triage_findings(
                result.contract,
                result.expected,
                result.actual,
                result.current_rows,
                result.findings,
            )
        # Only the key with untrustworthy rows is re-diagnosed; with a single
        # untrustworthy row the joint removal IS the single removal, so the
        # cache serves both — one scoped call, never a full-repo re-run.
        self.assertEqual(sorted(set(calls)), ["M-X"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(view.to_dict(), result.triage.to_dict())

    def test_report_ledger_and_rendering_expose_the_view(self) -> None:
        result = self._scenario()
        paths = write_reports(self.workspace, result)
        report = json.loads(paths["json"].read_text(encoding="utf-8"))
        triage_dict = report["triage"]
        self.assertEqual(triage_dict["kind"], "review_grouping")
        self.assertEqual(triage_dict["findings_total"], len(result.findings))
        self.assertEqual(len(triage_dict["groups"]), 1)
        self.assertEqual(triage_dict["derived_count"], 2)
        # Raw findings and plan are untouched by the grouping.
        self.assertEqual(len(report["findings"]), len(result.findings))
        self.assertEqual(len(report["plan"]), len(result.findings))
        with Ledger(self.workspace / "ledger" / "dimhist.sqlite3") as ledger:
            self.assertEqual(ledger.count("triage_groups"), 1)
            self.assertEqual(ledger.count("findings"), len(result.findings))
        markdown = render_markdown(build_report(result))
        self.assertIn("复核分组 · Review triage", markdown)
        self.assertIn("不删除任何发现", markdown)
        html_text = render_html(build_report(result))
        self.assertIn("Review triage", html_text)
        self.assertIn("TRI-0001", html_text)

    def test_fixture_run_has_no_root_causes(self) -> None:
        support.install_fixtures(self.workspace)
        result = run_pipeline(self.workspace, now=support.NOW)
        view = result.triage.to_dict()
        self.assertEqual(view["groups"], [])
        self.assertEqual(view["root_cause_count"], 0)
        self.assertEqual(view["derived_count"], 0)
        self.assertEqual(view["unattributed_count"], len(result.findings))
        self.assertTrue(view["partition_complete"])


if __name__ == "__main__":
    unittest.main()
