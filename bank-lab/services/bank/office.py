"""Employee mail and shared documents for ordinary company activity."""
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .common import DATA, SECRETS, audit, identity

router = APIRouter()
STAFF = {"teller", "auditor", "operator"}
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


@contextmanager
def connect():
    db = sqlite3.connect(DATA / "office.db", timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        with db:
            yield db
    finally:
        db.close()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "shared").mkdir(exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            subject TEXT NOT NULL, body TEXT NOT NULL, created_ns INTEGER NOT NULL)""")


def shared_path(name: str) -> Path:
    if not NAME.fullmatch(name) or name in {".", ".."}:
        raise HTTPException(422, "invalid shared filename")
    path = DATA / "shared" / name
    if path.is_symlink():
        raise HTTPException(422, "symlink is not a shared document")
    return path


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(min_length=1, max_length=80)
    subject: str = Field(min_length=1, max_length=160)
    body: str = Field(max_length=4096)


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=65536)


@router.post("/messages")
def send_message(body: Message, request: Request):
    sender = identity(request, STAFF)["username"]
    users = json.loads((SECRETS / "users.json").read_text(encoding="utf-8"))
    if body.recipient not in users or users[body.recipient]["role"] not in STAFF:
        raise HTTPException(404, "staff recipient not found")
    message_id = uuid4().hex
    created_ns = time.time_ns()
    with connect() as db:
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                   (message_id, sender, body.recipient, body.subject, body.body, created_ns))
    audit("message_sent", message_id=message_id, sender=sender,
          recipient=body.recipient, body_sha256=hashlib.sha256(body.body.encode()).hexdigest())
    return {"id": message_id, "created_ns": created_ns}


@router.get("/messages")
def messages(request: Request):
    username = identity(request, STAFF)["username"]
    with connect() as db:
        rows = db.execute("""SELECT id,sender,recipient,subject,body,created_ns
                             FROM messages WHERE sender=? OR recipient=?
                             ORDER BY created_ns DESC LIMIT 100""",
                          (username, username)).fetchall()
    audit("messages_viewed", username=username, count=len(rows))
    return {"messages": [dict(row) for row in rows]}


@router.put("/shared/{name}")
def write_shared(name: str, body: Document, request: Request):
    username = identity(request, STAFF)["username"]
    path = shared_path(name)
    encoded = body.content.encode()
    if len(encoded) > 65536:
        raise HTTPException(413, "shared document too large")
    previous = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("xb") as output:
        output.write(encoded)
        output.flush()
        import os
        os.fsync(output.fileno())
    temporary.replace(path)
    digest = hashlib.sha256(encoded).hexdigest()
    audit("shared_document_written", username=username, filename=name, bytes=len(encoded),
          sha256=digest, previous_sha256=previous)
    return {"filename": name, "bytes": len(encoded), "sha256": digest}


@router.get("/shared/{name}")
def read_shared(name: str, request: Request):
    username = identity(request, STAFF)["username"]
    path = shared_path(name)
    try:
        if path.stat().st_size > 65536:
            raise HTTPException(413, "shared document too large")
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(404, "shared document not found") from exc
    digest = hashlib.sha256(content.encode()).hexdigest()
    audit("shared_document_read", username=username, filename=name, sha256=digest)
    return {"filename": name, "content": content, "sha256": digest}


@router.delete("/shared/{name}")
def delete_shared(name: str, request: Request):
    username = identity(request, STAFF)["username"]
    path = shared_path(name)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.unlink()
    except FileNotFoundError as exc:
        raise HTTPException(404, "shared document not found") from exc
    audit("shared_document_deleted", username=username, filename=name, sha256=digest)
    return {"deleted": True, "filename": name, "sha256": digest}
