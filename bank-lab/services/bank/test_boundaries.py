"""Focused boundary tests; PostgreSQL concurrency is tested by the integration suite.

Run in the common image: python -m unittest services.bank.test_boundaries -v
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

# Each test process uses its own synthetic secrets and SQLite directory.
_workspace = tempfile.TemporaryDirectory(prefix="company-bank-boundaries-")
_root = Path(_workspace.name)
(_root / "secrets").mkdir()
(_root / "data").mkdir()
_password = secrets.token_urlsafe(24)
_service_key = secrets.token_urlsafe(32)
(_root / "secrets" / "users.json").write_text(json.dumps({
    "alice": {"password": _password, "role": "customer", "customer_id": "c001"}
}), encoding="utf-8")
(_root / "secrets" / "service-key").write_text(_service_key, encoding="utf-8")
os.environ.update(SERVICE_ROLE="auth", DATA_DIR=str(_root / "data"),
                  SECRETS_DIR=str(_root / "secrets"))
os.environ.pop("AUDIT_URL", None)

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .app import app
from . import auth
from .core import Transfer
from .employee import file_for


class AuthenticationBoundaries(unittest.TestCase):
    def setUp(self):
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def login(self):
        response = self.client.post("/login", json={"username": "alice", "password": _password})
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def test_invalid_and_unicode_credentials_never_authenticate(self):
        for username, password in (("alice", "틀린 비밀번호"), ("missing", _password)):
            with self.subTest(username=username):
                response = self.client.post("/login", json={"username": username, "password": password})
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(password, response.text)

    def test_validation_requires_internal_key(self):
        token = self.login()
        response = self.client.post("/validate", headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 403)

    def test_revocation_is_durable_and_token_is_not_stored_plaintext(self):
        token = self.login()
        headers = {"Authorization": "Bearer " + token, "X-Service-Key": _service_key}
        self.assertEqual(self.client.post("/validate", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/logout", headers=headers).status_code, 200)
        self.client_context.__exit__(None, None, None)
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.assertEqual(self.client.post("/validate", headers=headers).status_code, 401)
        with sqlite3.connect(_root / "data" / "auth.db") as connection:
            hashes = [row[0] for row in connection.execute("SELECT token_hash FROM sessions")]
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), hashes)
        self.assertNotIn(token, hashes)

    def test_validation_error_does_not_echo_password(self):
        response = self.client.post("/login", json={"username": "alice", "password": _password,
                                                    "unexpected": _password})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(_password, response.text)

    def test_repeated_validation_releases_sqlite_connections(self):
        original_connect = sqlite3.connect
        opened = []
        def tracked_connect(*args, **kwargs):
            # Allow this test thread to inspect connections made by FastAPI workers.
            kwargs["check_same_thread"] = False
            connection = original_connect(*args, **kwargs)
            opened.append(connection)
            return connection
        with patch.object(auth.sqlite3, "connect", side_effect=tracked_connect):
            token = self.login()
            headers = {"Authorization": "Bearer " + token, "X-Service-Key": _service_key}
            for _ in range(15):
                self.assertEqual(self.client.post("/validate", headers=headers).status_code, 200)
        self.assertEqual(len(opened), 16)
        # Retaining strong references prevents garbage collection from hiding a leak.
        for connection in opened:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                connection.execute("SELECT 1")


class InputBoundaries(unittest.TestCase):
    def test_transfer_amount_rejects_floats_booleans_and_numeric_strings(self):
        for amount in (1.5, True, "100"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                Transfer(request_id="case-1", from_account="A001", to_account="A002",
                         amount_cents=amount)

    def test_employee_filename_stays_in_own_directory(self):
        user = {"username": "teller"}
        for name in ("../auth.db", "/etc/passwd", ".", "..", "nested/file", "\\windows"):
            with self.subTest(name=name), self.assertRaises(HTTPException):
                file_for(user, name)
        path = file_for(user, "report.json")
        self.assertEqual(path.parent, _root / "data" / "employees" / "teller")
        if os.name != "nt":
            path.symlink_to(_root / "secrets" / "users.json")
            with self.assertRaises(HTTPException):
                file_for(user, "report.json")
            path.unlink()


if __name__ == "__main__":
    unittest.main()
