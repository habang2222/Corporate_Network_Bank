"""Meaningful workload boundary checks, isolated from the running bank."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from workloads import run
from workloads.compare import compare


class WorkloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.credentials = self.root / "users.json"
        self.credentials.write_text(json.dumps({name: {"password": "generated-test-only"}
                                                for name in ("alice", "bob", "teller", "auditor", "operator")}))
        self.key = self.root / "service-key"
        self.key.write_text("test-only-key")
        self.args = run.arguments(["--duration", "0.08", "--users", "2", "--rps", "0.001",
                                   "--focus", "customer", "--failure-ratio", "0",
                                   "--output", str(self.root / "results"), "--credentials", str(self.credentials),
                                   "--service-key", str(self.key), "--max-output-mib", "4"])
        self.login_count = 0
        self.runner = None

    def tearDown(self):
        if self.runner:
            self.runner.truth.close()
            self.runner.metrics.close()
        self.temp.cleanup()

    def handler(self, request):
        if request.url.path == "/login":
            self.login_count += 1
            return httpx.Response(200, json={"token": f"session-{self.login_count}"})
        return httpx.Response(200, json={"accounts": [], "status": "completed"})

    def make_runner(self):
        self.runner = run.Runner(self.args)
        return self.runner

    def rows(self):
        return [json.loads(line) for line in (self.runner.directory / "truth.jsonl").read_text().splitlines()]

    async def test_refresh_before_expiry_is_recorded_and_replaces_tokens(self):
        runner = self.make_runner()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as client:
            await runner.setup(client)
            old = dict(runner.tokens)
            due = runner.next_refresh_at
            with patch.object(run.time, "monotonic", return_value=due - 1):
                self.assertFalse(await runner.refresh_if_due(client))
            self.assertEqual(self.login_count, 5)
            with patch.object(run.time, "monotonic", return_value=due):
                self.assertTrue(await runner.refresh_if_due(client))
            self.assertEqual(self.login_count, 10)
            self.assertLess(run.SESSION_REFRESH_SECONDS, 3600)
            self.assertEqual(runner.setup_requests, 10)
            self.assertTrue(all(old[name] != runner.tokens[name] for name in old))
        results = [row for row in self.rows() if row["record_type"] == "result"]
        self.assertEqual(len({row["operation_id"] for row in results}), 10)
        self.assertEqual(sum(row["operation_type"] == "session_refresh" for row in results), 5)
        self.assertTrue(all(row["phase"] == "setup" and row["success"] for row in results))
        self.assertNotIn("generated-test-only", (runner.directory / "truth.jsonl").read_text())
        self.assertNotIn("session-10", (runner.directory / "truth.jsonl").read_text())

    async def test_low_rps_does_not_wait_past_duration(self):
        runner = self.make_runner()
        real_client = httpx.AsyncClient
        started = time.monotonic()
        with patch.object(run.httpx, "AsyncClient", side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(self.handler), **kw)):
            result = await asyncio.wait_for(runner.run(), timeout=2)
        self.assertEqual(result, 0)
        self.assertLess(time.monotonic() - started, 2)
        summary = json.loads((runner.directory / "summary.json").read_text())
        self.assertEqual(summary["total_requests"], 1)
        self.assertEqual(summary["stop_reason"], "duration_complete")
        self.assertIsNone(summary["collector"]["collected_unique_events"])

    async def test_storage_budget_stops_before_new_work_preserves_truth(self):
        runner = self.make_runner()
        runner.truth.write({"record_type": "test_evidence", "evidence": "must-remain"})
        with (runner.directory / "budget-fixture").open("wb") as handle:
            handle.truncate(4 * 1024 * 1024)
        self.assertFalse(runner.has_storage_headroom())
        real_client = httpx.AsyncClient
        with patch.object(run.httpx, "AsyncClient", side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(self.handler), **kw)):
            self.assertEqual(await runner.run(), 0)
        summary = json.loads((runner.directory / "summary.json").read_text())
        self.assertEqual(summary["stop_reason"], "storage_budget")
        self.assertEqual(summary["total_requests"], 0)
        self.assertEqual(self.rows()[0]["evidence"], "must-remain")
        self.assertEqual(self.rows()[-1]["record_type"], "run_end")

    async def test_low_filesystem_space_stops_generation(self):
        runner = self.make_runner()
        with patch.object(run.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1024})()):
            self.assertFalse(runner.has_storage_headroom())
        self.assertEqual(runner.completed, 0)

    def test_seed_reproduces_slot_plan_and_presets_allow_overrides(self):
        first = [run.plan(index, self.args, "run") for index in range(20)]
        second = [run.plan(index, self.args, "run") for index in range(20)]
        self.assertEqual(first, second)
        self.assertTrue(all(item["service"] in run.ALLOWED_HOSTS for item in first))
        args = run.arguments(["--preset", "burst", "--duration", "3", "--rps", "2"])
        self.assertEqual((args.scenario, args.duration, args.rps), ("burst", 3, 2))

    def test_comparison_rejects_different_conditions_and_never_fills_collector_metrics(self):
        baseline = {"seed": 42, "users": 2, "target_rps": 5, "requested_duration_seconds": 5,
                    "scenario": "normal", "focus": "customer", "failure_ratio_requested": 0,
                    "user_types": ["customer"], "max_output_mib": 128, "session_refresh_seconds": 1800,
                    "error": None, "stop_reason": "duration_complete", "total_requests": 25,
                    "actual_rps": 5, "error_rate": 0, "latency_ms": {"mean": 10, "p50": 9, "p95": 20, "p99": 25}}
        comparison = dict(baseline, actual_rps=4)
        result = compare(baseline, comparison, "operator-supplied")
        self.assertEqual(result["service_deltas"]["actual_rps"]["percent_delta"], -20)
        self.assertIsNone(result["collector"]["metrics"])
        with self.assertRaises(ValueError):
            compare(baseline, dict(comparison, seed=9), "bad")
        with self.assertRaises(ValueError):
            compare(baseline, dict(comparison, stop_reason="storage_budget"), "incomplete")


if __name__ == "__main__":
    unittest.main()
