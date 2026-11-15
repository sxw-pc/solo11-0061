"""In-memory repair-plan simulation: preconditions, dependencies, rollback."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.config import load_contract
from dimhist.model import ActualVersion, ExpectedVersion
from dimhist.pipeline import run_pipeline, run_plan_simulation
from dimhist.simulate import rollback_state, row_fingerprint, simulate_repairs
from dimhist.util import DimhistError, canonical_json, sha256_file

NOW = "2026-01-15T00:00:00Z"


def ts(day: str) -> str:
    return f"2024-{day}T00:00:00Z"


def make_row(row_no, sk, segment, valid_from, valid_to, is_current):
    return ActualVersion(
        row_no=row_no,
        member_sk=sk,
        natural_key="M-1",
        attrs={"segment": segment, "region": "APAC", "status": "Active"},
        valid_from=valid_from,
        valid_to=valid_to,
        is_current=is_current,
        source="test",
        updated_at=ts("01-01"),
    )


def multiset(rows) -> list[str]:
    return sorted(canonical_json(row.to_dict()) for row in rows)


def step(op, targets, params=None, **extra):
    return {"op": op, "targets": targets, "params": params or {}, **extra}


def target(ref, row):
    return {"row": ref, "fingerprint": row_fingerprint(row)}


class SimulationUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_contract(support.CONTRACT_PATH)
        bronze = {"segment": "Bronze", "region": "APAC", "status": "Active"}
        gold = {"segment": "Gold", "region": "APAC", "status": "Active"}
        silver = {"segment": "Silver", "region": "APAC", "status": "Active"}
        self.expected = [
            ExpectedVersion("M-1", 1, bronze, ts("01-01"), ts("03-01")),
            ExpectedVersion("M-1", 2, gold, ts("03-01"), ts("06-01")),
            ExpectedVersion("M-1", 3, silver, ts("06-01"), None),
        ]
        self.actual = [
            make_row(1, "SK-1", "Bronze", ts("01-01"), ts("06-01"), False),
            make_row(2, "SK-2", "Silver", ts("06-01"), None, True),
        ]
        self.before_multiset = multiset(self.actual)

    def simulate(self, request):
        return simulate_repairs(self.contract, self.expected, self.actual, NOW, request)

    def test_split_then_update_uses_derived_dependency(self) -> None:
        half = make_row(0, "SK-1~SIM-0001", "Bronze", ts("03-01"), ts("06-01"), False)
        request = {"steps": [
            step("split_interval", [target(1, self.actual[0])], {"at": ts("03-01")}),
            step("update_attributes", [target("SIM-0001#2", half)], {"set": {"segment": "Gold"}}),
        ]}
        outcome = self.simulate(request)
        self.assertEqual(outcome.order, ["SIM-0001", "SIM-0002"])
        self.assertEqual(outcome.applied[1].depends_on, ("SIM-0001",))
        final = {row.valid_from: row for row in outcome.state.values()}
        self.assertEqual(final[ts("03-01")].attrs["segment"], "Gold")
        drift = [check for check in outcome.asof_before if check.status != "match"]
        self.assertEqual([(c.natural_key, c.at) for c in drift], [("M-1", ts("03-01"))])
        self.assertEqual([c for c in outcome.asof_after if c.status != "match"], [])

    def test_dependency_order_beats_declaration_order(self) -> None:
        half = make_row(0, "SK-1~SPLIT", "Bronze", ts("03-01"), ts("06-01"), False)
        request = {"steps": [
            step("update_attributes", [target("SPLIT#2", half)],
                 {"set": {"segment": "Gold"}}, id="FIX"),
            step("split_interval", [target(1, self.actual[0])], {"at": ts("03-01")}, id="SPLIT"),
        ]}
        outcome = self.simulate(request)
        self.assertEqual(outcome.order, ["SPLIT", "FIX"])
        final = {row.valid_from: row for row in outcome.state.values()}
        self.assertEqual(final[ts("03-01")].attrs["segment"], "Gold")

    def test_contradictory_targets_are_rejected_not_overwritten(self) -> None:
        first = step("update_attributes", [target(1, self.actual[0])], {"set": {"segment": "Gold"}})
        second = step("update_attributes", [target(1, self.actual[0])], {"set": {"segment": "Platinum"}})
        with self.assertRaises(DimhistError) as ctx:
            self.simulate({"steps": [first, second]})
        self.assertEqual(ctx.exception.code, "SIM_004")
        split = step("split_interval", [target(1, self.actual[0])], {"at": ts("03-01")})
        with self.assertRaises(DimhistError) as ctx:
            self.simulate({"steps": [split, second]})
        self.assertEqual(ctx.exception.code, "SIM_004")
        self.assertEqual(multiset(self.actual), self.before_multiset)

    def test_cyclic_and_unknown_references_are_rejected(self) -> None:
        cycle_a = step("update_attributes", [{"row": "B#1", "fingerprint": "0" * 64}],
                       {"set": {"segment": "Gold"}}, id="A")
        cycle_b = step("update_attributes", [{"row": "A#1", "fingerprint": "1" * 64}],
                       {"set": {"segment": "Gold"}}, id="B")
        with self.assertRaises(DimhistError) as ctx:
            self.simulate({"steps": [cycle_a, cycle_b]})
        self.assertEqual(ctx.exception.code, "SIM_005")
        unknown = step("update_attributes", [{"row": 99, "fingerprint": "0" * 64}],
                       {"set": {"segment": "Gold"}})
        with self.assertRaises(DimhistError) as ctx:
            self.simulate({"steps": [unknown]})
        self.assertEqual(ctx.exception.code, "SIM_002")

    def test_stale_fingerprint_preconditions_are_rejected(self) -> None:
        stale = step("update_attributes", [{"row": 1, "fingerprint": "0" * 64}],
                     {"set": {"segment": "Gold"}})
        with self.assertRaises(DimhistError) as ctx:
            self.simulate({"steps": [stale]})
        self.assertEqual(ctx.exception.code, "SIM_003")
        split = step("split_interval", [target(1, self.actual[0])], {"at": ts("03-01")})
        stale_half = step("update_attributes", [{"row": "SIM-0001#2", "fingerprint": "1" * 64}],
                          {"set": {"segment": "Gold"}})
        with self.assertRaises(DimhistError) as ctx:
            self.simulate({"steps": [split, stale_half]})
        self.assertEqual(ctx.exception.code, "SIM_003")
        self.assertEqual(multiset(self.actual), self.before_multiset)

    def test_invalid_action_params_are_rejected(self) -> None:
        cases = [
            step("split_interval", [target(1, self.actual[0])], {"at": ts("06-01")}),
            step("update_attributes", [target(1, self.actual[0])], {"set": {"unknown": "x"}}),
            step("update_attributes", [target(1, self.actual[0])], {"valid_to": ts("01-01")}),
            step("merge_duplicates", [target(1, self.actual[0]), target(2, self.actual[1])]),
        ]
        for request_step in cases:
            with self.subTest(op=request_step["op"], params=request_step["params"]):
                with self.assertRaises(DimhistError) as ctx:
                    self.simulate({"steps": [request_step]})
                self.assertEqual(ctx.exception.code, "SIM_006")

    def test_compound_simulation_rolls_back_to_original_multiset(self) -> None:
        actual = self.actual + [make_row(3, "SK-3", "Silver", ts("06-01"), None, True)]
        half = make_row(0, "SK-1~SIM-0001", "Bronze", ts("03-01"), ts("06-01"), False)
        request = {"steps": [
            step("split_interval", [target(1, actual[0])], {"at": ts("03-01")}),
            step("update_attributes", [target("SIM-0001#2", half)], {"set": {"segment": "Silver"}}),
            step("merge_duplicates", [target(2, actual[1]), target(3, actual[2])], {"keep": 2}),
        ]}
        outcome = simulate_repairs(self.contract, self.expected, actual, NOW, request)
        self.assertEqual(len(outcome.state), len(actual))
        self.assertNotEqual(multiset(outcome.state.values()), multiset(actual))
        restored = rollback_state(outcome.state, outcome.applied)
        self.assertEqual(multiset(restored.values()), multiset(actual))
        self.assertEqual(
            [record.action_id for record in reversed(outcome.applied)],
            ["SIM-0003", "SIM-0002", "SIM-0001"],
        )


class PipelineSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "workspace"
        support.install_fixtures(self.workspace)
        self.result = run_pipeline(self.workspace, now=support.NOW)
        self.rows = {row.row_no: row for row in self.result.actual}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def target_row(self, row_no):
        return {"row": row_no, "fingerprint": row_fingerprint(self.rows[row_no])}

    def test_merge_end_to_end_preserves_plan_evidence(self) -> None:
        action_id = next(
            action.action_id for action in self.result.plan
            if action.details["source_code"] == "DIM_DUPLICATE_VERSION"
        )
        request = {"steps": [{
            "op": "merge_duplicates",
            "plan_action": action_id,
            "targets": [self.target_row(14), self.target_row(15)],
            "params": {"keep": 15},
        }]}
        document = run_plan_simulation(self.result, request)
        eliminated = {(f["code"], f["natural_key"]) for f in document["findings"]["eliminated"]}
        self.assertIn(("DIM_DUPLICATE_VERSION", "M-1004"), eliminated)
        self.assertIn(("DIM_OVERLAP", "M-1004"), eliminated)
        self.assertNotIn(("DIM_MULTI_CURRENT", "M-1004"), eliminated)
        self.assertEqual(document["findings"]["introduced"], [])
        asof = {(c["natural_key"], c["at"], c["status"]) for c in document["asof"]["eliminated"]}
        self.assertIn(("M-1004", "2024-01-01T00:00:00Z", "multiple_actual"), asof)
        step_doc = document["steps"][0]
        self.assertEqual(step_doc["evidence"]["plan_action"]["action_id"], action_id)
        self.assertEqual(
            [f["code"] for f in step_doc["evidence"]["findings"]], ["DIM_DUPLICATE_VERSION"]
        )
        self.assertEqual(document["source_plan"]["actions_total"], 12)
        self.assertEqual(document["rollback"]["order"], ["SIM-0001"])
        json.dumps(document)

    def test_interval_and_attribute_updates_shift_findings_and_asof(self) -> None:
        request = {"steps": [
            {"op": "update_attributes", "targets": [self.target_row(11)],
             "params": {"valid_to": "2024-03-15T00:00:00Z"}},
            {"op": "update_attributes", "targets": [self.target_row(21)],
             "params": {"set": {"region": "EMEA"}}},
        ]}
        document = run_plan_simulation(self.result, request)
        eliminated = {(f["code"], f["natural_key"]) for f in document["findings"]["eliminated"]}
        self.assertIn(("DIM_OVERLAP", "M-1003"), eliminated)
        self.assertIn(("DIM_ATTR_DRIFT", "M-1008"), eliminated)
        self.assertEqual(document["findings"]["introduced"], [])
        asof = {(c["natural_key"], c["at"], c["status"]) for c in document["asof"]["eliminated"]}
        self.assertIn(("M-1003", "2024-03-15T00:00:00Z", "multiple_actual"), asof)
        self.assertIn(("M-1008", "2024-07-01T00:00:00Z", "attribute_drift"), asof)
        self.assertEqual(document["application_order"], ["SIM-0001", "SIM-0002"])

    def test_simulation_leaves_sources_ledger_and_plan_untouched(self) -> None:
        watched = [
            self.workspace / "ledger" / "dimhist.sqlite3",
            self.workspace / "data" / "dimension" / "member_history.csv",
            self.workspace / "data" / "dimension" / "member_current.csv",
            self.workspace / "data" / "events" / "change_events.jsonl",
        ]
        hashes = {path: sha256_file(path) for path in watched}
        before_rows = multiset(self.result.actual)
        request = {"steps": [{"op": "merge_duplicates",
                              "targets": [self.target_row(14), self.target_row(15)],
                              "params": {"keep": 15}}]}
        document = run_plan_simulation(self.result, request)
        self.assertEqual(document["mode"], "simulation_only")
        self.assertFalse(document["generates_sql"])
        self.assertFalse(document["mutates_source"])
        self.assertEqual({path: sha256_file(path) for path in watched}, hashes)
        self.assertEqual(multiset(self.result.actual), before_rows)
        self.assertEqual(list((self.workspace / "plans").iterdir()), [])
        stale = {"steps": [{"op": "update_attributes",
                            "targets": [{"row": 21, "fingerprint": "0" * 64}],
                            "params": {"set": {"region": "EMEA"}}}]}
        with self.assertRaises(DimhistError):
            run_plan_simulation(self.result, stale)
        unknown_action = {"steps": [{"op": "merge_duplicates", "plan_action": "ACT-9999",
                                     "targets": [self.target_row(14), self.target_row(15)]}]}
        with self.assertRaises(DimhistError):
            run_plan_simulation(self.result, unknown_action)
        self.assertEqual({path: sha256_file(path) for path in watched}, hashes)
        self.assertEqual(multiset(self.result.actual), before_rows)
        self.assertEqual(list((self.workspace / "plans").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
