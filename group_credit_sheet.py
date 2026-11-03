"""Clear only cards still available, under row locks, to avoid erasing a resale."""
def clear_recovered_product_sales(items,ns):
    ok=True
    for item in items:
        conn=ns['get_db']()
        try:
            with ns['PRODUCT_SHEET_WRITE_LOCK']:
                with conn.cursor(cursor_factory=ns['RealDictCursor']) as cur:
                    cur.execute('SELECT id,serial,status FROM serials WHERE id=ANY(%s) ORDER BY id FOR UPDATE',
                                (item['serial_ids'],))
                    # A new order may already have bought a returned card. Never clear its sale.
                    available=[r['serial'] for r in cur.fetchall() if r['status']=='available']
                    if available:
                        mirror=dict(item,serials=available)
                        ok=bool(ns['clear_product_sale']([mirror])) and ok
                conn.rollback()  # Read-only row locks, no inventory changes.
        except Exception:
            conn.rollback()
            ok=False
        finally:conn.close()
    return ok
