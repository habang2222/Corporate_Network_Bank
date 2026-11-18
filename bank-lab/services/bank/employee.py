"""Role-specific employee workflows with real bounded OS operations."""
import errno
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Literal
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
        raise HTTPException(422, "filename must be a plain laboratory basename")
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
        # The file/OS action may already have completed; do not disguise that as a rollback.
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


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["file-cycle", "permission-denied", "process", "network"]


@router.post("/operations")
def operation(body: Operation, request: Request):
    user = identity(request, {"operator"})
    operation_id = uuid4().hex
    steps = []
    target = root_for(user) / ("operation-" + operation_id + ".txt")
    if body.operation == "file-cycle":
        renamed = target.with_suffix(".renamed")
        try:
            target.write_text("synthetic first version", encoding="utf-8")
            steps.append({"operation": "create_write", "path": str(target), "return_code": 0})
            content = target.read_text(encoding="utf-8")
            steps.append({"operation": "read", "path": str(target), "bytes": len(content), "return_code": 0})
            with target.open("a", encoding="utf-8") as output:
                output.write("\nmodified")
            steps.append({"operation": "write", "path": str(target), "return_code": 0})
            target.rename(renamed)
            steps.append({"operation": "rename", "path": str(target), "destination": str(renamed), "return_code": 0})
            renamed.unlink()
            steps.append({"operation": "unlink", "path": str(renamed), "return_code": 0})
        finally:
            target.unlink(missing_ok=True)
            renamed.unlink(missing_ok=True)
    elif body.operation == "permission-denied":
        target.write_text("synthetic restricted file", encoding="utf-8")
        try:
            target.chmod(0o000)
            steps.append({"operation": "chmod", "path": str(target), "mode": "000", "return_code": 0})
            try:
                target.read_bytes()
                steps.append({"operation": "open_denied", "return_code": 0, "expected": False})
            except PermissionError as exc:
                steps.append({"operation": "open_denied", "return_code": -1, "errno": exc.errno,
                              "expected": exc.errno == errno.EACCES})
            try:
                os.chown(target, 0, 0)
                steps.append({"operation": "chown_denied", "return_code": 0, "expected": False})
            except PermissionError as exc:
                steps.append({"operation": "chown_denied", "return_code": -1, "errno": exc.errno,
                              "expected": exc.errno == errno.EPERM})
        finally:
            target.chmod(0o600)
            target.unlink()
    elif body.operation == "process":
        for expected in (0, 7):
            child = subprocess.run([sys.executable, "-c", "import sys; sys.exit(" + str(expected) + ")"],
                                   timeout=3, capture_output=True, check=False)
            steps.append({"operation": "process_exit", "return_code": child.returncode,
                          "expected_return_code": expected})
    elif body.operation == "network":
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.settimeout(2)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            steps.extend([{"operation": "bind", "port": port, "return_code": 0},
                          {"operation": "listen", "port": port, "return_code": 0}])
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                steps.append({"operation": "connect", "port": port, "return_code": 0})
                accepted, _ = server.accept()
                with accepted:
                    client.sendall(b"lab")
                    accepted.recv(3)
                steps.append({"operation": "accept", "port": port, "return_code": 0})
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                steps.append({"operation": "connect_failure", "return_code": 0, "expected": False})
        except OSError as exc:
            steps.append({"operation": "connect_failure", "return_code": -1, "errno": exc.errno,
                          "expected": exc.errno == errno.ECONNREFUSED})
    result = {"operation_id": operation_id, "operation": body.operation, "steps": steps,
              "source": "workload_os_results", "pid": os.getpid(),
              "all_expected": all(step.get("expected", True) and
                  step.get("return_code") == step.get("expected_return_code", step.get("return_code"))
                  for step in steps)}
    result["audit"] = activity(request, user, "operation_" + body.operation, result.copy())
    return result


@router.post("/admin/accounts")
def create_account(body: NewAccount, request: Request):
    identity(request, {"operator"})
    response = call(CORE_URL, "POST", "/admin/accounts", token=bearer(request), body=body.model_dump())
    return JSONResponse(response.json(), response.status_code)
