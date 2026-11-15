"""Atomic cross-member transaction replay: quarantine, ordering, evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.config import load_contract
from dimhist.ledger import Ledger
from dimhist.pipeline import run_pipeline
from dimhist.replay import ingest_and_replay
from dimhist.report import build_report

T1 = "2024-01-01T00:00:00Z"
T2 = "2024-02-01T00:00:00Z"
T3 = "2024-03-01T00:00:00Z"
T4 = "2024-04-01T00:00:00Z"
T6 = "2024-06-01T00:00:00Z"


def tx_event(event_id, key, at, sequence, tx_id, tx_seq, **payload):
    return {
        "event_id": event_id,
        "member_id": key,
        "effective_at": at,
        "sequence": sequence,
        "transaction_id": tx_id,
        "transaction_seq": tx_seq,
        "payload": payload,
    }


def commit(tx_id, at, members, depends_on=None):
    entry = {"transaction_id": tx_id, "effective_at": at, "members": members}
    if depends_on:
        entry["depends_on"] = depends_on
    return entry


def plain_event(event_id, key, at, sequence, **payload):
    return {
        "event_id": event_id,
        "member_id": key,
        "effective_at": at,
        "sequence": sequence,
        "payload": payload,
    }


class TransactionReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_contract(support.CONTRACT_PATH)
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest(self, events, manifest=None):
        event_path = support.write_events(self.tmp / "events.jsonl", events)
        tx_path = None
        if manifest is not None:
            tx_path = self.tmp / "commits.jsonl"
            lines = [
                json.dumps(entry, separators=(",", ":"), sort_keys=True)
                for entry in manifest
            ]
            tx_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ingest_and_replay(self.contract, event_path, tx_path)

    def _versions(self, result, key):
        return [v for v in result.expected_versions if v.natural_key == key]

    def _statuses(self, result):
        return {event.event_id: event.status for event in result.events}

    def _records(self, result):
        return {record.transaction_id: record for record in result.transactions}

    def _quarantines(self, result):
        return [f for f in result.findings if f.code == "EVT_TX_QUARANTINED"]

    def test_cross_dependency_orders_transactions_and_versions(self) -> None:
        result = self._ingest(
            [
                support.event("E-0A", "M-A", T1, 1),
                support.event("E-0B", "M-B", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
                tx_event("E-2", "M-B", T3, 2, "TX-A", 2, region="EMEA"),
                tx_event("E-3", "M-A", T3, 3, "TX-B", 1, segment="Platinum"),
                tx_event("E-4", "M-A", T4, 4, "TX-C", 1, status="Inactive"),
            ],
            [
                commit("TX-A", T3, ["E-1", "E-2"]),
                commit("TX-B", T3, ["E-3"], depends_on=["TX-A"]),
                commit("TX-C", T4, ["E-4"], depends_on=["TX-B"]),
            ],
        )
        self.assertEqual(self._quarantines(result), [])
        records = self._records(result)
        self.assertEqual(
            {tx: record.status for tx, record in records.items()},
            {"TX-A": "committed", "TX-B": "committed", "TX-C": "committed"},
        )
        self.assertEqual(records["TX-A"].order_classification, "unique")
        self.assertEqual(records["TX-B"].order_classification, "unique")

        versions = self._versions(result, "M-A")
        self.assertEqual([v.valid_from for v in versions], [T1, T3, T4])
        # TX-B depends on TX-A, so TX-B's Platinum wins over TX-A's Gold.
        self.assertEqual(versions[1].attrs["segment"], "Platinum")
        self.assertEqual(versions[1].attrs["region"], "APAC")
        self.assertEqual(versions[1].source_event_ids, ("E-1", "E-3"))
        self.assertEqual(versions[1].source_transaction_ids, ("TX-A", "TX-B"))
        self.assertEqual(versions[2].attrs["status"], "Inactive")
        self.assertEqual(versions[2].attrs["segment"], "Platinum")
        self.assertEqual(versions[2].source_transaction_ids, ("TX-C",))
        self.assertEqual(versions[1].valid_to, T4)

        member_b = self._versions(result, "M-B")
        self.assertEqual(member_b[-1].attrs["region"], "EMEA")
        self.assertEqual(member_b[-1].source_transaction_ids, ("TX-A",))

        statuses = self._statuses(result)
        self.assertEqual(statuses["E-1"], "superseded_same_time")
        self.assertEqual(statuses["E-3"], "applied")
        self.assertEqual(statuses["E-2"], "applied")
        version_dict = versions[1].to_dict()
        self.assertEqual(version_dict["source_transaction_ids"], ["TX-A", "TX-B"])
        self.assertEqual(version_dict["source_event_ids"], ["E-1", "E-3"])

    def test_commutable_transactions_share_one_result(self) -> None:
        result = self._ingest(
            [
                support.event("E-0", "M-A", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
                tx_event("E-2", "M-A", T3, 3, "TX-B", 1, region="EMEA"),
            ],
            [commit("TX-A", T3, ["E-1"]), commit("TX-B", T3, ["E-2"])],
        )
        self.assertEqual(self._quarantines(result), [])
        records = self._records(result)
        self.assertEqual(records["TX-A"].order_classification, "equivalent")
        self.assertEqual(records["TX-B"].order_classification, "equivalent")
        versions = self._versions(result, "M-A")
        self.assertEqual(len(versions), 2)
        merged = versions[-1]
        self.assertEqual(merged.valid_from, T3)
        self.assertEqual(merged.attrs["segment"], "Gold")
        self.assertEqual(merged.attrs["region"], "EMEA")
        self.assertEqual(merged.source_event_ids, ("E-1", "E-2"))
        self.assertEqual(merged.source_transaction_ids, ("TX-A", "TX-B"))

    def test_non_commuting_transactions_are_quarantined_as_true_conflict(self) -> None:
        result = self._ingest(
            [
                support.event("E-0", "M-A", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
                tx_event("E-2", "M-A", T3, 3, "TX-B", 1, segment="Silver"),
            ],
            [commit("TX-A", T3, ["E-1"]), commit("TX-B", T3, ["E-2"])],
        )
        records = self._records(result)
        self.assertEqual(records["TX-A"].status, "quarantined")
        self.assertEqual(records["TX-B"].status, "quarantined")
        self.assertEqual(records["TX-A"].quarantine_reason, "order_conflict")
        self.assertEqual(records["TX-B"].quarantine_reason, "order_conflict")
        conflict = records["TX-A"].details["conflicts"][0]
        self.assertEqual(conflict["member"], "M-A")
        self.assertEqual(conflict["attribute"], "segment")
        self.assertEqual(
            conflict["candidate_values"], {"TX-A": "Gold", "TX-B": "Silver"}
        )
        # The ambiguity is not masked by event_id order: neither value applies.
        versions = self._versions(result, "M-A")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].attrs["segment"], "Bronze")
        statuses = self._statuses(result)
        self.assertEqual(statuses["E-1"], "quarantined")
        self.assertEqual(statuses["E-2"], "quarantined")
        self.assertEqual(len(self._quarantines(result)), 2)
        self.assertEqual(result.stats["transactions_quarantined"], 2)
        self.assertEqual(result.stats["quarantined_events"], 2)

    def test_missing_member_quarantines_entire_transaction(self) -> None:
        result = self._ingest(
            [
                support.event("E-0", "M-A", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
            ],
            [commit("TX-A", T3, ["E-1", "E-2"])],
        )
        record = self._records(result)["TX-A"]
        self.assertEqual(record.status, "quarantined")
        self.assertEqual(record.quarantine_reason, "missing_member")
        self.assertEqual(record.details["missing_event_ids"], ["E-2"])
        # The valid subset (E-1) is not applied on its own.
        self.assertEqual(self._statuses(result)["E-1"], "quarantined")
        versions = self._versions(result, "M-A")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].attrs["segment"], "Bronze")

    def test_late_commit_is_reordered_by_business_time(self) -> None:
        events = [
            support.event("E-0", "M-A", T1, 1),
            plain_event("E-9", "M-A", T6, 2, segment="Silver"),
            tx_event("E-1", "M-A", T3, 3, "TX-LATE", 1, region="EMEA"),
            tx_event("E-2", "M-A", T2, 4, "TX-EARLY", 1, status="Inactive"),
        ]
        manifest = [
            commit("TX-LATE", T3, ["E-1"]),
            commit("TX-EARLY", T2, ["E-2"]),
        ]
        result = self._ingest(events, manifest)
        versions = self._versions(result, "M-A")
        self.assertEqual([v.valid_from for v in versions], [T1, T2, T3, T6])
        self.assertEqual(versions[1].attrs["status"], "Inactive")
        self.assertEqual(versions[1].attrs["region"], "APAC")
        self.assertEqual(versions[2].attrs["region"], "EMEA")
        self.assertEqual(versions[2].attrs["status"], "Inactive")
        self.assertEqual(versions[3].attrs["segment"], "Silver")
        self.assertEqual(versions[3].attrs["region"], "EMEA")
        for left, right in zip(versions, versions[1:]):
            self.assertEqual(left.valid_to, right.valid_from)
        self.assertIsNone(versions[-1].valid_to)
        # Physical manifest order does not change the business-time replay.
        reordered = self._ingest(events, list(reversed(manifest)))
        self.assertEqual(
            [v.to_dict() for v in result.expected_versions],
            [v.to_dict() for v in reordered.expected_versions],
        )

    def test_duplicate_transaction_seq_quarantines(self) -> None:
        result = self._ingest(
            [
                support.event("E-0", "M-A", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
                tx_event("E-2", "M-B", T3, 3, "TX-A", 1, region="EMEA"),
            ],
            [commit("TX-A", T3, ["E-1", "E-2"])],
        )
        record = self._records(result)["TX-A"]
        self.assertEqual(record.status, "quarantined")
        self.assertEqual(record.quarantine_reason, "duplicate_seq")
        self.assertEqual(record.details["sequences"], [1])
        self.assertEqual(self._statuses(result)["E-2"], "quarantined")
        self.assertEqual(self._versions(result, "M-B"), [])

    def test_conflicting_member_content_quarantines(self) -> None:
        result = self._ingest(
            [
                support.event("E-0", "M-A", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Silver"),
                tx_event("E-2", "M-B", T3, 3, "TX-A", 2, region="EMEA"),
            ],
            [commit("TX-A", T3, ["E-1", "E-2"])],
        )
        codes = [finding.code for finding in result.findings]
        self.assertIn("EVT_DUP_CONFLICT", codes)
        record = self._records(result)["TX-A"]
        self.assertEqual(record.status, "quarantined")
        self.assertEqual(record.quarantine_reason, "member_conflict")
        self.assertEqual(record.details["conflicting_event_ids"], ["E-1"])
        self.assertEqual(self._versions(result, "M-B"), [])
        self.assertEqual(len(self._versions(result, "M-A")), 1)

    def test_dependency_cycle_reports_minimal_set_and_cascades(self) -> None:
        result = self._ingest(
            [
                tx_event("E-1", "M-A", T3, 1, "TX-A", 1, segment="Gold"),
                tx_event("E-2", "M-B", T3, 2, "TX-B", 1, segment="Silver"),
                tx_event("E-3", "M-C", T3, 3, "TX-C", 1, segment="Platinum"),
                tx_event("E-4", "M-D", T3, 4, "TX-D", 1, segment="Bronze"),
            ],
            [
                commit("TX-A", T3, ["E-1"], depends_on=["TX-B"]),
                commit("TX-B", T3, ["E-2"], depends_on=["TX-A"]),
                commit("TX-C", T3, ["E-3"]),
                commit("TX-D", T3, ["E-4"], depends_on=["TX-A"]),
            ],
        )
        records = self._records(result)
        self.assertEqual(records["TX-A"].quarantine_reason, "dependency_cycle")
        self.assertEqual(records["TX-B"].quarantine_reason, "dependency_cycle")
        self.assertEqual(records["TX-A"].details["cycle"], ["TX-A", "TX-B"])
        self.assertEqual(records["TX-B"].details["cycle"], ["TX-A", "TX-B"])
        self.assertEqual(records["TX-D"].quarantine_reason, "unmet_dependency")
        self.assertEqual(
            records["TX-D"].details["unsatisfied_dependencies"], ["TX-A"]
        )
        self.assertEqual(records["TX-C"].status, "committed")
        member_c = self._versions(result, "M-C")
        self.assertEqual(member_c[-1].attrs["segment"], "Platinum")
        self.assertEqual(self._versions(result, "M-A"), [])
        self.assertEqual(self._versions(result, "M-D"), [])

    def test_partial_update_inside_transaction_inherits_prior_state(self) -> None:
        result = self._ingest(
            [
                support.event("E-0", "M-A", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
            ],
            [commit("TX-A", T3, ["E-1"])],
        )
        versions = self._versions(result, "M-A")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[-1].attrs["segment"], "Gold")
        self.assertEqual(versions[-1].attrs["region"], "APAC")
        self.assertEqual(versions[-1].attrs["status"], "Active")

    def test_failed_member_preserves_other_members_prior_state(self) -> None:
        result = self._ingest(
            [
                support.event("E-0A", "M-A", T1, 1),
                support.event("E-0B", "M-B", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-1", 1, segment="Gold"),
                tx_event("E-2", "M-B", T3, 3, "TX-1", 2, region="EMEA"),
                tx_event("E-3", "M-B", T4, 4, "TX-2", 1, segment="Silver"),
            ],
            [
                commit("TX-1", T3, ["E-1", "E-2", "E-9"]),
                commit("TX-2", T4, ["E-3"]),
            ],
        )
        records = self._records(result)
        self.assertEqual(records["TX-1"].quarantine_reason, "missing_member")
        self.assertEqual(records["TX-2"].status, "committed")
        member_b = self._versions(result, "M-B")
        self.assertEqual([v.valid_from for v in member_b], [T1, T4])
        # TX-1 was quarantined as a whole: its EMEA write never touched M-B.
        self.assertEqual(member_b[-1].attrs["segment"], "Silver")
        self.assertEqual(member_b[-1].attrs["region"], "APAC")
        self.assertEqual(len(self._versions(result, "M-A")), 1)

    def test_legacy_semantics_without_transaction_fields(self) -> None:
        result = self._ingest(
            [
                support.event("E-1", "M-A", T1, 1),
                support.event("E-2", "M-A", T1, 2, segment="Gold"),
            ]
        )
        self.assertEqual(result.transactions, [])
        versions = self._versions(result, "M-A")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].attrs["segment"], "Gold")
        self.assertEqual(versions[0].source_transaction_ids, ())
        codes = [finding.code for finding in result.findings]
        self.assertEqual(codes, ["EVT_SAME_TIME_CONFLICT"])
        self.assertEqual(result.stats["transactions_declared"], 0)
        self.assertEqual(result.stats["transactions_committed"], 0)
        self.assertEqual(result.stats["transactions_quarantined"], 0)
        self.assertEqual(result.stats["transaction_events"], 0)
        self.assertEqual(result.stats["quarantined_events"], 0)

    def test_events_without_commit_manifest_are_quarantined(self) -> None:
        result = self._ingest(
            [
                support.event("E-0", "M-A", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-X", 1, segment="Gold"),
            ]
        )
        record = self._records(result)["TX-X"]
        self.assertEqual(record.status, "quarantined")
        self.assertEqual(record.quarantine_reason, "missing_commit")
        self.assertEqual(record.member_event_ids, ("E-1",))
        self.assertEqual(self._statuses(result)["E-1"], "quarantined")
        versions = self._versions(result, "M-A")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].attrs["segment"], "Bronze")

    def test_transaction_layers_over_same_moment_plain_events(self) -> None:
        result = self._ingest(
            [
                support.event("E-0", "M-A", T3, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
            ],
            [commit("TX-A", T3, ["E-1"])],
        )
        versions = self._versions(result, "M-A")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].attrs["segment"], "Gold")
        self.assertEqual(versions[0].attrs["region"], "APAC")
        self.assertEqual(versions[0].source_event_ids, ("E-0", "E-1"))
        self.assertEqual(versions[0].source_transaction_ids, ("TX-A",))

    def test_pipeline_records_quarantine_in_ledger_and_report(self) -> None:
        workspace = self.tmp / "workspace"
        support.install_fixtures(workspace)
        support.write_events(
            workspace / "data" / "events" / "change_events.jsonl",
            [
                support.event("E-0", "M-A", T1, 1),
                tx_event("E-1", "M-A", T3, 2, "TX-A", 1, segment="Gold"),
                tx_event("E-2", "M-A", T3, 3, "TX-B", 1, segment="Silver"),
            ],
        )
        manifest_path = workspace / "data" / "events" / "transaction_commits.jsonl"
        manifest_path.write_text(
            "\n".join(
                json.dumps(entry, sort_keys=True)
                for entry in [
                    commit("TX-A", T3, ["E-1"]),
                    commit("TX-B", T3, ["E-2"]),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = run_pipeline(workspace, now=support.NOW)
        self.assertEqual(len(result.transactions), 2)
        self.assertTrue(
            all(record.status == "quarantined" for record in result.transactions)
        )
        tx_findings = [f for f in result.findings if f.code == "EVT_TX_QUARANTINED"]
        self.assertEqual(len(tx_findings), 2)
        self.assertEqual(len(result.inputs), 5)
        self.assertEqual(result.inputs[-1]["kind"], "transaction_commits")

        report = build_report(result)
        self.assertEqual(len(report["transactions"]), 2)
        self.assertEqual(
            {row["transaction_id"] for row in report["transactions"]},
            {"TX-A", "TX-B"},
        )
        self.assertNotIn("transactions_quarantined", report["summary"])

        with Ledger(workspace / "ledger" / "dimhist.sqlite3") as ledger:
            self.assertEqual(ledger.count("transactions"), 2)
            rows = {
                row["transaction_id"]: row
                for row in ledger.rows("transactions", "transaction_id")
            }
            self.assertEqual(rows["TX-A"]["status"], "quarantined")
            self.assertEqual(rows["TX-A"]["quarantine_reason"], "order_conflict")
            self.assertEqual(rows["TX-B"]["quarantine_reason"], "order_conflict")
            self.assertEqual(
                json.loads(rows["TX-A"]["member_event_ids_json"]), ["E-1"]
            )
            event_rows = {
                row["event_id"]: row["status"]
                for row in ledger.rows("events", "position")
            }
            self.assertEqual(event_rows["E-1"], "quarantined")
            self.assertEqual(event_rows["E-2"], "quarantined")
            finding_codes = {
                row["code"] for row in ledger.rows("findings", "finding_id")
            }
            self.assertIn("EVT_TX_QUARANTINED", finding_codes)
            artifacts = {
                row["kind"] for row in ledger.rows("artifacts", "kind")
            }
            self.assertIn("transaction_commits", artifacts)


if __name__ == "__main__":
    unittest.main()
