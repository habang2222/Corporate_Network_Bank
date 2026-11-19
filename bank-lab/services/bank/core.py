"""PostgreSQL transactions, ownership checks and durable idempotency records."""
import os
from pathlib import Path
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .common import audit, identity, secret

router = APIRouter()


def db():
    return psycopg.connect(host=os.getenv("DATABASE_HOST", "db"),
                           dbname=os.getenv("POSTGRES_DB", "bank"),
                           user=os.getenv("POSTGRES_USER", "bank"),
                           password=secret("db-password"), connect_timeout=3,
                           row_factory=dict_row,
                           options="-c statement_timeout=10000 -c lock_timeout=8000")


def initialize():
    directory = Path(os.getenv("DATABASE_SCHEMA_DIR", "/app/database"))
    for attempt in range(30):
        try:
            with db() as connection:
                connection.execute((directory / "schema.sql").read_text(encoding="utf-8"))
                connection.execute((directory / "seed.sql").read_text(encoding="utf-8"))
            audit("database_initialized", seed="synthetic", idempotent=True)
            return
        except psycopg.OperationalError:
            if attempt == 29:
                raise
            time.sleep(1)


def customer(request: Request):
    return identity(request, {"customer"}, require_internal=True)


def employee(request: Request, allowed: set[str]):
    user = identity(request, allowed, require_internal=True)
    with db() as connection:
        row = connection.execute("SELECT role FROM employee_roles WHERE username=%s",
                                 (user["username"],)).fetchone()
    if row is None or row["role"] != user["role"]:
        raise HTTPException(403, "employee role is not provisioned")
    return user


def record(connection, username, action, status, detail):
    connection.execute("INSERT INTO request_audit(username,action,status,detail) VALUES (%s,%s,%s,%s)",
                       (username, action, status, Jsonb(detail)))


@router.get("/accounts")
def accounts(request: Request):
    user = customer(request)
    with db() as connection:
        rows = connection.execute("SELECT * FROM accounts WHERE customer_id=%s ORDER BY account_id",
                                  (user["customer_id"],)).fetchall()
        record(connection, user["username"], "accounts_read", "completed", {"count": len(rows)})
    audit("accounts_read", username=user["username"], count=len(rows))
    return {"accounts": rows}


