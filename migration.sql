-- Review only. Do not run against production without deployment approval.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
CREATE TABLE IF NOT EXISTS group_credit_accounts (
    group_id TEXT PRIMARY KEY,
    customer TEXT NOT NULL CHECK (customer <> ''),
    debt NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (debt >= 0),
    credit_limit NUMERIC(18,2) CHECK (credit_limit > 0),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS group_credit_members (
    group_id TEXT NOT NULL REFERENCES group_credit_accounts(group_id),
    line_user_id TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (group_id, line_user_id)
);
CREATE TABLE IF NOT EXISTS group_credit_events (
    group_id TEXT NOT NULL REFERENCES group_credit_accounts(group_id),
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount NUMERIC(18,2) NOT NULL DEFAULT 0,
    debt_after NUMERIC(18,2) NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    order_no TEXT,
    child_orders JSONB NOT NULL DEFAULT '[]'::jsonb,
    delivery_status TEXT NOT NULL DEFAULT 'not_applicable'
      CHECK (delivery_status IN ('not_applicable','pending','delivered','uncertain')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, event_id),
    UNIQUE (order_no)
);
-- Supabase public API must not expose group balances or member IDs.
ALTER TABLE group_credit_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_credit_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_credit_events ENABLE ROW LEVEL SECURITY;
COMMIT;
