"""Role-specific employee workflows."""
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import Login
from .common import AUTH_URL, CORE_URL, DATA, audit, bearer, call, identity, response_data
from .core import NewAccount

router = APIRouter()
EMPLOYEES = {"teller", "auditor", "operator"}


def root_for(user: dict) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", user["username"]):
        raise HTTPException(500, "invalid provisioned username")
    root = DATA / "employees" / user["username"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def file_for(user: dict, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
        raise HTTPException(422, "filename must be a plain basename")
    path = root_for(user) / name
    if path.is_symlink():
        raise HTTPException(403, "symbolic links are not permitted")
    return path


def activity(request, user, action, detail):
    audit("employee_action", username=user["username"], action=action, detail=detail)
    try:
        return {"recorded": True, **response_data(call(CORE_URL, "POST", "/employee-actions",
                 token=bearer(request), body={"action": action, "detail": detail}))}
    except HTTPException as exc:
        # The file action may already have completed; keep its outcome visible.
        audit("employee_audit_failure", username=user["username"], action=action,
              http_status=exc.status_code)
        return {"recorded": False, "http_status": exc.status_code, "reason": "core audit unavailable"}


@router.post("/login")
def login(body: Login):
    result = response_data(call(AUTH_URL, "POST", "/login", body=body.model_dump()))
    if result["user"]["role"] not in EMPLOYEES:
        call(AUTH_URL, "POST", "/logout", token=result["token"])
        raise HTTPException(403, "employee login required")
    return result


@router.post("/logout")
def logout(request: Request):
    identity(request, EMPLOYEES)
    return response_data(call(AUTH_URL, "POST", "/logout", token=bearer(request)))


@router.get("/customers")
def customers(request: Request):
    identity(request, {"teller"})
    return response_data(call(CORE_URL, "GET", "/customers", token=bearer(request)))


@router.post("/reports")
def report(request: Request):
    user = identity(request, {"teller", "auditor"})
    data = response_data(call(CORE_URL, "GET", "/report-data", token=bearer(request)))
    report_id = uuid4().hex
    name = "report-" + report_id + ".json"
    report = {"report_id": report_id, "created_ns": time.time_ns(),
              "created_by": user["username"], "source": "application_database", **data}
    target = file_for(user, name)
    temporary = target.with_suffix(".tmp")
    encoded = json.dumps(report, ensure_ascii=False, indent=2).encode()
    with open(temporary, "xb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)
    result = {"report_id": report_id, "filename": name, "bytes": len(encoded),
              "storage_status": "file_fsync_completed"}
    result["audit"] = activity(request, user, "report_created", result.copy())
    return result


class FileContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=65536)


@router.put("/files/{name}")
def write_file(name: str, body: FileContent, request: Request):
    user = identity(request, EMPLOYEES)
    target = file_for(user, name)
    encoded = body.content.encode("utf-8")
    if len(encoded) > 65536:
        raise HTTPException(413, "file exceeds 64 KiB")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    result = {"filename": name, "bytes": len(encoded), "written": True}
    result["audit"] = activity(request, user, "file_write", result.copy())
    return result


@router.get("/files/{name}")
def read_file(name: str, request: Request):
    user = identity(request, EMPLOYEES)
    target = file_for(user, name)
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            content = source.read(65537)
        if len(content) > 65536:
            raise HTTPException(413, "file exceeds read limit")
    except FileNotFoundError as exc:
        activity(request, user, "file_read_missing", {"filename": name, "errno": exc.errno})
        raise HTTPException(404, "file does not exist") from exc
    result = {"filename": name, "content": content}
    result["audit"] = activity(request, user, "file_read", {"filename": name})
    return result


@router.delete("/files/{name}")
def delete_file(name: str, request: Request):
    user = identity(request, EMPLOYEES)
    target = file_for(user, name)
    try:
        target.unlink()
    except FileNotFoundError as exc:
        activity(request, user, "file_delete_missing", {"filename": name, "errno": exc.errno})
        raise HTTPException(404, "file does not exist") from exc
    return {"filename": name, "deleted": True,
            "audit": activity(request, user, "file_delete", {"filename": name})}


@router.post("/admin/accounts")
def create_account(body: NewAccount, request: Request):
    identity(request, {"operator"})
    response = call(CORE_URL, "POST", "/admin/accounts", token=bearer(request), body=body.model_dump())
    return JSONResponse(response.json(), response.status_code)
