BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='15s';
CREATE TABLE IF NOT EXISTS bot_ui_admin_groups (
 group_id TEXT PRIMARY KEY, configured_by TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bot_ui_sessions (
 token TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
 start_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('sale','stock')),
 state TEXT NOT NULL CHECK(state IN ('product','quantity','data','confirm','done','cancelled')),
 product_code TEXT, quantity INTEGER, revision INTEGER NOT NULL DEFAULT 0, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
 receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL DEFAULT (now()+interval '10 minutes'),
 UNIQUE(group_id,user_id,start_id)
);
ALTER TABLE bot_ui_sessions ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS bot_ui_one_pending ON bot_ui_sessions(group_id,user_id)
 WHERE state IN ('product','quantity','data','confirm');
ALTER TABLE bot_ui_admin_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_ui_sessions ENABLE ROW LEVEL SECURITY;
COMMIT;
