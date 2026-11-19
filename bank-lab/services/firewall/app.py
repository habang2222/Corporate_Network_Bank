"""The only host-published entry point: fixed routes and a fixed upstream."""
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

WEB_URL = os.getenv("WEB_URL", "http://web:8080")
AUDIT_URL = os.getenv("AUDIT_URL", "")
RULE_FILE = Path(os.getenv("EDGE_ROUTES_FILE", "/config/edge-routes.json"))


def load_rules():
    entries = json.loads(RULE_FILE.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("firewall route policy is empty")
    return tuple((entry["method"], re.compile(entry["path_regex"])) for entry in entries)


RULES = load_rules()


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def audit(request: Request, decision: str, status: int,
                response_bytes: int | None = None) -> None:
    record = {"event_id": uuid4().hex, "source": "edge", "service": "firewall",
              "event": "request", "time_ns": time.time_ns(),
              "method": request.method, "path": request.url.path,
              "client": request.client.host if request.client else None,
              "decision": decision, "status": status,
              "response_bytes": response_bytes,
              "operation_id": request.headers.get("X-Operation-ID", "")[:128]}
    print(json.dumps(record, ensure_ascii=False), flush=True)
    if AUDIT_URL:
        try:
            key = Path("/secrets/service-key").read_text(encoding="utf-8").strip()
            response = await request.app.state.client.post(
                AUDIT_URL + "/events", json=record, headers={"X-Service-Key": key},
                timeout=0.5)
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            print(json.dumps({"source": "audit_delivery", "service": "firewall",
                              "event_id": record["event_id"], "error": type(exc).__name__}), flush=True)


@app.get("/health")
def health():
    return {"status": "ok", "service": "firewall"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def ingress(path: str, request: Request):
    allowed = any(request.method == method and pattern.fullmatch(request.url.path)
                  for method, pattern in RULES)
    if not allowed:
        await audit(request, "deny_route", 404)
        return JSONResponse({"detail": "route not exposed"}, status_code=404)
    body = await request.body()
    if len(body) > 65536:
        await audit(request, "deny_size", 413)
        return JSONResponse({"detail": "request too large"}, status_code=413)
    headers = {name: value for name, value in request.headers.items()
               if name.lower() in {"authorization", "content-type", "x-operation-id", "x-run-id"}}
    try:
        upstream = await request.app.state.client.request(
            request.method, WEB_URL + request.url.path,
            params=request.query_params, headers=headers, content=body)
    except httpx.HTTPError:
        await audit(request, "upstream_error", 503)
        return JSONResponse({"detail": "web service unavailable"}, status_code=503)
    await audit(request, "allow", upstream.status_code, len(upstream.content))
    return Response(upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"))
