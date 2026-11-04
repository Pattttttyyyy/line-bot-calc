-- Apply only with the compatible prepaid-aware runtime release.
-- Existing balances, memberships, limits, orders and cards remain unchanged.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
ALTER TABLE public.group_credit_accounts DROP CONSTRAINT group_credit_accounts_debt_check;
ALTER TABLE public.group_credit_accounts ADD CONSTRAINT group_credit_accounts_debt_check
    CHECK (debt BETWEEN -9999999999999999.99 AND 9999999999999999.99);
COMMIT;
