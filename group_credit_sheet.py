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


TEST_SALES_SHEET_NAME='測試銷售紀錄'
TEST_SALES_HEADERS=['日期','客戶','數量','單價','售價金額','成本','利潤','商品','訂單編號','同步狀態','最後同步時間']


def get_test_sales_sheet(book, not_found, lock):
    """Dedicated mirror; never included in the formal two-way sync/report set."""
    with lock:
        try:
            sheet=book.worksheet(TEST_SALES_SHEET_NAME)
        except not_found:
            sheet=book.add_worksheet(title=TEST_SALES_SHEET_NAME,rows=1000,cols=11)
        headers=sheet.row_values(1)
        if not headers:
            sheet.append_row(TEST_SALES_HEADERS,value_input_option='RAW')
        elif headers!=TEST_SALES_HEADERS:
            raise ValueError('Test sales headers do not match; existing data retained')
        return sheet
