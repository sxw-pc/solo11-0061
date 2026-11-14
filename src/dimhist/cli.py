"""Command-line interface for DimHist."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import PROJECT_ROOT
from .pipeline import init_workspace, reset_workspace, run_pipeline
from .report import build_summary, write_reports
from .server import create_server
from .util import DimhistError, relative_display

DEFAULT_NOW = "2026-01-15T00:00:00Z"
FIXTURE_FILES = (
    ("contracts/dimension_member.json", "contracts/dimension_member.json"),
    ("fixtures/events/change_events.jsonl", "data/events/change_events.jsonl"),
    ("fixtures/dimension/member_history.csv", "data/dimension/member_history.csv"),
    ("fixtures/dimension/member_current.csv", "data/dimension/member_current.csv"),
)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_workspace(Path(args.workspace))
            print(f"INIT OK workspace={args.workspace}")
            return 0
        if args.command == "demo":
            return _demo(args)
        if args.command == "run":
            return _run(args)
        if args.command == "serve":
            return _serve(args)
    except DimhistError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR IO_001: local filesystem operation failed: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 2


def _demo(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    if args.reset:
        reset_workspace(workspace)
    else:
        init_workspace(workspace)
    _install_fixtures(workspace)
    result = run_pipeline(workspace, now=args.now)
    paths = write_reports(workspace, result)
    summary = build_summary(result)
    if result.now == DEFAULT_NOW:
        _verify_expected(summary)
    print(
        "DEMO OK "
        f"workspace={workspace} "
        f"events={summary['event_rows']} "
        f"expected_versions={summary['expected_versions']} "
        f"actual_versions={summary['actual_versions']} "
        f"findings={summary['findings_total']} "
        f"asof_checks={summary['asof_checks']} "
        f"asof_mismatches={summary['asof_mismatches']} "
        f"plan_actions={summary['plan_actions']} "
        f"report={relative_display(paths['html'], workspace)}"
    )
    return 0


def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    init_workspace(workspace)
    result = run_pipeline(workspace, now=args.now)
    paths = write_reports(workspace, result)
    summary = build_summary(result)
    print(
        "RUN OK "
        f"findings={summary['findings_total']} "
        f"asof_mismatches={summary['asof_mismatches']} "
        f"plan_actions={summary['plan_actions']} "
        f"report={relative_display(paths['json'], workspace)}"
    )
    return 0


def _serve(args: argparse.Namespace) -> int:
    server = create_server(Path(args.workspace), host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(f"SERVE OK http://{host}:{port} read_only=true", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("SERVE STOPPED", file=sys.stderr)
    finally:
        server.server_close()
    return 0


def _install_fixtures(workspace: Path) -> None:
    for source, target in FIXTURE_FILES:
        source_path = PROJECT_ROOT / source
        target_path = workspace / target
        if not source_path.is_file():
            raise DimhistError("FIX_001", f"bundled fixture is missing: {source}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)


def _verify_expected(summary: dict[str, object]) -> None:
    expected_path = PROJECT_ROOT / "fixtures" / "expected" / "demo_summary.json"
    try:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DimhistError("FIX_001", f"expected summary is missing: {expected_path}") from exc
    except json.JSONDecodeError as exc:
        raise DimhistError("FIX_001", "expected summary is not valid JSON") from exc
    if expected != summary:
        raise DimhistError(
            "DEMO_001",
            "demo summary does not match fixtures/expected/demo_summary.json",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dimhist",
        description="Offline SCD2 dimension history calibration workbench.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create workspace directories")
    init_parser.add_argument("--workspace", default=".dimhist-work")
    demo_parser = subparsers.add_parser("demo", help="run the bundled end-to-end demo")
    demo_parser.add_argument("--workspace", default=".dimhist-work")
    demo_parser.add_argument("--reset", action="store_true")
    demo_parser.add_argument("--now", default=DEFAULT_NOW)
    run_parser = subparsers.add_parser("run", help="calibrate the current workspace inputs")
    run_parser.add_argument("--workspace", default=".dimhist-work")
    run_parser.add_argument("--now", default=DEFAULT_NOW)
    serve_parser = subparsers.add_parser("serve", help="start the read-only loopback server")
    serve_parser.add_argument("--workspace", default=".dimhist-work")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5017)
    return parser
