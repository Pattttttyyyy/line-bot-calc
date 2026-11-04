-- Explicitly approved access reduction; no stock or order data changes.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
REVOKE SELECT ON public.inventory_summary FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
        REVOKE SELECT ON public.inventory_summary FROM anon;
    END IF;
END $$;
COMMIT;
