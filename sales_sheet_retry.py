"""Retry only Sheet writes, never the inventory transaction or LINE delivery."""
import time


def append_sales_with_retry(rows, get_sheet, append_batch, after_append,
                            sleep=time.sleep, log=print):
    """Use column I order IDs to avoid replaying an acknowledged or ambiguous write.

    The caller holds the sales write lock. If a write raises after reaching Sheets,
    preserve the warning (formatting may be incomplete), without appending again.
    No exception details, card data or credentials are emitted to logs.
    """
    if not rows:
        return True
    order_ids = [str(row[8]).strip() for row in rows]
    if not all(order_ids) or len(set(order_ids)) != len(order_ids):
        log('SALES_SHEET_INVALID_ORDER_IDS', flush=True)
        return False
    had_error = False
    uncertain_saved = False
    for attempt in range(3):
        try:
            ws = get_sheet()
            existing = {str(value).strip() for value in ws.col_values(9)[1:]}
            uncertain_saved = uncertain_saved or (had_error and bool(set(order_ids) & existing))
            missing = [row for row in rows if str(row[8]).strip() not in existing]
            if not missing:
                # A previous ambiguous write may have saved rows but not colors.
                return not had_error
            response = append_batch(ws, missing)
            after_append(ws, response)
            return not uncertain_saved
        except Exception as exc:
            had_error = True
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if not isinstance(status, int):
                status = None
            log('SALES_SHEET_WRITE_FAILURE', 'attempt', attempt + 1,
                'type', type(exc).__name__, 'status', status,
                'orders', ','.join(order_ids), flush=True)
            if attempt < 2:
                sleep((0.5, 1.5)[attempt])
    return False
