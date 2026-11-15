"""Temporal hierarchy diagnostics: cycles, dangling parents, ambiguity, budgets."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.config import load_contract
from dimhist.hierarchy import diagnose_temporal_hierarchy
from dimhist.ledger import Ledger
from dimhist.model import ActualVersion
from dimhist.pipeline import init_workspace, run_pipeline
from dimhist.report import build_report, build_summary
from dimhist.util import DimhistError, sha256_file

JAN = "2024-01-01T00:00:00Z"
JAN15 = "2024-01-15T00:00:00Z"
FEB = "2024-02-01T00:00:00Z"
FEB15 = "2024-02-15T00:00:00Z"
MAR = "2024-03-01T00:00:00Z"

PARENT_OVERRIDES = {
    "tracked_attributes": ["segment", "region", "status", "parent_id"],
    "attribute_types": {
        "segment": "string",
        "region": "string",
        "status": "string",
        "parent_id": "string",
    },
    "parent_key_attribute": "parent_id",
}


def make_contract(directory: Path, **overrides) -> object:
    directory.mkdir(parents=True, exist_ok=True)
    document = dict(PARENT_OVERRIDES)
    document.update(overrides)
    return load_contract(support.write_contract(directory, **document))


def row(row_no: int, key: str, parent: str, start: str, end: str | None) -> ActualVersion:
    return ActualVersion(
        row_no=row_no,
        member_sk=f"SK-{key}-{row_no}",
        natural_key=key,
        attrs={
            "segment": "Bronze",
            "region": "APAC",
            "status": "Active",
            "parent_id": parent,
        },
        valid_from=start,
        valid_to=end,
        is_current=end is None,
        source="test",
        updated_at="",
    )


class TemporalHierarchyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.contract = make_contract(self.root / "contract")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def findings(self, actual: list[ActualVersion], **kwargs):
        return diagnose_temporal_hierarchy(self.contract, actual, **kwargs)

    def by_code(self, actual: list[ActualVersion], **kwargs) -> dict[str, list]:
        grouped: dict[str, list] = {}
        for finding in self.findings(actual, **kwargs):
            grouped.setdefault(finding.code, []).append(finding)
        return grouped

    def test_cross_period_edges_do_not_form_a_cycle(self) -> None:
        actual = [
            row(1, "A", "B", JAN, FEB),
            row(2, "A", "", FEB, MAR),
            row(3, "B", "", JAN, FEB),
            row(4, "B", "A", FEB, MAR),
        ]
        # A static union of all edges would show A <-> B; temporally they
        # never co-exist, so no hierarchy finding may be raised at all.
        self.assertEqual(self.findings(actual), [])

    def test_brief_true_cycle_reports_exact_common_interval(self) -> None:
        actual = [
            row(1, "A", "B", JAN, MAR),
            row(2, "B", "", JAN, FEB),
            row(3, "B", "A", FEB, FEB15),
            row(4, "B", "", FEB15, MAR),
        ]
        cycles = self.by_code(actual)["DIM_HIER_CYCLE"]
        self.assertEqual(len(cycles), 1)
        cycle = cycles[0]
        self.assertEqual(cycle.start, FEB)
        self.assertEqual(cycle.end, FEB15)
        self.assertEqual(cycle.severity, "high")
        self.assertEqual(cycle.details["certainty"], "necessary")
        self.assertEqual(cycle.details["cycle_keys"], ["A", "B"])
        self.assertEqual(cycle.details["resolution_counts"], [[1, 1]])
        sks = {
            item["member_sk"]
            for edge in cycle.details["edges"]
            for item in edge["child_rows"] + edge["parent_rows"]
        }
        self.assertEqual(sks, {"SK-A-1", "SK-B-3"})

    def test_cycle_merges_across_unrelated_state_changes(self) -> None:
        actual = [
            row(1, "A", "B", JAN, MAR),
            row(2, "B", "A", JAN, MAR),
            row(3, "R", "", JAN, FEB),
            row(4, "R", "", FEB, MAR),
        ]
        cycles = self.by_code(actual)["DIM_HIER_CYCLE"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].start, JAN)
        self.assertEqual(cycles[0].end, MAR)
        self.assertEqual(cycles[0].details["resolution_counts"], [[1, 1], [1, 1]])

    def test_overlapping_versions_necessary_cycle_lists_all_rows(self) -> None:
        actual = [
            row(1, "A", "B", JAN, MAR),
            row(2, "A", "B", JAN15, FEB15),
            row(3, "B", "A", JAN, MAR),
        ]
        cycles = self.by_code(actual)["DIM_HIER_CYCLE"]
        self.assertEqual(len(cycles), 1)
        cycle = cycles[0]
        self.assertEqual((cycle.start, cycle.end), (JAN, MAR))
        self.assertEqual(cycle.details["certainty"], "necessary")
        child_rows = {
            item["member_sk"]
            for edge in cycle.details["edges"]
            for item in edge["child_rows"]
        }
        # Both overlapping A rows are evidence; none is picked arbitrarily.
        self.assertEqual(child_rows, {"SK-A-1", "SK-A-2", "SK-B-3"})

    def test_overlapping_versions_possible_cycle_and_ambiguity(self) -> None:
        actual = [
            row(1, "A", "B", JAN, MAR),
            row(2, "A", "C", JAN, MAR),
            row(3, "B", "A", JAN, MAR),
            row(4, "C", "", JAN, MAR),
        ]
        grouped = self.by_code(actual)
        cycles = grouped["DIM_HIER_CYCLE"]
        self.assertEqual(len(cycles), 1)
        cycle = cycles[0]
        self.assertEqual(cycle.details["certainty"], "possible")
        self.assertEqual(cycle.details["cycle_keys"], ["A", "B"])
        self.assertEqual(cycle.details["resolution_counts"], [[1, 2]])
        self.assertEqual(cycle.severity, "medium")
        ambiguity = grouped["DIM_HIER_AMBIGUOUS_PARENT"]
        self.assertEqual(len(ambiguity), 1)
        self.assertEqual(ambiguity[0].natural_key, "A")
        self.assertEqual(ambiguity[0].details["parent_values"], ["B", "C"])
        self.assertNotIn("necessary", {c.details["certainty"] for c in cycles})

    def test_necessary_violation_with_differing_variants(self) -> None:
        actual = [
            row(1, "A", "B", JAN, FEB),
            row(2, "A", "C", JAN, FEB),
            row(3, "B", "A", JAN, FEB),
            row(4, "C", "A", JAN, FEB),
        ]
        cycles = self.by_code(actual)["DIM_HIER_CYCLE"]
        self.assertEqual(len(cycles), 3)
        possible = [c for c in cycles if c.details["certainty"] == "possible"]
        necessary = [c for c in cycles if c.details["certainty"] == "necessary"]
        self.assertEqual(len(possible), 2)
        self.assertEqual(
            sorted(c.details["cycle_keys"] for c in possible), [["A", "B"], ["A", "C"]]
        )
        self.assertEqual(len(necessary), 1)
        self.assertEqual(necessary[0].details["variants"], [["A", "B"], ["A", "C"]])
        self.assertEqual(necessary[0].details["resolution_counts"], [[2, 2]])

    def test_dangling_parent_necessary_and_possible(self) -> None:
        actual = [
            row(1, "D", "MISSING", JAN, FEB),
            row(2, "E", "GONE", JAN, FEB),
            row(3, "E", "", JAN, FEB),
        ]
        grouped = self.by_code(actual)
        dangling = {
            finding.natural_key: finding
            for finding in grouped["DIM_HIER_DANGLING_PARENT"]
        }
        self.assertEqual(dangling["D"].details["certainty"], "necessary")
        self.assertEqual(dangling["D"].details["parent_key"], "MISSING")
        self.assertEqual(dangling["D"].severity, "medium")
        self.assertEqual(dangling["E"].details["certainty"], "possible")
        self.assertEqual(dangling["E"].details["parent_key"], "GONE")
        self.assertEqual(dangling["E"].severity, "low")
        self.assertEqual(
            [row["member_sk"] for row in dangling["D"].details["child_rows"]],
            ["SK-D-1"],
        )
        self.assertEqual(grouped["DIM_HIER_AMBIGUOUS_PARENT"][0].natural_key, "E")

    def test_budget_exhaustion_reports_undecided_instead_of_silence(self) -> None:
        actual = [
            row(1, "K1", "K2", JAN, FEB),
            row(2, "K1", "X1", JAN, FEB),
            row(3, "K2", "K3", JAN, FEB),
            row(4, "K2", "X2", JAN, FEB),
            row(5, "K3", "K1", JAN, FEB),
            row(6, "K3", "X3", JAN, FEB),
            row(7, "X1", "", JAN, FEB),
            row(8, "X2", "", JAN, FEB),
            row(9, "X3", "", JAN, FEB),
        ]
        limited = self.by_code(actual, max_resolutions=4)
        undecided = limited["DIM_HIER_UNDECIDED"]
        self.assertEqual(len(undecided), 1)
        self.assertEqual(undecided[0].start, JAN)
        self.assertEqual(undecided[0].end, FEB)
        self.assertEqual(undecided[0].details["combinations"], 8)
        self.assertEqual(undecided[0].details["budget"], 4)
        self.assertEqual(undecided[0].details["component_keys"], ["K1", "K2", "K3"])
        self.assertNotIn("DIM_HIER_CYCLE", limited)
        self.assertEqual(len(limited["DIM_HIER_AMBIGUOUS_PARENT"]), 3)
        full = self.by_code(actual, max_resolutions=16)
        self.assertNotIn("DIM_HIER_UNDECIDED", full)
        cycles = full["DIM_HIER_CYCLE"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].details["certainty"], "possible")
        self.assertEqual(cycles[0].details["cycle_keys"], ["K1", "K2", "K3"])
        self.assertEqual(cycles[0].details["resolution_counts"], [[1, 8]])

    def test_self_cycle_and_open_interval(self) -> None:
        actual = [row(1, "A", "A", JAN, None)]
        cycles = self.by_code(actual)["DIM_HIER_CYCLE"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].details["cycle_keys"], ["A"])
        self.assertEqual(cycles[0].details["certainty"], "necessary")
        self.assertIsNone(cycles[0].end)

    def test_empty_parent_marks_root_and_yields_no_findings(self) -> None:
        actual = [row(1, "A", "", JAN, FEB), row(2, "B", "", JAN, FEB)]
        self.assertEqual(self.findings(actual), [])

    def test_contract_without_parent_attribute_skips_hierarchy(self) -> None:
        contract = load_contract(support.CONTRACT_PATH)
        actual = [row(1, "A", "B", JAN, FEB), row(2, "B", "A", JAN, FEB)]
        self.assertEqual(diagnose_temporal_hierarchy(contract, actual), [])

    def test_contract_parent_attribute_validation(self) -> None:
        self.assertEqual(self.contract.parent_key_attribute, "parent_id")
        cases = [
            {"parent_key_attribute": "missing"},
            {"parent_key_attribute": ""},
            {"parent_key_attribute": 123},
            {
                "tracked_attributes": ["segment", "region", "status", "level"],
                "attribute_types": {
                    "segment": "string",
                    "region": "string",
                    "status": "string",
                    "level": "integer",
                },
                "parent_key_attribute": "level",
            },
        ]
        for index, overrides in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(DimhistError) as caught:
                    make_contract(self.root / f"bad-{index}", **overrides)
                self.assertEqual(caught.exception.code, "CFG_009")


class TemporalHierarchyPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _event(self, event_id: str, key: str, at: str, sequence: int, parent: str):
        return {
            "event_id": event_id,
            "member_id": key,
            "effective_at": at,
            "sequence": sequence,
            "payload": {
                "segment": "Bronze",
                "region": "APAC",
                "status": "Active",
                "parent_id": parent,
            },
        }

    def _install_workspace(self) -> None:
        init_workspace(self.workspace)
        document = support.fixture_contract()
        document.update(PARENT_OVERRIDES)
        (self.workspace / "contracts" / "dimension_member.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        events = [
            self._event("EVT-A1", "A", JAN, 1, ""),
            self._event("EVT-A2", "A", FEB, 2, "B"),
            self._event("EVT-A3", "A", MAR, 3, ""),
            self._event("EVT-B1", "B", JAN, 1, ""),
            self._event("EVT-B2", "B", FEB, 2, "A"),
            self._event("EVT-B3", "B", MAR, 3, ""),
            self._event("EVT-D1", "D", JAN, 1, "MISSING"),
            self._event("EVT-O1", "O", JAN, 1, ""),
        ]
        support.write_events(self.workspace / "data/events/change_events.jsonl", events)
        header = (
            "member_sk,member_id,segment,region,status,parent_id,"
            "valid_from,valid_to,is_current,source,updated_at"
        )
        history_rows = [
            "SK-A-01,A,Bronze,APAC,Active,,2024-01-01T00:00:00Z,2024-02-01T00:00:00Z,false,test,2024-01-01T00:00:00Z",
            "SK-A-02,A,Bronze,APAC,Active,B,2024-02-01T00:00:00Z,2024-03-01T00:00:00Z,false,test,2024-02-01T00:00:00Z",
            "SK-A-03,A,Bronze,APAC,Active,,2024-03-01T00:00:00Z,,true,test,2024-03-01T00:00:00Z",
            "SK-B-01,B,Bronze,APAC,Active,,2024-01-01T00:00:00Z,2024-02-01T00:00:00Z,false,test,2024-01-01T00:00:00Z",
            "SK-B-02,B,Bronze,APAC,Active,A,2024-02-01T00:00:00Z,2024-03-01T00:00:00Z,false,test,2024-02-01T00:00:00Z",
            "SK-B-03,B,Bronze,APAC,Active,,2024-03-01T00:00:00Z,,true,test,2024-03-01T00:00:00Z",
            "SK-D-01,D,Bronze,APAC,Active,MISSING,2024-01-01T00:00:00Z,,true,test,2024-01-01T00:00:00Z",
            "SK-O-01,O,Bronze,APAC,Active,,2024-01-01T00:00:00Z,,true,test,2024-01-01T00:00:00Z",
            "SK-O-02,O,Bronze,APAC,Active,,2024-01-01T00:00:00Z,,true,test,2024-01-01T00:00:00Z",
        ]
        (self.workspace / "data/dimension/member_history.csv").write_text(
            header + "\n" + "\n".join(history_rows) + "\n", encoding="utf-8"
        )
        current_rows = [
            "A,Bronze,APAC,Active,,2024-03-01T00:00:00Z",
            "B,Bronze,APAC,Active,,2024-03-01T00:00:00Z",
            "D,Bronze,APAC,Active,MISSING,2024-01-01T00:00:00Z",
            "O,Bronze,APAC,Active,,2024-01-01T00:00:00Z",
            "O,Bronze,APAC,Active,,2024-01-01T00:00:00Z",
        ]
        (self.workspace / "data/dimension/member_current.csv").write_text(
            "member_id,segment,region,status,parent_id,updated_at\n"
            + "\n".join(current_rows)
            + "\n",
            encoding="utf-8",
        )

    def test_pipeline_wires_hierarchy_into_ledger_report_and_plan(self) -> None:
        self._install_workspace()
        history_path = self.workspace / "data/dimension/member_history.csv"
        events_path = self.workspace / "data/events/change_events.jsonl"
        history_hash = sha256_file(history_path)
        events_hash = sha256_file(events_path)
        result = run_pipeline(self.workspace, now=support.NOW)

        grouped: dict[str, list] = {}
        for finding in result.findings:
            grouped.setdefault(finding.code, []).append(finding)
        # Relational findings are produced alongside the structural ones.
        cycles = grouped["DIM_HIER_CYCLE"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual((cycles[0].start, cycles[0].end), (FEB, MAR))
        self.assertEqual(cycles[0].details["certainty"], "necessary")
        evidence_sks = {
            item["member_sk"]
            for edge in cycles[0].details["edges"]
            for item in edge["child_rows"]
        }
        self.assertEqual(evidence_sks, {"SK-A-02", "SK-B-02"})
        dangling = grouped["DIM_HIER_DANGLING_PARENT"]
        self.assertEqual(len(dangling), 1)
        self.assertEqual(dangling[0].natural_key, "D")
        self.assertEqual(dangling[0].details["parent_key"], "MISSING")
        # Existing single-key structural findings are preserved.
        self.assertIn("DIM_OVERLAP", grouped)
        self.assertIn("DIM_MULTI_CURRENT", grouped)

        # Findings flow into the SQLite ledger and the dry-run plan.
        with Ledger(self.workspace / "ledger" / "dimhist.sqlite3") as ledger:
            self.assertEqual(ledger.count("findings"), len(result.findings))
            self.assertEqual(ledger.count("plan_actions"), len(result.plan))
            stored = {
                row["code"] for row in ledger.rows("findings")
            }
        self.assertIn("DIM_HIER_CYCLE", stored)
        self.assertIn("DIM_HIER_DANGLING_PARENT", stored)
        plan_kinds = {action.kind for action in result.plan}
        self.assertIn("BREAK_HIERARCHY_CYCLE", plan_kinds)
        self.assertIn("RESOLVE_DANGLING_PARENT", plan_kinds)
        for action in result.plan:
            self.assertTrue(action.review_required)
            self.assertEqual(action.details["execution_mode"], "dry_run_only")

        # The report summary and document carry the relational findings.
        summary = build_summary(result)
        self.assertEqual(summary["findings_by_code"]["DIM_HIER_CYCLE"], 1)
        self.assertEqual(summary["findings_by_code"]["DIM_HIER_DANGLING_PARENT"], 1)
        report = build_report(result)
        reported = [
            item for item in report["findings"] if item["code"] == "DIM_HIER_CYCLE"
        ]
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["interval"], {"from": FEB, "to": MAR})
        self.assertEqual(reported[0]["details"]["parent_attribute"], "parent_id")

        # Source inputs are read-only: hashes are unchanged after the run.
        self.assertEqual(sha256_file(history_path), history_hash)
        self.assertEqual(sha256_file(events_path), events_hash)


if __name__ == "__main__":
    unittest.main()
