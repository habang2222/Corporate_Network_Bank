"""Seeded, bounded workload planner with independent write-ahead truth records."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import signal
import shutil
import sqlite3
import time
import uuid

import httpx

ALLOWED_HOSTS = {"web", "employee", "events"}
SESSION_REFRESH_SECONDS = 1800


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def redact(value):
    if isinstance(value, dict):
        return {k: "<redacted>" if k in {"password", "token", "authorization", "service_key"}
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Truth:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=False)
        self.handle = (directory / "truth.jsonl").open("x", encoding="utf-8")

    def write(self, row):
        self.handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.handle.flush()
        # Each intent is on the filesystem before sending its request. This costs I/O
        # and is deliberately measured as part of generator capacity, not hidden.
        os.fsync(self.handle.fileno())

    def close(self):
        self.handle.close()


def plan(index, args, run_id):
    rng = random.Random(f"{args.seed}:{index}")
    user_id = index % args.users
    user_type = args.user_types[user_id % len(args.user_types)]
    if args.focus != "all":
        user_type = args.focus if args.focus in ("customer", "employee", "events") else "events"
    if args.scenario == "hotspot" and args.focus == "all":
        user_type = "events"
    elif args.scenario == "critical" and index % 2 == 0:
        user_type = "events"
    failed = rng.random() < args.failure_ratio
    operation_id = f"{run_id}-{index:08d}"
    common = {"operation_id": operation_id, "user_id": user_id,
              "scenario": args.scenario, "target_rps": args.rps, "phase": "workload",
              "scenario_label": "abnormal" if failed else "normal"}
    if user_type == "customer":
        who = rng.choice(["alice", "bob"])
        account, other = ("A001", "A002") if who == "alice" else ("A002", "A001")
        if failed:
            choice = rng.choice(["bad_login", "unauthorized_account", "malformed_transfer"])
            if choice == "bad_login":
                return dict(common, type=choice, service="web", username=who, method="POST",
                            path="/login", body={"username": who, "password": "intentionally-invalid"},
                            expected_status=[401], use_token=False)
            if choice == "unauthorized_account":
                return dict(common, type=choice, service="web", username=who, method="GET",
                            path=f"/accounts/{other}", body=None, expected_status=[403], use_token=True)
            return dict(common, type=choice, service="web", username=who, method="POST",
                        path="/transfers", body={"amount_cents": "invalid"},
                        expected_status=[422], use_token=True)
        choice = rng.choice(["accounts", "balance", "transactions", "transfer"])
        if choice == "transfer":
            return dict(common, type=choice, service="web", username=who, method="POST",
                        path="/transfers", body={"request_id": operation_id, "from_account": account,
                                                 "to_account": other, "amount_cents": 1},
                        expected_status=[200, 201], use_token=True)
        path = "/accounts" if choice == "accounts" else f"/accounts/{account}"
        if choice == "transactions":
            path += "/transactions"
        return dict(common, type=choice, service="web", username=who, method="GET", path=path,
                    body=None, expected_status=[200], use_token=True)
    if user_type == "employee":
        if failed:
            return dict(common, type="employee_access_denied", service="employee", username="auditor",
                        method="GET", path="/customers", body=None, expected_status=[403], use_token=True)
        choice = rng.choice(["customers", "report", "operation"])
        if choice == "customers":
            return dict(common, type=choice, service="employee", username="teller", method="GET",
                        path="/customers", body=None, expected_status=[200], use_token=True)
        if choice == "report":
            return dict(common, type=choice, service="employee", username="auditor", method="POST",
                        path="/reports", body={}, expected_status=[200, 201], use_token=True)
        return dict(common, type="employee_file_cycle", service="employee", username="operator",
                    method="POST", path="/operations", body={"operation": "file-cycle"},
                    expected_status=[200], use_token=True)
    kind = args.focus if args.focus in ("file", "process", "permission", "network", "cpu", "memory", "disk") else rng.choice(["file", "process", "permission", "network"])
    if args.scenario == "critical" and args.focus == "all":
        kind = "permission"
    if failed:
        return dict(common, type="invalid_event_request", service="events", username=None,
                    method="POST", path="/run", body={"kind": "not-an-operation"},
                    expected_status=[422], use_token=False)
    return dict(common, type=f"event_{kind}", service="events", username=None, method="POST",
                path="/run", body={"kind": kind, "operation_id": operation_id},
                expected_status=[200], use_token=False)


class Runner:
    def __init__(self, args):
        self.args = args
        self.run_id = str(uuid.uuid4())
        self.directory = args.output / self.run_id
        self.truth = Truth(self.directory)
        self.tokens = {}
        self.key = args.service_key.read_text().strip()
        self.users = json.loads(args.credentials.read_text())
        self.stop = asyncio.Event()
        self.completed = self.success_count = self.expected_count = self.setup_requests = 0
        # Keep every latency on disk for exact percentiles without retaining all
        # response bodies or a growing latency array in the generator's RAM.
        self.metrics = sqlite3.connect(self.directory / "metrics.sqlite")
        self.metrics.execute("CREATE TABLE measurements (latency_ms REAL NOT NULL)")
        self.skipped = 0
        self.scheduled = 0
        self.started_at = utcnow()
        self.stop_reason = "duration_complete"
        self.session_generation = 0
        self.next_refresh_at = float("inf")

    def request_stop(self):
        self.stop_reason = "signal_received"
        self.stop.set()

    async def execute(self, client, item, setup=False):
        # All URLs are fixed to lab DNS names; arbitrary external endpoints are not accepted.
        if item["service"] not in ALLOWED_HOSTS:
            raise ValueError("target is outside the isolated lab")
        started = time.monotonic()
        base = {"run_id": self.run_id, "operation_id": item["operation_id"], "operation_type": item["type"],
                "requested_service": item["service"], "timestamp": utcnow(),
                "target": item["path"], "method": item["method"], "attempted": True,
                "scenario": self.args.scenario, "scenario_label": item.get("scenario_label", "normal"),
                "phase": "setup" if setup else "workload", "target_rps": self.args.rps,
                "effective_target_rps": item.get("effective_target_rps", self.args.rps),
                "user_id": item.get("user_id"), "expected_status": item["expected_status"],
                "request": redact(item.get("body")), "source": "independent_workload"}
        self.truth.write(dict(base, record_type="intent", success=None, return_code=None,
                              error_reason=None, actual_completed_so_far=self.completed))
        headers = {"X-Operation-ID": item["operation_id"], "X-Run-ID": self.run_id}
        if item["service"] == "events":
            headers["X-Service-Key"] = self.key
        if item.get("use_token"):
            headers["Authorization"] = "Bearer " + self.tokens[item["username"]]
        response_body = None
        request_started = time.monotonic()
        try:
            response = await client.request(item["method"], f"http://{item['service']}:8080{item['path']}",
                                            json=item.get("body"), headers=headers)
            status = response.status_code
            try:
                response_body = response.json()
            except ValueError:
                response_body = {"text": response.text[:4096]}
            success = 200 <= status < 300
            expected = status in item["expected_status"]
            if item["service"] == "events" and success and response_body.get("success") is False:
                expected = False
            error = None if success else (response_body.get("detail") or response_body.get("error") or "http_error")
        except httpx.HTTPError as exc:
            status, success, expected, error = None, False, False, f"{type(exc).__name__}: {exc}"
        latency = (time.monotonic() - request_started) * 1000
        result = dict(base, record_type="result", finished_at=utcnow(), success=success,
                      expectation_met=expected, return_code=status, error_reason=error,
                      operation_duration_ms=(time.monotonic() - started) * 1000,
                      latency_ms=latency, response=redact(response_body),
                      actual_completed_so_far=self.completed + (0 if setup else 1))
        self.truth.write(result)
        if setup:
            self.setup_requests += 1
        else:
            self.completed += 1
            self.success_count += success
            self.expected_count += expected
            self.metrics.execute("INSERT INTO measurements VALUES (?)", (latency,))
            self.metrics.commit()
        return result, response_body

    async def setup(self, client):
        generation = self.session_generation
        for username in ("alice", "bob", "teller", "auditor", "operator"):
            service = "web" if username in ("alice", "bob") else "employee"
            result, body = await self.execute(client, {
                "operation_id": f"{self.run_id}-login-{generation}-{username}",
                "type": "session_refresh" if generation else "login", "service": service,
                "method": "POST", "path": "/login", "username": username, "use_token": False,
                "body": {"username": username, "password": self.users[username]["password"]},
                "expected_status": [200]}, setup=True)
            if not result["success"] or not isinstance(body, dict) or "token" not in body:
                raise RuntimeError(f"setup login failed for {username}: {result['return_code']}")
            self.tokens[username] = body["token"]
        self.session_generation += 1
        self.next_refresh_at = time.monotonic() + SESSION_REFRESH_SECONDS

    async def refresh_if_due(self, client):
        if time.monotonic() < self.next_refresh_at:
            return False
        await self.setup(client)
        return True

    def has_storage_headroom(self):
        # Reserve space for all in-flight results, metric pages, and final summary.
        # This bounds new generation, never drops a result from an attempted request.
        reserve = (self.args.users + 1) * 256 * 1024
        used = sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file())
        return (used + reserve < self.args.max_output_mib * 1024 * 1024
                and shutil.disk_usage(self.directory).free > reserve + 8 * 1024 * 1024)

    def multiplier(self, elapsed):
        if self.args.scenario == "burst" and self.args.duration * 0.4 <= elapsed < self.args.duration * 0.6:
            return 5
        return 1

    async def run(self):
        started = time.monotonic()
        measured_start = None
        pending = set()
        error = None
        try:
            timeout = httpx.Timeout(10, connect=2)
            async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=self.args.users),
                                         trust_env=False) as client:
                await self.setup(client)
                measured_start = time.monotonic()
                next_slot = measured_start
                index = 0
                while not self.stop.is_set():
                    instant = time.monotonic()
                    elapsed = instant - measured_start
                    if elapsed >= self.args.duration:
                        break
                    if not self.has_storage_headroom():
                        self.stop_reason = "storage_budget"
                        break
                    if instant >= self.next_refresh_at:
                        if pending:
                            await asyncio.gather(*pending)
                            pending.clear()
                        await self.refresh_if_due(client)
                        continue
                    if instant < next_slot:
                        try:
                            wake_at = min(next_slot, measured_start + self.args.duration, self.next_refresh_at)
                            await asyncio.wait_for(self.stop.wait(), timeout=max(0, wake_at - instant))
                        except asyncio.TimeoutError:
                            pass
                        continue
                    item = plan(index, self.args, self.run_id)
                    item["effective_target_rps"] = self.args.rps * self.multiplier(elapsed)
                    index += 1
                    self.scheduled += 1
                    done = {task for task in pending if task.done()}
                    for task in done:
                        task.result()  # A truth/storage failure must stop the run visibly.
                    pending -= done
                    if len(pending) >= self.args.users:
                        self.skipped += 1
                        self.truth.write({"record_type": "schedule_skip", "run_id": self.run_id,
                                          "operation_id": item["operation_id"], "operation_type": item["type"],
                                          "requested_service": item["service"], "target": item["path"],
                                          "timestamp": utcnow(), "attempted": False, "success": None,
                                          "return_code": None, "error_reason": "concurrency_capacity",
                                          "scenario": self.args.scenario, "target_rps": self.args.rps,
                                          "source": "independent_workload"})
                    else:
                        pending.add(asyncio.create_task(self.execute(client, item)))
                    # Never catch up an unbounded backlog after a stalled generator.
                    interval = 1 / (self.args.rps * self.multiplier(elapsed))
                    next_slot = max(next_slot + interval, time.monotonic())
                if pending:
                    await asyncio.gather(*pending)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.stop_reason = "runner_error"
            # Preserve in-flight results before closing truth where possible.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        elapsed = time.monotonic() - (measured_start or started)
        def exact_percentile(percent):
            if not self.completed:
                return None
            index = (self.completed - 1) * percent / 100
            lower, upper = math.floor(index), math.ceil(index)
            a = self.metrics.execute("SELECT latency_ms FROM measurements ORDER BY latency_ms LIMIT 1 OFFSET ?", (lower,)).fetchone()[0]
            b = self.metrics.execute("SELECT latency_ms FROM measurements ORDER BY latency_ms LIMIT 1 OFFSET ?", (upper,)).fetchone()[0]
            return a + (b - a) * (index - lower)
        mean = self.metrics.execute("SELECT AVG(latency_ms) FROM measurements").fetchone()[0]
        multiplier_integral = 1.8 if self.args.scenario == "burst" else 1.0
        summary = {
            "run_id": self.run_id, "scenario": self.args.scenario, "started_at": self.started_at,
            "finished_at": utcnow(), "seed": self.args.seed, "users": self.args.users,
            "user_types": self.args.user_types, "focus": self.args.focus,
            "requested_duration_seconds": self.args.duration, "actual_duration_seconds": elapsed,
            "target_rps": self.args.rps, "target_peak_rps": self.args.rps * (5 if self.args.scenario == "burst" else 1),
            "target_request_count": self.args.duration * self.args.rps * multiplier_integral,
            "scheduled_slots": self.scheduled, "skipped_capacity": self.skipped,
            "total_requests": self.completed, "success_count": self.success_count,
            "failure_count": self.completed - self.success_count, "expected_outcome_count": self.expected_count,
            "unexpected_outcome_count": self.completed - self.expected_count,
            "error_rate": (self.completed - self.success_count) / self.completed if self.completed else None,
            "actual_rps": self.completed / elapsed if elapsed else None,
            "latency_ms": {"mean": mean, "p50": exact_percentile(50),
                           "p95": exact_percentile(95), "p99": exact_percentile(99)},
            "failure_ratio_requested": self.args.failure_ratio,
            "setup_requests": self.setup_requests, "stop_reason": self.stop_reason,
            "session_refreshes": max(self.session_generation - 1, 0),
            "session_refresh_seconds": SESSION_REFRESH_SECONDS,
            "max_output_mib": self.args.max_output_mib,
            "error": error, "collector": {"status": "not_integrated", "ground_truth_event_count": None,
              "collected_unique_events": None, "storage_confirmed_events": None,
              "critical_retention_rate": None, "general_retention_rate": None,
              "duplicate_events": None, "uncertain_events": None, "loss_by_cause": None,
              "receive_latency_ms": None, "storage_latency_ms": None, "cpu": None,
              "memory_bytes": None, "disk_io_bytes": None, "queue_backlog": None,
              "recovery_seconds": None},
            "service_resources": {"status": "external_docker_stats_required", "cpu": None, "memory": None},
            "truth_file": "truth.jsonl", "truth_semantics": "intent/result pairs; unmatched intent is uncertain",
            "actual_request_scope": "workload HTTP calls, excludes separately counted setup logins",
            "success_semantics": "HTTP 2xx; expectation_met separately checks intended rejection and event outcomes",
            "latency_scope": "HTTP client request through response; truth fsync excluded",
            "reproducibility": "seed fixes planned operation per slot, not concurrent scheduling or DB state",
        }
        self.truth.write({"record_type": "run_end", "run_id": self.run_id, "timestamp": utcnow(),
                          "actual_requests": self.completed, "actual_rps": summary["actual_rps"],
                          "stop_reason": self.stop_reason, "error": error})
        self.truth.close()
        self.metrics.close()
        destination = self.directory / "summary.json"
        destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"run_id": self.run_id, "summary": str(destination), "requests": self.completed,
                          "actual_rps": summary["actual_rps"], "error": error}), flush=True)
        return 1 if error else 0


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    presets = ["normal", "burst", "sustained", "hotspot", "critical", "multi", "cpu", "memory", "disk"]
    parser.add_argument("--preset", choices=presets, help="load a versioned scenario JSON before CLI overrides")
    parser.add_argument("--duration", type=float, default=15)
    parser.add_argument("--users", type=int, default=4)
    parser.add_argument("--rps", type=float, default=5)
    parser.add_argument("--failure-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scenario", choices=["normal", "burst", "sustained", "hotspot", "critical", "multi"], default="normal")
    parser.add_argument("--focus", choices=["all", "customer", "employee", "events", "file", "process", "permission", "network", "cpu", "memory", "disk"], default="all")
    parser.add_argument("--user-types", default="customer,employee,events",
                        help="round-robin virtual user roles: customer,employee,events")
    parser.add_argument("--output", type=Path, default=Path("/results"))
    parser.add_argument("--credentials", type=Path, default=Path("/secrets/users.json"))
    parser.add_argument("--service-key", type=Path, default=Path("/secrets/service-key"))
    parser.add_argument("--max-output-mib", type=int, default=128,
                        help="stop new work with reserved result headroom; preserve existing truth")
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--preset", choices=presets)
    selected, _ = pre.parse_known_args(argv)
    if selected.preset:
        preset = Path(__file__).resolve().parents[1] / "scenarios" / f"{selected.preset}.json"
        parser.set_defaults(**json.loads(preset.read_text(encoding="utf-8"))["parameters"])
    args = parser.parse_args(argv)
    args.user_types = args.user_types.split(",")
    if not 0 < args.duration <= 86400 or not 1 <= args.users <= 64 or not 0 < args.rps <= 2000:
        parser.error("duration must be (0,86400], users [1,64], rps (0,2000]")
    if not 0 <= args.failure_ratio <= 1 or not args.user_types or any(t not in ("customer", "employee", "events") for t in args.user_types):
        parser.error("invalid failure ratio or user types")
    if not 4 <= args.max_output_mib <= 1024:
        parser.error("max-output-mib must be [4,1024]")
    return args


async def main_async(args):
    runner = Runner(args)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: runner.request_stop())
    return await runner.run()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(arguments())))
