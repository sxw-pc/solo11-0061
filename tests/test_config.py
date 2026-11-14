"""Contract loading and stable configuration error codes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.config import load_contract
from dimhist.util import DimhistError, sha256_file


class ContractTests(unittest.TestCase):
    def test_bundled_contract_loads_with_file_hash(self) -> None:
        contract = load_contract(support.CONTRACT_PATH)
        self.assertEqual(contract.table, "dim_member")
        self.assertEqual(contract.natural_key, "member_id")
        self.assertEqual(contract.tracked_attributes, ("segment", "region", "status"))
        self.assertEqual(contract.sha256, sha256_file(support.CONTRACT_PATH))
        self.assertEqual(contract.delete_semantics, "status_only")

    def test_missing_file_and_invalid_json_are_cfg_001(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            with self.assertRaises(DimhistError) as missing:
                load_contract(tmp / "absent.json")
            self.assertEqual(missing.exception.code, "CFG_001")
            bad = tmp / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(DimhistError) as invalid:
                load_contract(bad)
            self.assertEqual(invalid.exception.code, "CFG_001")

    def test_missing_required_field_is_cfg_002(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = support.fixture_contract()
            del document["same_time_policy"]
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(DimhistError) as caught:
                load_contract(path)
            self.assertEqual(caught.exception.code, "CFG_002")
            self.assertIn("same_time_policy", caught.exception.message)

    def test_duplicate_attribute_is_cfg_003(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = support.write_contract(
                Path(directory), tracked_attributes=["segment", "segment", "region"]
            )
            with self.assertRaises(DimhistError) as caught:
                load_contract(path)
            self.assertEqual(caught.exception.code, "CFG_003")

    def test_unknown_type_is_cfg_004(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = support.write_contract(
                Path(directory),
                attribute_types={"segment": "blob", "region": "string", "status": "string"},
            )
            with self.assertRaises(DimhistError) as caught:
                load_contract(path)
            self.assertEqual(caught.exception.code, "CFG_004")

    def test_same_time_policy_is_cfg_005(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = support.write_contract(
                Path(directory), same_time_policy="last_file_line_wins"
            )
            with self.assertRaises(DimhistError) as caught:
                load_contract(path)
            self.assertEqual(caught.exception.code, "CFG_005")

    def test_delete_semantics_is_cfg_006(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = support.write_contract(Path(directory), delete_semantics="hard_delete")
            with self.assertRaises(DimhistError) as caught:
                load_contract(path)
            self.assertEqual(caught.exception.code, "CFG_006")

    def test_time_format_is_cfg_007(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = support.write_contract(Path(directory), time_format="epoch_millis")
            with self.assertRaises(DimhistError) as caught:
                load_contract(path)
            self.assertEqual(caught.exception.code, "CFG_007")

    def test_field_collision_is_cfg_008(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = support.write_contract(
                Path(directory), sequence_field="event_id"
            )
            with self.assertRaises(DimhistError) as caught:
                load_contract(path)
            self.assertEqual(caught.exception.code, "CFG_008")


if __name__ == "__main__":
    unittest.main()
