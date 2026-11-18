"""Storage semantics and malformed-key regression tests for the mock receiver."""
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from services.receiver import app as receiver
from services.events import app as events


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.key = root / "service-key"
        self.key.write_text("test-only-key")
        self.patches = [patch.object(receiver, "DB", root / "receiver.db"),
                        patch.object(receiver, "KEY_FILE", self.key), patch.object(events, "KEY_FILE", self.key)]
        for item in self.patches:
            item.start()
        with receiver.control_lock:
            receiver.controls.update(delay_seconds=0, disconnect=False, write_failure=False)
        self.headers = {"X-Service-Key": "test-only-key"}
        self.message = {"server_id": "unit", "event_id": "event-1", "payload": {"original": "synthetic"}}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_connection_is_closed_after_context_and_errors_rollback(self):
        with TestClient(receiver.app):
            with receiver.connect() as con:
                con.execute("CREATE TABLE boundary (value TEXT)")
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")
            with self.assertRaises(RuntimeError):
                with receiver.connect() as transactional:
                    transactional.execute("INSERT INTO boundary VALUES ('must rollback')")
                    raise RuntimeError("abort transaction")
            with receiver.connect() as current:
                self.assertEqual(current.execute("SELECT COUNT(*) FROM boundary").fetchone()[0], 0)

    def test_restart_duplicate_conflict_and_raw_body_preservation(self):
        raw = b'{ "server_id":"unit", "event_id":"event-1", "payload": {"original":"synthetic"} }'
        with TestClient(receiver.app) as client:
            first = client.post("/events", content=raw, headers=self.headers).json()
            self.assertTrue(first["stored"])
            self.assertIn("storage_ack_at", first)
        with TestClient(receiver.app) as client:
            duplicate = client.post("/events", json=self.message, headers=self.headers)
            self.assertEqual(duplicate.status_code, 200)
            self.assertTrue(duplicate.json()["duplicate"])
            conflict = client.post("/events", json=dict(self.message, payload={"different": True}), headers=self.headers)
            self.assertEqual(conflict.status_code, 409)
            original = client.get("/events/unit/event-1", headers=self.headers).json()
            self.assertEqual(original["payload"], self.message["payload"])
            with receiver.connect() as con:
                self.assertEqual(con.execute("SELECT raw_body FROM events").fetchone()[0], raw)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 3)

    def test_received_but_failed_storage_retries_after_recovery(self):
        with TestClient(receiver.app) as client:
            client.post("/control", json={"write_failure": True}, headers=self.headers).raise_for_status()
            failed = client.post("/events", json=self.message, headers=self.headers)
            self.assertEqual(failed.status_code, 507)
            self.assertTrue(failed.json()["received"])
            self.assertFalse(failed.json()["stored"])
            self.assertEqual(client.get("/events/unit/event-1", headers=self.headers).status_code, 404)
            with receiver.connect() as con:
                self.assertEqual(con.execute("SELECT state FROM receipts").fetchone()[0], "storage_failed")
            client.post("/control", json={}, headers=self.headers).raise_for_status()
            self.assertEqual(client.post("/events", json=self.message, headers=self.headers).status_code, 201)

    def test_concurrent_duplicates_commit_one_unique_event(self):
        with TestClient(receiver.app) as client:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                statuses = list(pool.map(lambda _: client.post("/events", json=self.message,
                                                               headers=self.headers).status_code, range(8)))
            self.assertEqual(sorted(statuses), [200] * 7 + [201])
            status = client.get("/status", headers=self.headers).json()
            self.assertEqual(status["committed_unique_events"], 1)
            self.assertEqual(status["receipt_states"], {"committed": 1, "duplicate": 7})

    def test_non_ascii_keys_are_unauthorized_not_internal_errors(self):
        for module in (receiver, events):
            with self.assertRaises(HTTPException) as error:
                module.authorize("비밀값")
            self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
