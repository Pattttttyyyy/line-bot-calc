"""Daily LINE report. Read-only inventory queries; no stock or Sheet writes."""
import argparse
import os
import sys
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP

TAIPEI = ZoneInfo('Asia/Taipei')
REPORT_PRODUCTS = ['MY1萬', 'MY5000', 'MY3000', 'MY1000', '大卡', '小卡',
                   '貝1000', '貝1490', '貝2990', '貝500']
STOCK_UNIT_VALUES = {
    **{code: Decimal(face) * Decimal('0.898') for code, face in
       [('MY1萬', '10000'), ('MY5000', '5000'), ('MY3000', '3000'), ('MY1000', '1000')]},
    **{code: Decimal(face) * Decimal('0.9033') for code, face in
       [('大卡', '10000'), ('小卡', '3000')]},
    **{code: Decimal(face) * Decimal('0.88') for code, face in
       [('貝1000', '1000'), ('貝1490', '1490'), ('貝2990', '2990'), ('貝500', '500')]},
}


def reporting_window(now):
    end = now.astimezone(TAIPEI)
    start = datetime.combine(end.date(), time.min, TAIPEI)
    return start, end


def read_report(context_id, now):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    start, end = reporting_window(now)
    conn = psycopg2.connect(os.environ['DATABASE_URL'], connect_timeout=20)
    try:
        conn.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SET LOCAL statement_timeout = 30000')
            cur.execute('''
                WITH manual AS (
                    SELECT product_code, SUM(quantity) AS outgoing,
                        COALESCE(SUM(sale_amount),0) AS sales_amount,
                        COUNT(*) FILTER(WHERE sale_amount IS NULL) AS unpriced_orders
                    FROM manual_sales
                    WHERE state='active' AND group_id=%s AND sale_date=%s
                    GROUP BY product_code
                ), sales AS (
                    SELECT product_id, SUM(quantity) AS outgoing,
                        COALESCE(SUM(sale_amount),0) AS sales_amount,
                        COUNT(*) FILTER(WHERE sale_amount IS NULL) AS unpriced_orders
                    FROM active_transactions
                    WHERE group_id=%s AND created_at >= %s AND created_at < %s
                    GROUP BY product_id
                ), codes AS (
                    SELECT code FROM products WHERE active=TRUE AND code=ANY(%s)
                    UNION SELECT product_code FROM manual
                )
                SELECT c.code, (p.id IS NOT NULL) AS stock_managed,
                    (SELECT COUNT(*) FROM serials s WHERE s.product_id=p.id
                      AND s.created_at >= %s AND s.created_at < %s) AS incoming,
                    COALESCE(t.outgoing,0) + COALESCE(m.outgoing,0) AS outgoing,
                    COALESCE(t.sales_amount,0) + COALESCE(m.sales_amount,0) AS sales_amount,
                    COALESCE(t.unpriced_orders,0) + COALESCE(m.unpriced_orders,0) AS unpriced_orders,
                    (SELECT COUNT(*) FROM serials s WHERE s.product_id=p.id
                      AND s.status='available') AS available
                FROM codes c LEFT JOIN products p ON p.code=c.code AND p.active=TRUE
                    AND p.code=ANY(%s)
                LEFT JOIN manual m ON m.product_code=c.code
                LEFT JOIN sales t ON t.product_id=p.id
                ORDER BY LOWER(c.code)
            ''', (context_id, start.date(), context_id, start, end,
                  REPORT_PRODUCTS, start, end, REPORT_PRODUCTS))
            return start, cur.fetchall()
    finally:
        conn.rollback()
        conn.close()


def format_report(start, rows, now):
    lines = [f'📊 每日報表｜{start:%Y/%m/%d}',
             '進貨：全庫存；銷貨：本群組',
             f'庫存時間：{now.astimezone(TAIPEI):%Y/%m/%d %H:%M}',
             '', '商品｜進貨／銷貨／可售庫存']
    for row in rows:
        available = row['available'] if row.get('stock_managed', True) else '—（手填、不扣庫存）'
        lines.append(f"{row['code']}｜{row['incoming']}／{row['outgoing']}／{available}")
    lines.extend(['', f"合計｜{sum(r['incoming'] for r in rows)}／"
                  f"{sum(r['outgoing'] for r in rows)}／{sum(r['available'] for r in rows)}",
                  f'進銷貨範圍：當天 00:00～{now.astimezone(TAIPEI):%H:%M}（台灣時間）'])
    sales_amount = sum((Decimal(str(r.get('sales_amount') or 0)) for r in rows), Decimal(0))
    unpriced = sum(r.get('unpriced_orders', 0) for r in rows)
    stock_amount = sum((STOCK_UNIT_VALUES[r['code']] * r['available'] for r in rows
                        if r.get('stock_managed', True) and r['code'] in STOCK_UNIT_VALUES), Decimal(0))
    unknown_stock = sum(r['available'] for r in rows if r.get('stock_managed', True)
                        and r['code'] not in STOCK_UNIT_VALUES)
    money = lambda value: format(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), ',.2f')
    lines.extend(['', f'銷貨金額（實際售價）：NT$ {money(sales_amount)}'
                  + (f'（已填合計；另 {unpriced} 筆未填售價）' if unpriced else ''),
                  f'庫存金額（折數估值）：NT$ {money(stock_amount)}'
                  + (f'（另 {unknown_stock} 張未設定估值）' if unknown_stock else '')])
    return '\n'.join(lines)


def push_report(group_id, day, text):
    import json
    import urllib.request
    from urllib.error import HTTPError
    retry_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f'line-daily-report:{group_id}:{day}'))
    chunks = [text[i:i+4500] for i in range(0, len(text), 4500)]
    if len(chunks) > 5:
        raise ValueError('Report exceeds LINE message limit')
    request = urllib.request.Request('https://api.line.me/v2/bot/message/push',
        data=json.dumps({'to': group_id, 'messages': [
            {'type': 'text', 'text': chunk} for chunk in chunks]}).encode(),
        headers={'Authorization': 'Bearer '+os.environ['LINE_CHANNEL_ACCESS_TOKEN'],
                 'Content-Type': 'application/json', 'X-Line-Retry-Key': retry_key},
        method='POST')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print('DAILY_REPORT_SENT', day, response.status)
    except HTTPError as error:
        if error.code == 409 and error.headers.get('x-line-accepted-request-id'):
            print('DAILY_REPORT_ALREADY_ACCEPTED', day)
            return
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    group_id = os.environ['DAILY_REPORT_GROUP_ID'].strip()
    if not group_id.startswith('C') or len(group_id) != 33:
        raise ValueError('Invalid LINE group recipient')
    now = datetime.now(TAIPEI)
    start, rows = read_report('group:'+group_id, now)
    report = format_report(start, rows, now)
    if args.preview:
        print(report)
    else:
        push_report(group_id, start.date().isoformat(), report)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('DAILY_REPORT_FAILED', type(error).__name__, file=sys.stderr)
        sys.exit(1)