def owned_account(connection, account_id, user):
    row = connection.execute("SELECT * FROM accounts WHERE account_id=%s", (account_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "account does not exist")
    if row["customer_id"] != user["customer_id"]:
        audit("account_access_denied", username=user["username"], account_id=account_id)
        raise HTTPException(403, "account is owned by another customer")
    return row


@router.get("/accounts/{account_id}")
def account(account_id: str, request: Request):
    user = customer(request)
    with db() as connection:
        row = owned_account(connection, account_id, user)
        record(connection, user["username"], "account_read", "completed", {"account_id": account_id})
    audit("account_read", username=user["username"], account_id=account_id)
    return row


@router.get("/accounts/{account_id}/transactions")
def history(account_id: str, request: Request):
    user = customer(request)
    with db() as connection:
        owned_account(connection, account_id, user)
        # A recipient sees completed inbound transfers only, not another user's failed requests.
        rows = connection.execute("""SELECT request_id,from_account,to_account,amount_cents,status,
            created_at FROM transfers WHERE
            (from_account=%s AND username=%s) OR (to_account=%s AND status='completed')
            ORDER BY created_at DESC LIMIT 1000""", (account_id, user["username"], account_id)).fetchall()
        record(connection, user["username"], "transactions_read", "completed",
               {"account_id": account_id, "row_count": len(rows)})
    audit("transactions_read", username=user["username"], account_id=account_id,
          row_count=len(rows))
    return {"transactions": rows, "limit": 1000}


class Transfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    from_account: str = Field(min_length=1, max_length=80)
    to_account: str = Field(min_length=1, max_length=80)
    amount_cents: StrictInt = Field(ge=-9_000_000_000_000_000, le=9_000_000_000_000_000)


@router.post("/transfers")
def transfer(body: Transfer, request: Request):
    user = customer(request)
    payload = {**body.model_dump(), "username": user["username"]}
    result = {**body.model_dump(), "status": "completed", "replayed": False}
    status_code = 200
    with db() as connection:
        # A transaction-scoped advisory lock serializes identical request IDs before reading
        # the idempotency record. Account row locks use stable order to avoid transfer deadlocks.
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (body.request_id,))
        previous = connection.execute("SELECT payload,result,http_status FROM transfers WHERE request_id=%s",
                                      (body.request_id,)).fetchone()
        if previous:
            if previous["payload"] != payload:
                record(connection, user["username"], "transfer", "idempotency_conflict",
                       {"request_id": body.request_id})
                return JSONResponse({"detail": "request_id was already used with another payload"}, 409)
            repeated = {**previous["result"], "replayed": True}
            record(connection, user["username"], "transfer", "replayed", {"request_id": body.request_id})
            return JSONResponse(repeated, previous["http_status"])
        rows = connection.execute("SELECT * FROM accounts WHERE account_id IN (%s,%s) ORDER BY account_id FOR UPDATE",
                                  (body.from_account, body.to_account)).fetchall()
        by_id = {row["account_id"]: row for row in rows}
        source = by_id.get(body.from_account)
        target = by_id.get(body.to_account)
        reason = None
        if source is None or target is None:
            status_code, reason = 404, "account does not exist"
        elif source["customer_id"] != user["customer_id"]:
            status_code, reason = 403, "source account is owned by another customer"
        elif body.from_account == body.to_account:
            status_code, reason = 422, "source and destination must differ"
        elif body.amount_cents <= 0:
            status_code, reason = 422, "amount_cents must be positive"
        elif source["balance_cents"] < body.amount_cents:
            status_code, reason = 409, "insufficient balance"
        elif target["balance_cents"] + body.amount_cents > 9_000_000_000_000_000_000:
            status_code, reason = 409, "destination balance limit"
        if reason is not None:
            result.update(status="failed", error=reason)
        else:
            connection.execute("UPDATE accounts SET balance_cents=balance_cents-%s WHERE account_id=%s",
                               (body.amount_cents, body.from_account))
            connection.execute("UPDATE accounts SET balance_cents=balance_cents+%s WHERE account_id=%s",
                               (body.amount_cents, body.to_account))
            result["from_balance_cents"] = source["balance_cents"] - body.amount_cents
        connection.execute("""INSERT INTO transfers(request_id,username,from_account,to_account,amount_cents,
            payload,status,http_status,result) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                           (body.request_id, user["username"], body.from_account, body.to_account,
                            body.amount_cents, Jsonb(payload), result["status"], status_code, Jsonb(result)))
        record(connection, user["username"], "transfer", result["status"],
               {"request_id": body.request_id, "reason": reason})
    audit("transfer", username=user["username"], request_id=body.request_id,
          status=result["status"], http_status=status_code)
    return JSONResponse(result, status_code)


@router.get("/customers")
def customers(request: Request):
    user = employee(request, {"teller"})
    with db() as connection:
        rows = connection.execute("SELECT customer_id,display_name FROM customers ORDER BY customer_id").fetchall()
        record(connection, user["username"], "customers_read", "completed", {})
    return {"customers": rows, "synthetic_data": True}


@router.get("/report-data")
def report_data(request: Request):
    user = employee(request, {"teller", "auditor"})
    with db() as connection:
        accounts = connection.execute("SELECT count(*) AS account_count,sum(balance_cents) AS total_balance_cents FROM accounts").fetchone()
        transfers = connection.execute("SELECT status,count(*) AS count FROM transfers GROUP BY status ORDER BY status").fetchall()
        record(connection, user["username"], "report_data_read", "completed",
               {"account_count": accounts["account_count"]})
    audit("report_data_read", username=user["username"], account_count=accounts["account_count"])
    return {**accounts, "transfers": transfers, "synthetic_data": True}


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1, max_length=80)
    detail: dict = Field(default_factory=dict)


@router.post("/employee-actions")
def employee_action(body: Action, request: Request):
    user = employee(request, {"teller", "auditor", "operator"})
    import json
    if len(json.dumps(body.detail)) > 8192:
        raise HTTPException(413, "audit detail is too large")
    with db() as connection:
        row = connection.execute("""INSERT INTO employee_actions(username,role,action,detail)
            VALUES (%s,%s,%s,%s) RETURNING action_id""",
                                 (user["username"], user["role"], body.action, Jsonb(body.detail))).fetchone()
    return row


class NewAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    customer_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


@router.post("/admin/accounts")
def create_account(body: NewAccount, request: Request):
    user = employee(request, {"operator"})
    with db() as connection:
        if connection.execute("SELECT 1 FROM customers WHERE customer_id=%s", (body.customer_id,)).fetchone() is None:
            raise HTTPException(404, "synthetic customer does not exist")
        row = connection.execute("""INSERT INTO accounts(account_id,customer_id,balance_cents)
            VALUES (%s,%s,0) ON CONFLICT DO NOTHING RETURNING *""",
                                 (body.account_id, body.customer_id)).fetchone()
        if row is None:
            raise HTTPException(409, "account already exists")
        record(connection, user["username"], "create_account", "completed", body.model_dump())
    return row
