"""Small, explicit trust boundaries shared by the bank laboratory services."""
import hmac
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request

SECRETS = Path(os.getenv("SECRETS_DIR", "/secrets"))
DATA = Path(os.getenv("DATA_DIR", "/data"))
ROLE = os.getenv("SERVICE_ROLE", "customer")
AUTH_URL = os.getenv("AUTH_URL", "http://auth:8080")
CORE_URL = os.getenv("CORE_URL", "http://core:8080")
CORRELATION: ContextVar[dict] = ContextVar("bank_request_correlation", default={})


def secret(name: str) -> str:
    return (SECRETS / name).read_text(encoding="utf-8").strip()


def audit(event: str, **fields: Any) -> None:
    print(json.dumps({"source": "application", "service": ROLE, "event": event,
                      "time_ns": time.time_ns(), **CORRELATION.get(), **fields}, ensure_ascii=False), flush=True)


def bearer(request: Request) -> str:
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer ") or not value[7:].strip():
        raise HTTPException(401, "bearer token required")
    return value[7:].strip()


def internal(request: Request) -> None:
    supplied = request.headers.get("X-Service-Key", "")
    if not hmac.compare_digest(supplied.encode(), secret("service-key").encode()):
        raise HTTPException(403, "internal service credential required")


def call(service: str, method: str, path: str, *, token: str | None = None,
         body: Any = None) -> httpx.Response:
    # All paths and upstream service locations come from this program/configuration.
    headers = {"X-Service-Key": secret("service-key")}
    correlation = CORRELATION.get()
    for key, header in (("operation_id", "X-Operation-ID"), ("run_id", "X-Run-ID")):
        if correlation.get(key):
            headers[header] = correlation[key]
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        return httpx.request(method, service + path, json=body, headers=headers,
                             timeout=httpx.Timeout(10.0, connect=2.0), trust_env=False)
    except httpx.HTTPError as exc:
        audit("upstream_failure", upstream=service, error=type(exc).__name__)
        raise HTTPException(503, "upstream service unavailable") from exc


def response_data(response: httpx.Response) -> Any:
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(502, "invalid upstream response") from exc
    if response.is_error:
        raise HTTPException(response.status_code, result.get("detail", result))
    return result


def identity(request: Request, allowed: set[str] | None = None,
             require_internal: bool = False) -> dict:
    if require_internal:
        internal(request)
    user = response_data(call(AUTH_URL, "POST", "/validate", token=bearer(request)))
    if allowed is not None and user["role"] not in allowed:
        audit("access_denied", username=user["username"], path=request.url.path)
        raise HTTPException(403, "role is not permitted for this operation")
    return user
