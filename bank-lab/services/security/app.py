"""Independent central audit record for the reusable company network."""
import hashlib
import hmac
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time

from fastapi import FastAPI, Header, HTTPException, Query, Request

DATA = Path(os.getenv("DATA_DIR", "/data"))
SECRETS = Path(os.getenv("SECRETS_DIR", "/secrets"))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def authorize(key: str | None) -> None:
    expected = (SECRETS / "service-key").read_text(encoding="utf-8").strip()
    if not key or not hmac.compare_digest(key, expected):
        raise HTTPException(403, "internal service credential required")


@contextmanager
def connect():
    connection = sqlite3.connect(DATA / "security.db", timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        with connection:
            yield connection
    finally:
        connection.close()


@app.on_event("startup")
def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL, service TEXT NOT NULL, event TEXT NOT NULL,
            occurred_ns INTEGER, received_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL, record TEXT NOT NULL)""")


@app.get("/health")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok", "service": "security"}


@app.post("/events")
async def ingest(request: Request, x_service_key: str | None = Header(default=None)):
    authorize(x_service_key)
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(413, "audit event too large")
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "invalid audit event") from exc
    if not isinstance(record, dict) or not all(
        isinstance(record.get(name), str) and record[name]
        for name in ("event_id", "source", "service", "event")
    ):
        raise HTTPException(422, "missing audit identity")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    with connect() as db:
        try:
            cursor = db.execute(
                "INSERT INTO events(event_id,source,service,event,occurred_ns,received_ns,sha256,record) VALUES (?,?,?,?,?,?,?,?)",
                (record["event_id"], record["source"], record["service"], record["event"],
                 record.get("time_ns"), time.time_ns(), digest, canonical))
        except sqlite3.IntegrityError:
            row = db.execute("SELECT id,sha256 FROM events WHERE event_id=?", (record["event_id"],)).fetchone()
            if row["sha256"] != digest:
                raise HTTPException(409, "event id conflicts with saved record")
            return {"id": row["id"], "stored": True, "duplicate": True}
    return {"id": cursor.lastrowid, "stored": True, "duplicate": False}


@app.get("/events")
def list_events(after_id: int = Query(default=0, ge=0),
                limit: int = Query(default=100, ge=1, le=500),
                x_service_key: str | None = Header(default=None)):
    authorize(x_service_key)
    with connect() as db:
        rows = db.execute("SELECT id,received_ns,sha256,record FROM events WHERE id>? ORDER BY id LIMIT ?",
                          (after_id, limit)).fetchall()
    return {"events": [{"id": row["id"], "received_ns": row["received_ns"],
                        "sha256": row["sha256"], "record": json.loads(row["record"])}
                       for row in rows]}
