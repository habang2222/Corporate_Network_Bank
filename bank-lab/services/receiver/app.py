"""Mock receiver. A receipt and a committed event are deliberately separate states."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

DB = Path(os.getenv("RECEIVER_DB", "/data/receiver.db"))
KEY_FILE = Path(os.getenv("SERVICE_KEY_FILE", "/secrets/service-key"))
control_lock = threading.Lock()
controls = {"delay_seconds": 0.0, "disconnect": False, "write_failure": False}


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA synchronous=FULL")
    try:
        with con:
            yield con
    finally:
        con.close()


@asynccontextmanager
async def lifespan(app):
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS receipts (
          receipt_id TEXT PRIMARY KEY, server_id TEXT NOT NULL, event_id TEXT NOT NULL,
          received_at TEXT NOT NULL, state TEXT NOT NULL, raw_body BLOB NOT NULL,
          error TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
          server_id TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
          payload_hash TEXT NOT NULL, raw_body BLOB NOT NULL,
          received_at TEXT NOT NULL, committed_at TEXT NOT NULL,
          PRIMARY KEY(server_id,event_id)
        );
        """)
    yield


app = FastAPI(title="llooggeerr mock log receiver", lifespan=lifespan)


def authorize(x_service_key: str = Header(default="")):
    if not KEY_FILE.is_file() or not hmac.compare_digest(x_service_key.encode(), KEY_FILE.read_bytes().strip()):
        raise HTTPException(401, "invalid service key")


class Message(BaseModel):
    server_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    payload: dict


class Controls(BaseModel):
    delay_seconds: float = Field(default=0, ge=0, le=5)
    disconnect: bool = False
    write_failure: bool = False


@app.get("/health")
def health():
    with connect() as con:
        con.execute("SELECT 1").fetchone()
    return {"status": "ok", "kind": "mock_receiver", "collector_integrated": False}


@app.post("/control", dependencies=[Depends(authorize)])
def control(body: Controls):
    with control_lock:
        controls.update(body.model_dump())
        return {"controls": dict(controls), "source": "mock_receiver_fault_control"}


def receive(message, raw, snapshot):
    if snapshot["delay_seconds"]:
        time.sleep(snapshot["delay_seconds"])
    if snapshot["disconnect"]:
        return JSONResponse({"received": False, "stored": False,
                             "error": "simulated_unavailable", "source": "mock_receiver"}, 503)
    received_at, receipt_id = now(), str(uuid.uuid4())
    payload = json.dumps(message.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    try:
        # Commit the independent receipt before attempting event storage.
        with connect() as con:
            con.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?)", (
                receipt_id, message.server_id, message.event_id, received_at, "received", raw, None))
        if snapshot["write_failure"]:
            with connect() as con:
                con.execute("UPDATE receipts SET state=?,error=? WHERE receipt_id=?",
                            ("storage_failed", "simulated_write_failure", receipt_id))
            return JSONResponse({"received": True, "stored": False, "receipt_id": receipt_id,
                                 "received_at": received_at, "committed_at": None,
                                 "error": "simulated_write_failure"}, 507)
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM events WHERE server_id=? AND event_id=?",
                              (message.server_id, message.event_id)).fetchone()
            if old and old["payload_hash"] != digest:
                con.execute("UPDATE receipts SET state=?,error=? WHERE receipt_id=?",
                            ("conflict", "event_id_payload_conflict", receipt_id))
                response = {"received": True, "stored": False, "receipt_id": receipt_id,
                            "error": "event_id_payload_conflict"}
                status = 409
            else:
                committed_at = old["committed_at"] if old else now()
                if not old:
                    con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (
                        message.server_id, message.event_id, payload, digest, raw, received_at, committed_at))
                con.execute("UPDATE receipts SET state=? WHERE receipt_id=?",
                            ("duplicate" if old else "committed", receipt_id))
                response = {"received": True, "stored": True, "duplicate": old is not None,
                            "server_id": message.server_id, "event_id": message.event_id,
                            "receipt_id": receipt_id, "received_at": received_at,
                            "committed_at": committed_at,
                            "storage_basis": "sqlite_commit_wal_synchronous_full"}
                status = 200 if old else 201
        # Context exit above is the commit boundary; never acknowledge before it.
        response["storage_ack_at"] = now()
        return JSONResponse(response, status)
    except sqlite3.Error as exc:
        return JSONResponse({"received": True, "stored": False, "receipt_id": receipt_id,
                             "received_at": received_at, "committed_at": None,
                             "error": "storage_error", "reason": str(exc),
                             "receipt_persisted": "unknown"}, 507)


@app.post("/events", dependencies=[Depends(authorize)])
async def events(request: Request):
    # Stream with a hard bound: a caller cannot force unbounded body buffering.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise HTTPException(413, "mock message limit is 65536 bytes")
    try:
        message = Message.model_validate_json(bytes(raw))
    except ValidationError as exc:
        raise HTTPException(422, "invalid message envelope") from exc
    with control_lock:
        snapshot = dict(controls)
    return await asyncio.to_thread(receive, message, bytes(raw), snapshot)


@app.get("/events/{server_id}/{event_id}", dependencies=[Depends(authorize)])
def event(server_id: str, event_id: str):
    with connect() as con:
        row = con.execute("SELECT * FROM events WHERE server_id=? AND event_id=?",
                          (server_id, event_id)).fetchone()
    if not row:
        raise HTTPException(404, "event not committed")
    result = dict(row)
    result.pop("raw_body")
    result["payload"] = json.loads(result["payload"])
    result["stored"] = True
    return result


@app.get("/status", dependencies=[Depends(authorize)])
def status():
    with connect() as con:
        count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        states = dict(con.execute("SELECT state,COUNT(*) FROM receipts GROUP BY state").fetchall())
    with control_lock:
        snapshot = dict(controls)
    return {"committed_unique_events": count, "receipt_states": states, "controls": snapshot,
            "source": "mock_receiver", "collector_integrated": False}
