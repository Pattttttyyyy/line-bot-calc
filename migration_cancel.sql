-- Additive only: original orders and card credentials are retained.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
CREATE TABLE IF NOT EXISTS group_credit_cancel_requests (
    token TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES group_credit_accounts(group_id),
    message_id TEXT NOT NULL,
    order_no TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','confirmed','declined')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '5 minutes'),
    UNIQUE(group_id,message_id)
);
CREATE TABLE IF NOT EXISTS group_credit_cancelled_orders (
    order_no TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES group_credit_accounts(group_id),
    parent_order_no TEXT NOT NULL,
    request_token TEXT NOT NULL REFERENCES group_credit_cancel_requests(token),
    confirmed_by TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    original_transaction JSONB NOT NULL,
    serial_ids BIGINT[] NOT NULL
);
CREATE INDEX IF NOT EXISTS group_credit_cancelled_parent_idx
    ON group_credit_cancelled_orders(group_id,parent_order_no);
ALTER TABLE group_credit_cancel_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_credit_cancelled_orders ENABLE ROW LEVEL SECURITY;
CREATE OR REPLACE VIEW active_transactions WITH (security_invoker = true) AS
    SELECT t.* FROM transactions t
    WHERE NOT EXISTS (SELECT 1 FROM group_credit_cancelled_orders c WHERE c.order_no=t.order_no);
REVOKE ALL ON active_transactions FROM PUBLIC;
-- Supabase default grants must not publish this internal reporting view.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
        REVOKE ALL ON active_transactions FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
        REVOKE ALL ON active_transactions FROM authenticated;
    END IF;
END $$;
COMMIT;
