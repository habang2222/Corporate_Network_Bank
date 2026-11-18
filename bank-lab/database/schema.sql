-- Synthetic laboratory data only. No connection to a real financial institution.
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY, display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employee_roles (
    username TEXT PRIMARY KEY, role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    balance_cents BIGINT NOT NULL CHECK (balance_cents >= 0),
    currency TEXT NOT NULL DEFAULT 'LAB'
);
CREATE TABLE IF NOT EXISTS transfers (
    request_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    from_account TEXT NOT NULL,
    to_account TEXT NOT NULL,
    amount_cents BIGINT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS transfers_source ON transfers(from_account, created_at);
CREATE INDEX IF NOT EXISTS transfers_target ON transfers(to_account, created_at);
CREATE TABLE IF NOT EXISTS employee_actions (
    action_id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    detail JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS request_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    username TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    detail JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
