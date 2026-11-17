"""Durable Sheet mirror backlog using committed sale event markers, no card copies.

Runs within the HTTP request AFTER LINE reply, never in an idle background thread.
An independent non-waiting mirror lock prevents duplicate workers. Original order
row locks order mirroring against cancellation, without locking the group balance.
Only mirrors committed transactions; never allocates cards, sends LINE or adjusts debt.
"""
import json
from time import perf_counter
from group_credit import CreditError, TEST_CUSTOMER

PENDING = 'sheet:pending:v1'
DONE = 'sheet:done:v1'


def sync_pending_sales(service, ns, group, limit=1):
    started = perf_counter()
    completed = 0
    failed = False
    for _ in range(limit):
        conn = service.get_db()
        try:
            with conn.cursor(cursor_factory=service.dict_cursor) as cur:
                # Never hold the group account while calling Google. A second
                # mirror worker returns immediately instead of occupying another
                # HTTP thread behind a slow Google request. Pending rows persist.
                cur.execute('SELECT pg_try_advisory_xact_lock(hashtext(%s),hashtext(%s)) AS acquired',
                            ('group-sheet-mirror:v2',group))
                if not cur.fetchone()['acquired']:
                    conn.rollback()
                    print('GROUP_SHEET_SYNC_BUSY',flush=True)
                    return dict(completed=completed,failed=False,busy=True)
                cur.execute('''SELECT event_id,order_no,child_orders FROM group_credit_events
                    WHERE group_id=%s AND kind='sale' AND reason=%s
                    ORDER BY created_at,event_id LIMIT 1 FOR UPDATE''', (group,PENDING))
                event = cur.fetchone()
                if not event:
                    conn.rollback()
                    break
                orders = event['child_orders']
                if isinstance(orders,str): orders=json.loads(orders)
                # Cancel locks these exact original transactions before releasing
                # any cards/refunding debt. New sales do not touch these rows.
                # NOWAIT avoids waiting behind cancellation while holding its
                # sale event. Read cancellation state in a NEW statement AFTER
                # locking, so a prior committed cancellation is always visible.
                cur.execute('''SELECT id FROM transactions WHERE group_id=%s AND order_no=ANY(%s)
                    ORDER BY product_id,id FOR UPDATE NOWAIT''', (group,orders))
                if len(cur.fetchall()) != len(orders):
                    raise CreditError('待同步訂單資料不完整。')
                cur.execute('''SELECT t.order_no,t.quantity,t.unit_price,t.sale_amount,t.sold_to,p.code,p.id AS product_id,
                    to_char(t.created_at AT TIME ZONE 'Asia/Taipei','YYYY/MM/DD') AS sale_date,
                    EXISTS(SELECT 1 FROM group_credit_cancelled_orders c WHERE c.order_no=t.order_no) AS cancelled
                    FROM transactions t JOIN products p ON p.id=t.product_id
                    WHERE t.group_id=%s AND t.order_no=ANY(%s) ORDER BY t.id''', (group,orders))
                rows = cur.fetchall()
                if not rows or {r['order_no'] for r in rows} != set(orders) or len({r['sold_to'] for r in rows}) != 1:
                    raise CreditError('待同步訂單資料不完整。')
                customer = rows[0]['sold_to']
                items=[]
                for row in rows:
                    cur.execute("SELECT serial FROM serials WHERE order_no=%s AND status='sold' ORDER BY id",(row['order_no'],))
                    serials=[r['serial'] for r in cur.fetchall()]
                    if not row['cancelled'] and len(serials)!=row['quantity']:
                        raise CreditError('待同步卡片狀態不一致。')
                    items.append(dict(product=dict(id=row['product_id'],code=row['code']),
                        quantity=row['quantity'],unit_price=row['unit_price'],sale_amount=row['sale_amount'],
                        order_no=row['order_no'],serials=serials,sale_date=row['sale_date']))
                # append_sales_rows already uses order IDs to avoid duplicate Sheet rows.
                ok=bool(ns['append_sales_rows'](customer,items))
                active=[item for item,row in zip(items,rows) if not row['cancelled']]
                cancelled=[row['order_no'] for row in rows if row['cancelled']]
                if customer!=TEST_CUSTOMER and active:
                    ok=bool(ns['mark_product_sale'](active,customer)) and ok
                if cancelled:
                    if customer==TEST_CUSTOMER:
                        ok=bool(ns['mark_sales_rows_undone_in_sheet'](cancelled,ns['get_sales_sheet'](customer))) and ok
                    else:
                        ok=bool(ns['mark_sales_rows_undone'](cancelled)) and ok
                if not ok:
                    conn.rollback()
                    failed=True
                    break
                cur.execute("UPDATE group_credit_events SET reason=%s WHERE group_id=%s AND event_id=%s AND kind='sale' AND reason=%s",
                            (DONE,group,event['event_id'],PENDING))
            conn.commit()
            completed+=1
        except Exception as exc:
            conn.rollback()
            failed=True
            # Never log card data, credentials, or exception text; never retry delivery.
            print('GROUP_SHEET_SYNC_PENDING', type(exc).__name__, flush=True)
            break
        finally:
            conn.close()
    print('GROUP_SHEET_SYNC_MS',round((perf_counter()-started)*1000),
          'completed',completed,'failed',failed,flush=True)
    return dict(completed=completed,failed=failed)
