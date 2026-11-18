"""Executed through stdin inside a selected container, never through a shell."""
import concurrent.futures
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

records = []


def check(name, expected, actual, passed=None):
    records.append({"name": name, "expected": expected, "actual": actual,
                    "passed": expected == actual if passed is None else bool(passed)})


def request(base, path, method="GET", body=None, token=None, internal=False, timeout=12):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if internal:
        headers["X-Service-Key"] = Path("/secrets/service-key").read_text().strip()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return 0, {"error": type(exc).__name__}, time.monotonic() - started
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        value = {"body_preview": raw[:160].decode(errors="replace")}
    return status, value, time.monotonic() - started


def login(base, username):
    users = json.loads(Path("/secrets/users.json").read_text())
    status, value, _ = request(base, "/login", "POST", {
        "username": username, "password": users[username]["password"]})
    if status != 200 or not value.get("token"):
        raise RuntimeError("login failed for " + username + ": " + str(status))
    return value["token"]


def balance(base, token, account):
    status, value, _ = request(base, "/accounts/" + account, token=token)
    if status != 200:
        raise RuntimeError("balance failed: " + str(status))
    return value["balance_cents"]


def tcp(host, port):
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def finish(extra=None):
    print("LAB_RESULT=" + json.dumps({"records": records, "extra": extra}, ensure_ascii=False))
