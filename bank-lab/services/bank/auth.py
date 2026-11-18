"""Persistent, revocable opaque sessions. Credentials are generated lab secrets."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .common import DATA, SECRETS, audit, bearer, internal

router = APIRouter()


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


@contextmanager
def db():
    connection = sqlite3.connect(DATA / "auth.db", timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
            expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""")


def users():
    return json.loads((SECRETS / "users.json").read_text(encoding="utf-8"))


def current(token: str) -> dict:
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db() as connection:
        row = connection.execute("SELECT username, expires, revoked FROM sessions WHERE token_hash=?",
                                 (digest,)).fetchone()
    if row is None or row["revoked"] or row["expires"] <= time.time():
        raise HTTPException(401, "invalid, revoked or expired session")
    account = users().get(row["username"])
    if account is None:
        raise HTTPException(401, "account is no longer available")
    return {"username": row["username"], "role": account["role"],
            "customer_id": account.get("customer_id")}


@router.post("/login")
def login(body: Login):
    account = users().get(body.username)
    # A constant-time comparison avoids prefix timing differences. These are lab accounts.
    valid = hmac.compare_digest(body.password.encode(),
                                (account["password"] if account else secrets.token_hex(32)).encode())
    if account is None or not valid:
        audit("authentication_failed", username=body.username)
        raise HTTPException(401, "invalid credentials")
    token = secrets.token_urlsafe(32)
    with db() as connection:
        connection.execute("INSERT INTO sessions(token_hash,username,expires) VALUES (?,?,?)",
                           (hashlib.sha256(token.encode()).hexdigest(), body.username,
                            time.time() + 3600))
    user = {"username": body.username, "role": account["role"],
            "customer_id": account.get("customer_id")}
    audit("authentication_succeeded", username=body.username, role=account["role"])
    return {"token": token, "token_type": "bearer", "expires_in": 3600, "user": user}


@router.post("/validate")
def validate(request: Request):
    internal(request)
    return current(bearer(request))


@router.post("/logout")
def logout(request: Request):
    token = bearer(request)
    user = current(token)
    with db() as connection:
        connection.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?",
                           (hashlib.sha256(token.encode()).hexdigest(),))
    audit("logout", username=user["username"])
    return {"logged_out": True}
