# Original inventory sale recovery

Legacy quick sales and `/發` now reply with committed cards before calling Google
Sheets. Sheet failures and ambiguous LINE errors never allocate new cards, refund
an order, or retry LINE automatically. Mirrors run inside the original request.

Administrators may use these commands in the original sale chat:

- `/補發序號 TX...` returns only the original sold cards, then retries mirroring.
- `/同步出庫 TX...` retries only the Sheet mirror and never sends cards.

Both batch IDs and child order IDs are accepted. Recovery checks the current chat,
legacy sale action, customer, product, sold state and exact original quantity.
Customer credit orders keep their separate `/自助補發` path and permissions.
Mirroring preserves the original sale date and uses existing order-ID deduplication.
No stock, ledger, pricing or discount settings change during recovery.

If a reply is missing, use recovery rather than repeating the original sale.
An API timeout does not prove that LINE failed to deliver; recovery is explicit.
There is no automatic background retry of failed Sheet writes in this change.

Validation: credential-free delivery ordering, independent Sheet exceptions,
ambiguous LINE timeouts, admin denial, same-chat queries, recalled/missing orders,
original serial recovery, mirror-only requests, both router paths, image bundling,
and the existing offline and cloud-control suites. CI runs isolated PostgreSQL.
