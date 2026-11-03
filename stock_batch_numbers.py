"""Per-product daily stock-in batch numbers; first batch has no suffix."""
from datetime import datetime
from zoneinfo import ZoneInfo


def allocate_stock_batch_no(conn, product_code, batch_date=None):
    batch_date = batch_date or datetime.now(ZoneInfo('Asia/Taipei')).date()
    counter_key = 'stock-in:' + product_code + ':' + batch_date.strftime('%Y%m%d')
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO order_number_counters(product_code,last_value)
            VALUES (%s,1) ON CONFLICT(product_code) DO UPDATE
            SET last_value=order_number_counters.last_value+1
            RETURNING last_value''', (counter_key,))
        row = cur.fetchone()
        sequence = row['last_value'] if isinstance(row,dict) else row[0]
    suffix = '' if sequence == 1 else '-' + str(sequence)
    return f'{product_code}-{batch_date:%Y%m%d}{suffix}'
