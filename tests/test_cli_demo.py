"""CLI initialization, one-command demo and user-facing errors."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import _support as support

from dimhist.cli import main


def invoke(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class CliDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "work"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_init_creates_required_directories(self) -> None:
        code, stdout, stderr = invoke(["init", "--workspace", str(self.workspace)])
        self.assertEqual(code, 0, stderr)
        self.assertIn("INIT OK", stdout)
        for relative in (
            "contracts",
            "data/events",
            "data/dimension",
            "ledger",
            "reports",
            "plans",
        ):
            self.assertTrue((self.workspace / relative).is_dir(), relative)
        self.assertTrue((self.workspace / ".dimhist-workspace").is_file())

    def test_demo_runs_end_to_end_and_is_repeatable(self) -> None:
        code, stdout, stderr = invoke(
            ["demo", "--workspace", str(self.workspace), "--reset"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("DEMO OK", stdout)
        self.assertIn("findings=12", stdout)
        self.assertIn("asof_mismatches=14", stdout)
        self.assertTrue((self.workspace / "reports" / "demo_report.html").is_file())
        self.assertTrue((self.workspace / "plans" / "repair_plan.json").is_file())
        self.assertTrue((self.workspace / "ledger" / "dimhist.sqlite3").is_file())
        report = json.loads(
            (self.workspace / "reports" / "demo_report.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["summary"], support.expected_summary())
        code, second_stdout, stderr = invoke(
            ["demo", "--workspace", str(self.workspace), "--reset"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, second_stdout)

    def test_custom_now_is_supported(self) -> None:
        code, stdout, stderr = invoke(
            [
                "demo",
                "--workspace",
                str(self.workspace),
                "--reset",
                "--now",
                "2025-01-01T00:00:00Z",
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("DEMO OK", stdout)
        report = json.loads(
            (self.workspace / "reports" / "demo_report.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["generated_at"], "2025-01-01T00:00:00Z")

    def test_reset_refuses_unmarked_non_empty_directory(self) -> None:
        self.workspace.mkdir()
        sentinel = self.workspace / "keep.txt"
        sentinel.write_text("do not delete", encoding="utf-8")
        code, _, stderr = invoke(
            ["demo", "--workspace", str(self.workspace), "--reset"]
        )
        self.assertEqual(code, 2)
        self.assertIn("WS_002", stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not delete")

    def test_invalid_contract_has_no_traceback(self) -> None:
        support.install_fixtures(self.workspace)
        (self.workspace / "contracts" / "dimension_member.json").write_text(
            "{}", encoding="utf-8"
        )
        code, _, stderr = invoke(["run", "--workspace", str(self.workspace)])
        self.assertEqual(code, 2)
        self.assertIn("CFG_002", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_serve_rejects_external_host(self) -> None:
        code, _, stderr = invoke(
            ["serve", "--workspace", str(self.workspace), "--host", "0.0.0.0"]
        )
        self.assertEqual(code, 2)
        self.assertIn("SRV_001", stderr)


if __name__ == "__main__":
    unittest.main()
