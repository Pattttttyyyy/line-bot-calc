"""Deliver committed inventory before Sheets; recover only original same-chat cards.

No inventory allocation, balance changes, automatic LINE retries or background work.
Original transactions remain the durable source for explicit recovery and mirroring.
"""
import re
from time import perf_counter

ORDER = re.compile(r'TX[0-9]{14}[0-9A-F]{6}(?:-[0-9]{2})?')


def mirror_sale(ns, customer, items):
    results = []
    for name, args in (('append_sales_rows', (customer, items)),
                       ('mark_product_sale', (items, customer))):
        try:
            results.append(bool(ns[name](*args)))
        except Exception as exc:
            results.append(False)
            print('LEGACY_SALE_MIRROR_ERROR', name, type(exc).__name__, flush=True)
    print('LEGACY_SALE_MIRROR_DONE', all(results), flush=True)
    return all(results)


def deliver_committed_sale(event, ns, customer, items, chunks, order_no, *, resend=False):
    """Called only after commit/close. Never send a second reply on uncertainty."""
    summary = ['原訂單序號補發' if resend else '✅ 出庫完成']
    if customer:
        summary.append(f'客戶：《{customer}》')
    summary.extend(f"{item['product']['code']} × {item['quantity']}" for item in items)
    summary.extend([f'訂單：{order_no}', '原單補發不另扣庫存、不新增帳款。' if resend else
                    '資料庫已出庫，請勿重複下單。',
                    '試算表接續同步；需要重試同步時可用 /同步出庫 訂單編號。'])
    started = perf_counter()
    try:
        ns['reply_messages'](event, list(chunks) + ['\n'.join(summary)])
    except Exception as exc:
        # The caller must not issue a new sale or another reply after a timeout.
        print('LEGACY_SALE_REPLY_UNCERTAIN', type(exc).__name__, flush=True)
        return False
    else:
        print('LEGACY_SALE_REPLY_MS', round((perf_counter() - started) * 1000), flush=True)
        return True
    finally:
        # Request-based Cloud Run supplies CPU here; no idle background thread.
        mirror_sale(ns, customer, items)


def original_sale(ns, context, order_no):
    if not ORDER.fullmatch(order_no):
        raise ValueError('訂單編號格式不正確。')
    conn = ns['get_db']()
    try:
        with conn.cursor(cursor_factory=ns['RealDictCursor']) as cur:
            cur.execute('''SELECT t.order_no,t.sale_batch_no,t.quantity,t.sold_to,
                t.unit_price,t.sale_amount,p.id AS product_id,p.code,
                to_char(t.created_at AT TIME ZONE 'Asia/Taipei','YYYY/MM/DD') AS sale_date
                FROM transactions t JOIN products p ON p.id=t.product_id
                WHERE t.group_id=%s AND (t.order_no=%s OR t.sale_batch_no=%s)
                ORDER BY t.id''', (context, order_no, order_no))
            rows = cur.fetchall()
            if not rows or len({r['sold_to'] for r in rows}) != 1:
                raise ValueError('找不到此聊天室的原出庫訂單。')
            # Only legacy inventory sales, never the independent customer ledger.
            for row in rows:
                cur.execute('''SELECT 1 FROM stock_actions WHERE group_id=%s
                    AND action_type='sale' AND ref_no=%s''',
                    (context, row['sale_batch_no'] or row['order_no']))
                if not cur.fetchone():
                    raise ValueError('原訂單不屬於此出庫流程或已撤回，請人工核對。')
            items = []
            for row in rows:
                cur.execute('''SELECT serial FROM serials WHERE order_no=%s
                    AND product_id=%s AND sold_to=%s AND status='sold' ORDER BY id''',
                    (row['order_no'], row['product_id'], row['sold_to']))
                serials = [r['serial'] for r in cur.fetchall()]
                if len(serials) != row['quantity']:
                    raise ValueError('原訂單卡片數量不完整或已回收，停止補發，請人工核對。')
                items.append(dict(product=dict(id=row['product_id'], code=row['code']),
                    quantity=row['quantity'], order_no=row['order_no'], serials=serials,
                    unit_price=row['unit_price'], sale_amount=row['sale_amount'],
                    sale_date=row['sale_date']))
        return rows[0]['sold_to'], items
    finally:
        try:
            conn.rollback()  # Read only: never allocate, charge or modify a sale.
        finally:
            conn.close()


def handle_sale_recovery(event, ns):
    parts = event.message.text.strip().split()
    if not parts or parts[0] not in ('/補發序號', '/同步出庫'):
        return False
    if not ns['is_admin'](ns['get_operator'](event)):
        ns['reply'](event, '⛔ 只有管理員可以補發原單序號或同步出庫。')
        return True
    if len(parts) != 2:
        ns['reply'](event, '格式：' + parts[0] + ' 原訂單編號；請在原出庫聊天室操作。')
        return True
    try:
        customer, items = original_sale(ns, ns['get_context_id'](event), parts[1])
        if parts[0] == '/同步出庫':
            ok = mirror_sale(ns, customer, items)
            ns['reply'](event, '原單試算表同步完成；未發送序號、未扣庫存或新增帳款。' if ok else
                        '試算表尚未同步完整，可用相同指令重試；未發送序號、未扣庫存或新增帳款。')
            return True
        text = '\n\n'.join(f"【{i['product']['code']} × {i['quantity']}】\n" +
                            '\n'.join(i['serials']) for i in items)
        chunks = ns['split_text_chunks'](text)
        if len(chunks) > 4:
            raise ValueError('原訂單過長，請改用子訂單編號分批補發。')
    except ValueError as exc:
        ns['reply'](event, str(exc))
        return True
    deliver_committed_sale(event, ns, customer, items, chunks, parts[1], resend=True)
    return True
