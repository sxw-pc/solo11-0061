"""Loopback-only read-only HTTP service tests."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

import _support as support

from dimhist.server import (
    create_server,
    is_allowed_host,
)
from dimhist.util import DimhistError


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls.temporary.name) / "workspace"
        support.fixture_run(cls.workspace, reports=True)
        cls.server = create_server(cls.workspace, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temporary.cleanup()

    def request(self, method: str, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def json_request(self, path: str):
        status, headers, body = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        return json.loads(body) if body else None

    def test_allowed_hosts(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(is_allowed_host(host))
        self.assertFalse(is_allowed_host("0.0.0.0"))
        self.assertFalse(is_allowed_host("example.test"))

    def test_external_host_is_rejected_before_binding(self) -> None:
        with self.assertRaises(DimhistError) as caught:
            create_server(self.workspace, "0.0.0.0", 5017)
        self.assertEqual(caught.exception.code, "SRV_001")

    def test_health_and_summary(self) -> None:
        health = self.json_request("/api/health")
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["read_only"])
        summary = self.json_request("/api/summary")
        self.assertEqual(summary["findings_total"], 12)
        self.assertEqual(summary["asof_checks"], 33)

    def test_findings_filtering(self) -> None:
        payload = self.json_request("/api/findings?severity=high")
        self.assertEqual(payload["count"], 6)
        self.assertTrue(all(item["severity"] == "high" for item in payload["findings"]))
        overlap = self.json_request("/api/findings?code=DIM_OVERLAP")
        self.assertEqual(overlap["count"], 3)

    def test_member_plan_events_and_asof_endpoints(self) -> None:
        member = self.json_request("/api/members/M-1008")
        self.assertEqual(len(member["expected"]), 2)
        self.assertEqual(len(member["actual"]), 2)
        plan = self.json_request("/api/plan")
        self.assertEqual(plan["mode"], "dry_run_only")
        self.assertEqual(plan["count"], 12)
        events = self.json_request("/api/events?status=noop")
        self.assertEqual(events["count"], 1)
        asof = self.json_request("/api/asof?status=missing_actual")
        self.assertEqual(asof["count"], 6)

    def test_static_report_and_head(self) -> None:
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"SCD2 Calibration Report", body)
        status, headers, body = self.request("HEAD", "/reports/demo_report.json")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        status, _, body = self.request("GET", "/reports/demo_report.md")
        self.assertEqual(status, 200)
        self.assertIn(b"DIM_OVERLAP", body)

    def test_unknown_path_and_traversal_are_not_served(self) -> None:
        status, _, body = self.request("GET", "/api/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not_found")
        status, _, _ = self.request(
            "GET", "/reports/..%2Fcontracts%2Fdimension_member.json"
        )
        self.assertEqual(status, 404)

    def test_write_methods_are_rejected(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, _, body = self.request(method, "/api/summary")
            self.assertEqual(status, 405, method)
            self.assertEqual(json.loads(body)["error"], "method_not_allowed")

    def test_missing_report_has_stable_error(self) -> None:
        empty = Path(self.temporary.name) / "empty-work"
        empty.mkdir()
        with self.assertRaises(DimhistError) as caught:
            create_server(empty, "127.0.0.1", 0)
        self.assertEqual(caught.exception.code, "SRV_002")


if __name__ == "__main__":
    unittest.main()
