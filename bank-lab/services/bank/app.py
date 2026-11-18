"""One small image, four separate independently networked service processes."""
from contextlib import asynccontextmanager
import importlib
import os
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .common import CORRELATION, DATA, ROLE, audit

if ROLE not in {"auth", "customer", "core", "employee"}:
    raise RuntimeError("unknown SERVICE_ROLE")
service = importlib.import_module("services.bank." + ROLE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA.mkdir(parents=True, exist_ok=True)
    if hasattr(service, "initialize"):
        service.initialize()
    audit("service_started", pid=os.getpid())
    yield
    audit("service_stopped", pid=os.getpid())


app = FastAPI(title="llooggeerr synthetic bank: " + ROLE,
              docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.include_router(service.router)


@app.get("/health")
def health():
    if ROLE == "core":
        with service.db() as connection:
            connection.execute("SELECT 1")
    return {"status": "ok", "service": ROLE, "pid": os.getpid()}


@app.middleware("http")
async def log_request(request: Request, call_next):
    started = time.monotonic_ns()
    marker = CORRELATION.set({"operation_id": request.headers.get("X-Operation-ID", "")[:128],
                              "run_id": request.headers.get("X-Run-ID", "")[:128]})
    try:
        response = await call_next(request)
        if request.url.path != "/health":
            audit("http_request", method=request.method, path=request.url.path,
                  status=response.status_code, elapsed_ns=time.monotonic_ns() - started)
        return response
    finally:
        CORRELATION.reset(marker)


@app.exception_handler(RequestValidationError)
async def validation_failure(request: Request, exc: RequestValidationError):
    # Do not echo input or passwords into responses/logs.
    errors = [{"loc": error["loc"], "type": error["type"], "msg": error["msg"]}
              for error in exc.errors()]
    audit("request_rejected", path=request.url.path, errors=errors)
    return JSONResponse({"detail": errors}, 422)
