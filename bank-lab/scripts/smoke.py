"""Small ordinary-traffic smoke check; no load or fault generation."""
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx


def credentials(name):
    return json.loads(Path("/secrets/users.json").read_text(encoding="utf-8"))[name]["password"]


def request(method, url, **kwargs):
    response = httpx.request(method, url, timeout=10, trust_env=False, **kwargs)
    response.raise_for_status()
    return response.json()


def customer():
    base = "http://127.0.0.1:8080"
    login = request("POST", base + "/login",
                    json={"username": "alice", "password": credentials("alice")})
    headers = {"Authorization": "Bearer " + login["token"]}
    accounts = request("GET", base + "/accounts", headers=headers)
    assert accounts, "customer account listing is empty"
    request("GET", base + "/accounts/A001/transactions", headers=headers)
    denied = httpx.get(base + "/admin", timeout=10, trust_env=False)
    assert denied.status_code == 404, denied.status_code
    request("POST", base + "/logout", headers=headers)
    print("customer and edge firewall: ok")


def staff():
    login = request("POST", "http://employee:8080/login",
                    json={"username": "teller", "password": credentials("teller")})
    headers = {"Authorization": "Bearer " + login["token"]}
    base = "http://office:8080"
    request("POST", base + "/messages", headers=headers,
            json={"recipient": "operator", "subject": "Routine office note",
                  "body": "Synthetic company mail for service verification."})
    filename = "smoke-" + uuid4().hex + ".txt"
    try:
        written = request("PUT", base + "/shared/" + filename, headers=headers,
                          json={"content": "Shared office document"})
        read = request("GET", base + "/shared/" + filename, headers=headers)
        assert read["sha256"] == written["sha256"]
    finally:
        request("DELETE", base + "/shared/" + filename, headers=headers)
    request("POST", "http://employee:8080/logout", headers=headers)
    print("employee mail and shared documents: ok")


def audit():
    key = Path("/secrets/service-key").read_text(encoding="utf-8").strip()
    events = request("GET", "http://127.0.0.1:8080/events",
                     headers={"X-Service-Key": key}, params={"limit": 500})["events"]
    sources = {item["record"]["service"] for item in events}
    assert {"firewall", "office"}.issubset(sources), sources
    print("central security audit: ok")


if __name__ == "__main__":
    {"customer": customer, "staff": staff, "audit": audit}[sys.argv[1]]()
