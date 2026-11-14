"""Raw-line fidelity, duplicate event handling and same-time merging."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.config import load_contract
from dimhist.io_utils import read_jsonl
from dimhist.replay import ingest_and_replay
from dimhist.util import DimhistError, sha256_text


class IoAndDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_contract(support.CONTRACT_PATH)
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest(self, events):
        path = support.write_events(self.tmp / "events.jsonl", events)
        return ingest_and_replay(self.contract, path)

    def test_jsonl_preserves_line_numbers_text_and_hashes(self) -> None:
        path = self.tmp / "events.jsonl"
        path.write_text("\n\n" + '{"b": 1, "a": 2}\n', encoding="utf-8")
        lines, digest = read_jsonl(path)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].line_no, 3)
        self.assertEqual(lines[0].text, '{"b": 1, "a": 2}')
        self.assertEqual(lines[0].sha256, sha256_text(lines[0].text))
        self.assertEqual(len(digest), 64)

    def test_exact_duplicate_is_counted_and_conflict_is_reported(self) -> None:
        first = support.event("E-1", "M-A", "2024-01-01T00:00:00Z", 1)
        exact = dict(first)
        conflict = dict(first)
        conflict["payload"] = {"segment": "Bronze", "region": "EMEA", "status": "Active"}
        result = self._ingest([first, exact, conflict])
        self.assertEqual(result.stats["event_rows"], 3)
        self.assertEqual(result.stats["unique_event_ids"], 1)
        self.assertEqual(result.stats["duplicate_exact"], 1)
        self.assertEqual(result.stats["duplicate_conflict"], 1)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].code, "EVT_DUP_CONFLICT")
        self.assertEqual(
            result.findings[0].details["differing_fields"], ["payload.region"]
        )
        self.assertEqual(result.expected_versions[0].attrs["region"], "APAC")
        by_line = {event.raw_line: event.status for event in result.events}
        self.assertEqual(by_line[1], "applied")
        self.assertEqual(by_line[2], "duplicate_exact")
        self.assertEqual(by_line[3], "duplicate_conflict")

    def test_same_time_events_merge_by_sequence_and_event_id(self) -> None:
        result = self._ingest(
            [
                support.event(
                    "E-9", "M-A", "2024-03-01T00:00:00Z", 1, "Silver", "APAC"
                ),
                support.event(
                    "E-2", "M-A", "2024-03-01T00:00:00Z", 2, "Gold", "EMEA"
                ),
            ]
        )
        self.assertEqual(len(result.expected_versions), 1)
        self.assertEqual(result.expected_versions[0].attrs["segment"], "Gold")
        self.assertEqual(result.findings[0].code, "EVT_SAME_TIME_CONFLICT")
        self.assertEqual(
            result.findings[0].details["selected_event_id"], "E-2"
        )
        statuses = {event.event_id: event.status for event in result.events}
        self.assertEqual(statuses["E-9"], "superseded_same_time")
        self.assertEqual(statuses["E-2"], "applied")

    def test_same_time_tie_breaks_on_event_id_not_file_order(self) -> None:
        result = self._ingest(
            [
                support.event(
                    "E-B", "M-A", "2024-03-01T00:00:00Z", 1, "Silver", "APAC"
                ),
                support.event(
                    "E-A", "M-A", "2024-03-01T00:00:00Z", 1, "Gold", "EMEA"
                ),
            ]
        )
        self.assertEqual(result.expected_versions[0].attrs["segment"], "Silver")
        self.assertEqual(result.findings[0].details["event_ids"], ["E-A", "E-B"])

    def test_noop_event_does_not_create_a_version(self) -> None:
        result = self._ingest(
            [
                support.event("E-1", "M-A", "2024-01-01T00:00:00Z", 1),
                support.event("E-2", "M-A", "2024-04-01T00:00:00Z", 2),
            ]
        )
        self.assertEqual(len(result.expected_versions), 1)
        self.assertEqual(result.stats["noop_events"], 1)
        self.assertEqual(result.events[-1].status, "noop")

    def test_invalid_event_time_has_stable_code(self) -> None:
        event = support.event("E-1", "M-A", "2024-01-01 00:00:00", 1)
        with self.assertRaises(DimhistError) as caught:
            self._ingest([event])
        self.assertEqual(caught.exception.code, "INP_004")

    def test_unknown_payload_field_is_rejected(self) -> None:
        event = support.event("E-1", "M-A", "2024-01-01T00:00:00Z", 1)
        event["payload"]["unexpected"] = "x"
        with self.assertRaises(DimhistError) as caught:
            self._ingest([event])
        self.assertEqual(caught.exception.code, "INP_003")


if __name__ == "__main__":
    unittest.main()
