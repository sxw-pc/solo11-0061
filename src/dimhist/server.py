"""Read-only loopback HTTP service for reports and calibration artifacts."""

from __future__ import annotations

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .util import DimhistError, is_loopback_host, validate_loopback_host

MAX_API_ITEMS = 1000


class DimhistHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], report: dict[str, Any], reports_dir: Path):
        self.report = report
        self.reports_dir = reports_dir
        super().__init__(address, DimhistHandler)


class DimhistHTTPServerV6(DimhistHTTPServer):
    address_family = socket.AF_INET6


class DimhistHandler(BaseHTTPRequestHandler):
    server: DimhistHTTPServer

    def do_HEAD(self) -> None:
        self._dispatch(head=True)

    def do_GET(self) -> None:
        self._dispatch(head=False)

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._json(
            {"error": "method_not_allowed", "read_only": True},
            head=False,
            status=405,
        )

    def _dispatch(self, head: bool) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path in {"/", "/report", "/report.html"}:
                return self._serve_file("demo_report.html", head)
            if path.startswith("/reports/"):
                return self._serve_file(path.removeprefix("/reports/"), head)
            if path == "/api/health":
                return self._json(
                    {
                        "status": "ok",
                        "read_only": True,
                        "schema_version": self.server.report["schema_version"],
                        "generated_at": self.server.report["generated_at"],
                    },
                    head,
                )
            if path == "/api/report":
                return self._json(self.server.report, head)
            if path == "/api/summary":
                return self._json(self.server.report["summary"], head)
            if path == "/api/findings":
                items = _filtered(
                    self.server.report["findings"],
                    query,
                    ("code", "severity", "natural_key"),
                )
                return self._json({"count": len(items), "findings": items}, head)
            if path == "/api/asof":
                items = _filtered(
                    self.server.report["asof_checks"],
                    query,
                    ("status", "at", "natural_key"),
                )
                return self._json({"count": len(items), "checks": items}, head)
            if path == "/api/plan":
                items = self.server.report["plan"]
                return self._json(
                    {"mode": "dry_run_only", "count": len(items), "actions": items},
                    head,
                )
            if path == "/api/triage":
                return self._json(self.server.report["triage"], head)
            if path == "/api/events":
                items = _filtered(
                    self.server.report["event_ledger"],
                    query,
                    ("status", "natural_key"),
                )
                return self._json({"count": len(items), "events": items}, head)
            if path.startswith("/api/members/"):
                key = unquote(path.removeprefix("/api/members/"))
                return self._member(key, head)
            if path.startswith("/api/"):
                return self._json({"error": "not_found", "path": path}, head, 404)
            return self._json({"error": "not_found", "path": path}, head, 404)
        except DimhistError as exc:
            self._json({"error": exc.code, "message": exc.message}, head, 404)

    def _member(self, key: str, head: bool) -> None:
        if not key:
            return self._json({"error": "missing_member_key"}, head, 400)
        report = self.server.report
        payload = {
            "natural_key": key,
            "expected": [
                item for item in report["expected_history"] if item["natural_key"] == key
            ],
            "actual": [
                item for item in report["actual_history"] if item["natural_key"] == key
            ],
            "findings": [
                item for item in report["findings"] if item["natural_key"] == key
            ],
            "asof_checks": [
                item for item in report["asof_checks"] if item["natural_key"] == key
            ],
        }
        if not any(payload[name] for name in ("expected", "actual", "findings")):
            return self._json({"error": "member_not_found", "natural_key": key}, head, 404)
        return self._json(payload, head)

    def _serve_file(self, name: str, head: bool) -> None:
        allowed = {
            "demo_report.html": "text/html; charset=utf-8",
            "demo_report.json": "application/json; charset=utf-8",
            "demo_report.md": "text/markdown; charset=utf-8",
        }
        if name not in allowed:
            raise DimhistError("SRV_004", "only the generated demo report is served")
        path = self.server.reports_dir / name
        if not path.is_file():
            raise DimhistError("SRV_004", f"report file not found: {name}")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", allowed[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(
        self, payload: Any, head: bool, status: int = 200
    ) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def load_report(workspace: Path) -> tuple[dict[str, Any], Path]:
    path = workspace / "reports" / "demo_report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DimhistError(
            "SRV_002", f"report not found; run demo first: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise DimhistError("SRV_002", f"report is not valid JSON: {path}") from exc
    if not isinstance(report, dict) or "summary" not in report:
        raise DimhistError("SRV_002", "report shape is not supported")
    return report, path.parent


def create_server(
    workspace: Path, host: str = "127.0.0.1", port: int = 5017
) -> DimhistHTTPServer:
    validate_loopback_host(host)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise DimhistError("SRV_001", "port must be an integer between 0 and 65535")
    report, reports_dir = load_report(workspace)
    bind_host = "127.0.0.1" if host == "localhost" else host
    server_class = DimhistHTTPServerV6 if host == "::1" else DimhistHTTPServer
    try:
        return server_class((bind_host, port), report, reports_dir)
    except OSError as exc:
        raise DimhistError("SRV_002", f"cannot bind {host}:{port}: {exc}") from exc


def _filtered(
    items: list[dict[str, Any]], query: dict[str, list[str]], names: tuple[str, ...]
) -> list[dict[str, Any]]:
    result = list(items)
    for name in names:
        if name not in query:
            continue
        wanted = query[name][0]
        result = [item for item in result if str(item.get(name, "")) == wanted]
    return result[:MAX_API_ITEMS]


def is_allowed_host(host: str) -> bool:
    return is_loopback_host(host)
