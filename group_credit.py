"""Group credit accounting; no network access or startup side effects on import.

Debt is positive when the group owes money. Customer identity is bound to the
group, not supplied by the buyer. Every operation locks the same account row;
credit approval, inventory allocation, priced sales and debt commit together.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from collections import Counter
import json
import re
import secrets

MY_FACES = {'MY1000': 1000, 'MY3000': 3000, 'MY5000': 5000, 'MY1萬': 10000}
RATE = Decimal('0.905')
TEST_CUSTOMER = '測試'
TEST_CODE = 'test100'
TEST_FACE = 1000
MAX_MONEY = Decimal('9999999999999999.99')


class CreditError(ValueError):
    pass


def money(value, *, positive=False):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or abs(n) > MAX_MONEY or n != n.quantize(Decimal('.01')):
            raise CreditError('金額最多兩位小數，且必須在有效範圍內。')
        if positive and n <= 0:
            raise CreditError('金額必須大於0。')
        return n.quantize(Decimal('.01'))
    except (InvalidOperation, TypeError) as exc:
        raise CreditError('金額格式不正確。') from exc


def customer_products(customer):
    if customer == TEST_CUSTOMER:
        return {TEST_CODE: TEST_FACE}
    if customer == '軒':
        return MY_FACES
    raise CreditError('此客戶尚未設定自助商品與售價。')


def quote(code, quantity, customer='軒'):
    faces = customer_products(customer)
    if code not in faces:
        if customer == TEST_CUSTOMER:
            raise CreditError('測試群只能拿 test100 測試卡，不能拿正式卡。')
        raise CreditError('這個商品尚未設定自助售價，目前僅開放MY卡。')
    if type(quantity) is not int or not 1 <= quantity <= 500:
        raise CreditError('數量需為1～500的整數。')
    unit = (Decimal(faces[code]) * RATE).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return unit, money(unit * quantity)


def checked_debt(debt, limit, amount, enabled):
    debt, amount = money(debt), money(amount, positive=True)
    if not enabled:
        raise CreditError('此群組自助拿卡尚未開放。')
    if limit is None:
        raise CreditError('請管理員先設定欠款上限。')
    limit = money(limit, positive=True)
    if debt >= limit or debt + amount > limit:
        raise CreditError(f'⛔ 額度不足：目前欠款{debt:,.2f}元，上限{limit:,.2f}元；本筆{amount:,.2f}元。請管理員處理後再拿卡。')
    return money(debt + amount)


def group_context(group_id):
    if not isinstance(group_id, str) or not re.fullmatch(r'group:C[0-9a-fA-F]{32}', group_id):
        raise CreditError('此功能只能在LINE群組中使用。')
    return group_id


def validate_identity(operator, event_id):
    if not re.fullmatch(r'U[0-9a-fA-F]{32}', str(operator)):
        raise CreditError('無法確認發話者身分，這次不發卡。')
    if not event_id or len(str(event_id)) > 160:
        raise CreditError('缺少有效訊息識別碼，這次不發卡。')


def can_buy(cur, account, operator, admin):
    if admin:
        return
    cur.execute('SELECT active FROM group_credit_members WHERE group_id=%s AND line_user_id=%s',
                (account['group_id'], operator))
    row = cur.fetchone()
    if not row or not row['active']:
        raise CreditError('⛔ 你尚未獲得此群組的拿卡權限，請管理員核准。')


class GroupCredit:
    def __init__(self, get_db, dict_cursor, find_product, create_order_no, split_chunks):
        self.get_db = get_db
        self.dict_cursor = dict_cursor
        self.find_product = find_product
        self.create_order_no = create_order_no
        self.split_chunks = split_chunks

    def registered(self, group_id):
        if not str(group_id).startswith('group:'):
            return False
        conn = self.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT 1 FROM group_credit_accounts WHERE group_id=%s', (group_id,))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def _account(self, cur, group_id):
        cur.execute('SELECT * FROM group_credit_accounts WHERE group_id=%s FOR UPDATE', (group_id,))
        account = cur.fetchone()
        if not account:
            raise CreditError('請管理員先在此群組執行 /自助綁定 軒。')
        return account

    def _previous(self, cur, group_id, event_id):
        cur.execute('SELECT kind,order_no,debt_after,delivery_status FROM group_credit_events WHERE group_id=%s AND event_id=%s',
                    (group_id, str(event_id)))
        row = cur.fetchone()
        if row:
            return {'duplicate': True, **dict(row)}
        return None

    def _event(self, cur, group_id, event_id, kind, amount, debt_after, operator,
               reason='', order_no=None, child_orders=(), delivery='not_applicable'):
        cur.execute('''INSERT INTO group_credit_events
            (group_id,event_id,kind,amount,debt_after,operator,reason,order_no,child_orders,delivery_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)''',
            (group_id,str(event_id),kind,amount,debt_after,operator,reason,order_no,json.dumps(list(child_orders)),delivery))

    def manage(self, group_id, operator, event_id, command, value=None, reason='', admin=False):
        group_context(group_id)
        validate_identity(operator, event_id)
        if command != 'status' and not admin:
            raise CreditError('⛔ 只有管理員可以修改群組帳款、額度及權限。')
        conn = self.get_db()
        try:
            with conn.cursor(cursor_factory=self.dict_cursor) as cur:
                if command == 'bind':
                    if value not in ('軒', TEST_CUSTOMER):
                        raise CreditError('目前可綁定軒，或使用 /自助綁定 測試；測試卡原價1000、售價905元。')
                    cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(group_id,))
                    cur.execute('SELECT 1 FROM bot_ui_admin_groups WHERE group_id=%s',(group_id,))
                    if cur.fetchone():
                        raise CreditError('此群組是管理群組，請另選客人自助群組。')
                    cur.execute('''INSERT INTO group_credit_accounts(group_id,customer)
                        VALUES (%s,%s) ON CONFLICT(group_id) DO NOTHING''', (group_id,value))
                account = self._account(cur, group_id)
                if command == 'status':
                    can_buy(cur, account, operator, admin)
                    conn.rollback()
                    return dict(account)
                previous = self._previous(cur, group_id, event_id)
                if previous:
                    conn.rollback()
                    return previous
                debt = money(account['debt'])
                delta = Decimal('0')
                if command == 'bind':
                    if account['customer'] != value:
                        raise CreditError('已綁定的群組不能藉重新綁定更換客戶。')
                    # Rebinding never resets debt, members, credit limit or enabled state.
                elif command == 'limit':
                    limit = money(value, positive=True)
                    cur.execute('UPDATE group_credit_accounts SET credit_limit=%s,updated_at=now() WHERE group_id=%s', (limit,group_id))
                elif command in ('charge','payment','adjust_credit'):
                    if not reason.strip() or len(reason) > 200:
                        raise CreditError('帳款調整必須附上1～200字原因。')
                    amount = money(value, positive=True)
                    delta = amount if command == 'charge' else -amount
                    debt = money(debt + delta)
                    if debt < 0:
                        raise CreditError('減少金額超過目前欠款；本版本不建立負欠款或預付款。')
                    cur.execute('UPDATE group_credit_accounts SET debt=%s,updated_at=now() WHERE group_id=%s', (debt,group_id))
                elif command in ('enable','disable'):
                    if command == 'enable' and account['credit_limit'] is None:
                        raise CreditError('請先設定欠款上限，不能無上限開放。')
                    cur.execute('UPDATE group_credit_accounts SET enabled=%s,updated_at=now() WHERE group_id=%s', (command=='enable',group_id))
                elif command in ('approve','revoke'):
                    if not re.fullmatch(r'U[0-9a-fA-F]{32}',str(value)):
                        raise CreditError('請提供有效LINE User ID。')
                    cur.execute('''INSERT INTO group_credit_members(group_id,line_user_id,active)
                        VALUES (%s,%s,%s) ON CONFLICT(group_id,line_user_id) DO UPDATE SET active=EXCLUDED.active''',
                        (group_id,value,command=='approve'))
                    reason = str(value)
                else:
                    raise CreditError('未知的管理指令。')
                self._event(cur, group_id,event_id,command,delta,debt,operator,reason)
            conn.commit()
            return {'duplicate': False, 'debt': debt, 'command': command}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def calculate(self, group_id, operator, event_id, raw, admin=False):
        from group_calculator import parse_calculation
        expression, amount, note = parse_calculation(raw)
        group_context(group_id)
        validate_identity(operator, event_id)
        # Positive = receipt/payment, negative = charge, matching the calculator.
        # Zero is a read-only balance lookup for an approved member or root admin.
        if amount != 0 and not admin:
            raise CreditError('⛔ 只有管理員可以記帳；核准成員可用 +0 查帳。')
        conn = self.get_db()
        try:
            with conn.cursor(cursor_factory=self.dict_cursor) as cur:
                account = self._account(cur, group_id)
                if amount == 0:
                    can_buy(cur, account, operator, admin)
                else:
                    previous = self._previous(cur, group_id, event_id)
                    if previous:
                        conn.rollback()
                        return previous
                before = money(account['debt'])
                debt = money(before - amount)
                if debt < 0:
                    raise CreditError('收款金額超過目前欠款，整筆未記帳；目前不建立預付款。')
                result = dict(duplicate=False, customer=account['customer'], expression=expression,
                              amount=amount, previous_debt=before, debt=debt,
                              credit_limit=account['credit_limit'], enabled=account['enabled'], note=note)
                if amount == 0:
                    conn.rollback()
                    return result
                cur.execute('UPDATE group_credit_accounts SET debt=%s,updated_at=now() WHERE group_id=%s', (debt, group_id))
                reason = '計算機 ' + expression + (' #' + note if note else '')
                self._event(cur, group_id, event_id, 'calculator', -amount, debt, operator, reason)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def buy(self, group_id, operator, event_id, requested_items, admin=False, transaction=None):
        group_context(group_id)
        validate_identity(operator, event_id)
        if not requested_items or sum(x['quantity'] for x in requested_items) > 500:
            raise CreditError('一次最多500張。')
        owns_connection = transaction is None
        conn = self.get_db() if owns_connection else transaction
        try:
            with conn.cursor(cursor_factory=self.dict_cursor) as cur:
                account = self._account(cur, group_id)
                can_buy(cur, account, operator, admin)
                previous = self._previous(cur, group_id, event_id)
                if previous:
                    if owns_connection: conn.rollback()
                    return previous
                prepared=[]
                for item in requested_items:
                    product = self.find_product(conn, item['product_text'])
                    if not product:
                        raise CreditError('找不到商品；整筆沒有出庫。')
                    unit,amount = quote(product['code'], item['quantity'], account['customer'])
                    prepared.append({'product':product,'quantity':item['quantity'],'unit_price':unit,'sale_amount':amount})
                total = sum((x['sale_amount'] for x in prepared), Decimal('0'))
                debt_after = checked_debt(account['debt'],account['credit_limit'],total,account['enabled'])
                # Canonical ordering and SKIP LOCKED also protect inventory shared with legacy sales.
                prepared.sort(key=lambda x:x['product']['id'])
                order_no = self.create_order_no()
                sections=[]
                for i,item in enumerate(prepared,1):
                    cur.execute('''SELECT id,serial FROM serials WHERE product_id=%s AND status='available'
                        ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s''', (item['product']['id'],item['quantity']))
                    cards=cur.fetchall()
                    if len(cards)!=item['quantity']:
                        raise CreditError('庫存不足或正在處理其他訂單；整筆沒有出庫、沒有增加欠款。')
                    child=f'{order_no}-{i:02d}'
                    item.update(order_no=child,serials=[c['serial'] for c in cards])
                    cur.execute('''UPDATE serials SET status='sold',sold_to=%s,order_no=%s,sold_at=now(),operator=%s
                        WHERE id=ANY(%s) AND status='available' ''', (account['customer'],child,operator,[c['id'] for c in cards]))
                    if cur.rowcount!=len(cards):
                        raise CreditError('庫存狀態已變更；整筆沒有出庫。')
                    cur.execute('''INSERT INTO transactions
                        (order_no,product_id,sold_to,quantity,operator,group_id,sale_batch_no,unit_price,sale_amount)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (child,item['product']['id'],account['customer'],item['quantity'],operator,group_id,order_no,item['unit_price'],item['sale_amount']))
                    sections.append(f"【{item['product']['code']} × {item['quantity']}】\n"+'\n'.join(item['serials']))
                chunks=self.split_chunks('\n\n'.join(sections))
                if len(chunks)>4:
                    raise CreditError('卡片內容超過LINE單次回覆長度；整筆取消，請減少張數。')
                # Do not add legacy stock_actions: its generic undo would release delivered cards
                # without a matching credit adjustment. This flow has its own immutable event log.
                cur.execute('UPDATE group_credit_accounts SET debt=%s,updated_at=now() WHERE group_id=%s', (debt_after,group_id))
                self._event(cur,group_id,event_id,'sale',total,debt_after,operator,order_no=order_no,
                            child_orders=[x['order_no'] for x in prepared],delivery='pending')
            if owns_connection: conn.commit()
            return {'duplicate':False,'customer':account['customer'],'order_no':order_no,'items':prepared,
                    'serial_chunks':chunks,'amount':total,'debt':debt_after,'credit_limit':money(account['credit_limit'])}
        except Exception:
            if owns_connection: conn.rollback()
            raise
        finally:
            if owns_connection: conn.close()

    def delivery(self, group_id, event_id, status):
        if status not in ('delivered','uncertain'):
            raise ValueError('Invalid delivery status')
        conn=self.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute('''UPDATE group_credit_events SET delivery_status=%s
                    WHERE group_id=%s AND event_id=%s AND kind='sale' AND delivery_status='pending' ''',
                    (status,group_id,str(event_id)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _cancel_sale(self, cur, group_id, order_no):
        cur.execute('''SELECT * FROM group_credit_events WHERE group_id=%s AND kind='sale'
            AND (order_no=%s OR child_orders @> %s::jsonb)''',
            (group_id,order_no,json.dumps([order_no])))
        sale=cur.fetchone()
        if not sale:
            raise CreditError('找不到此群組的自助訂單；只可撤回本群組自助訂單。')
        return sale

    def request_cancel(self, group_id, operator, message_id, order_no, admin=False):
        group_context(group_id)
        validate_identity(operator,message_id)
        if not admin:
            raise CreditError('⛔ 只有管理員可以申請撤回自助訂單。')
        conn=self.get_db()
        try:
            with conn.cursor(cursor_factory=self.dict_cursor) as cur:
                account=self._account(cur,group_id)
                sale=self._cancel_sale(cur,group_id,order_no)
                parent=sale['order_no']
                cur.execute('SELECT 1 FROM group_credit_cancelled_orders WHERE group_id=%s AND parent_order_no=%s', (group_id,parent))
                if cur.fetchone():
                    raise CreditError('此訂單已撤回，不會再次退帳或回收。')
                cur.execute('''SELECT token,state,expires_at>clock_timestamp() AS valid FROM group_credit_cancel_requests
                    WHERE group_id=%s AND message_id=%s''', (group_id,str(message_id)))
                previous=cur.fetchone()
                if previous:
                    if previous['state']!='pending' or not previous['valid']:
                        raise CreditError('這則撤回申請已失效；如需處理，請重新傳送撤回指令。')
                    token=previous['token']
                else:
                    token=secrets.token_hex(16)
                    cur.execute('''INSERT INTO group_credit_cancel_requests
                        (token,group_id,message_id,order_no,requested_by) VALUES (%s,%s,%s,%s,%s)''',
                        (token,group_id,str(message_id),parent,operator))
                orders=sale['child_orders']
                if isinstance(orders,str):orders=json.loads(orders)
                cur.execute('''SELECT p.code,t.quantity FROM transactions t JOIN products p ON p.id=t.product_id
                    WHERE t.order_no=ANY(%s) AND t.group_id=%s ORDER BY t.id''',(orders,group_id))
                rows=cur.fetchall()
                if len(rows)!=len(orders):
                    raise CreditError('原訂單明細不完整，需人工核對；尚未撤回。')
            conn.commit()
            return dict(token=token,order_no=parent,amount=money(sale['amount']),
                        customer=account['customer'],quantity=sum(r['quantity'] for r in rows),
                        items=[dict(r) for r in rows])
        except Exception:
            conn.rollback()
            raise
        finally:conn.close()

    def confirm_cancel(self, group_id, operator, token, admin=False, decline=False):
        group_context(group_id)
        validate_identity(operator,'cancel:'+str(token))
        if not admin:
            raise CreditError('⛔ 只有管理員可以確認卡片未使用並回收；客人不能自行撤回。')
        if not re.fullmatch('[0-9a-f]{32}',str(token)):
            raise CreditError('撤回按鈕無效。')
        conn=self.get_db()
        try:
            with conn.cursor(cursor_factory=self.dict_cursor) as cur:
                account=self._account(cur,group_id)
                cur.execute('''SELECT *,expires_at>clock_timestamp() AS valid FROM group_credit_cancel_requests
                    WHERE token=%s AND group_id=%s FOR UPDATE''',(token,group_id))
                request=cur.fetchone()
                if not request:
                    raise CreditError('這個撤回按鈕不屬於此群組，或已失效。')
                parent=request['order_no']
                cur.execute('SELECT order_no FROM group_credit_cancelled_orders WHERE group_id=%s AND parent_order_no=%s',(group_id,parent))
                cancelled=cur.fetchall()
                if cancelled:
                    conn.rollback()
                    return dict(duplicate=True,order_no=parent,debt=money(account['debt']))
                if request['state']!='pending' or not request['valid']:
                    raise CreditError('撤回申請已取消或超過5分鐘；請重新傳送指令。')
                if decline:
                    cur.execute("UPDATE group_credit_cancel_requests SET state='declined' WHERE token=%s",(token,))
                    conn.commit()
                    return dict(declined=True,order_no=parent)
                sale=self._cancel_sale(cur,group_id,parent)
                orders=sale['child_orders']
                if isinstance(orders,str):orders=json.loads(orders)
                cur.execute('''SELECT t.*,p.code FROM transactions t JOIN products p ON p.id=t.product_id
                    WHERE t.order_no=ANY(%s) AND t.group_id=%s ORDER BY t.product_id,t.id FOR UPDATE OF t''',(orders,group_id))
                transactions=cur.fetchall()
                if len(transactions)!=len(orders) or {t['order_no'] for t in transactions}!=set(orders):
                    raise CreditError('訂單紀錄不完整；尚未回收或退帳，請人工核對。')
                amount=money(sale['amount'],positive=True)
                debt=money(account['debt'])-amount
                if debt<0:
                    raise CreditError('目前欠款小於原訂單金額，可能已收款或先行減帳；請先人工對帳，避免重複退帳。本次未撤回。')
                items=[]
                for t in transactions:
                    cur.execute('''SELECT id,serial,status,product_id,sold_to FROM serials
                        WHERE order_no=%s ORDER BY id FOR UPDATE''',(t['order_no'],))
                    cards=cur.fetchall()
                    if len(cards)!=t['quantity'] or any(c['status']!='sold' or c['product_id']!=t['product_id'] or c['sold_to']!=t['sold_to'] for c in cards):
                        raise CreditError('原卡片狀態或數量不一致；整筆未撤回，需人工核對。')
                    ids=[c['id'] for c in cards]
                    cur.execute('''INSERT INTO group_credit_cancelled_orders
                        (order_no,group_id,parent_order_no,request_token,confirmed_by,original_transaction,serial_ids)
                        VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)''',
                        (t['order_no'],group_id,parent,token,operator,json.dumps(dict(t),default=str),ids))
                    cur.execute('''UPDATE serials SET status='available',sold_to=NULL,order_no=NULL,sold_at=NULL,operator=NULL
                        WHERE id=ANY(%s) AND status='sold' AND order_no=%s''',(ids,t['order_no']))
                    if cur.rowcount!=len(ids):
                        raise CreditError('卡片狀態已變更；整筆未撤回。')
                    items.append(dict(product=dict(id=t['product_id'],code=t['code']),quantity=t['quantity'],
                                      order_no=t['order_no'],serials=[c['serial'] for c in cards],serial_ids=ids))
                cur.execute('UPDATE group_credit_accounts SET debt=%s,updated_at=now() WHERE group_id=%s',(debt,group_id))
                cur.execute("UPDATE group_credit_cancel_requests SET state='confirmed' WHERE token=%s",(token,))
                self._event(cur,group_id,'cancel:'+token,'cancel',-amount,debt,operator,
                            reason='管理員確認未使用並回收 '+parent,child_orders=orders)
            conn.commit()
            return dict(duplicate=False,order_no=parent,amount=amount,debt=debt,items=items,customer=account['customer'],
                        quantity=sum(i['quantity'] for i in items))
        except Exception:
            conn.rollback()
            raise
        finally:conn.close()

    def resend(self, group_id, operator, event_id, order_no, admin=False):
        group_context(group_id)
        validate_identity(operator,event_id)
        if not admin:
            raise CreditError('⛔ 只有管理員可以補發已保留的原訂單卡片。')
        conn=self.get_db()
        try:
            with conn.cursor(cursor_factory=self.dict_cursor) as cur:
                account=self._account(cur,group_id)
                previous=self._previous(cur,group_id,event_id)
                if previous:
                    conn.rollback()
                    return previous
                cur.execute("SELECT child_orders FROM group_credit_events WHERE group_id=%s AND order_no=%s AND kind='sale'", (group_id,order_no))
                event=cur.fetchone()
                if not event:
                    raise CreditError('找不到此群組的自助訂單。')
                orders=event['child_orders']
                if isinstance(orders,str):orders=json.loads(orders)
                cur.execute('''SELECT s.serial,p.code,s.order_no FROM serials s JOIN products p ON p.id=s.product_id
                    WHERE s.order_no=ANY(%s) AND s.status='sold' AND s.sold_to=%s ORDER BY s.order_no,s.id''', (orders,account['customer']))
                cards=cur.fetchall()
                cur.execute('SELECT order_no,quantity FROM transactions WHERE order_no=ANY(%s) AND group_id=%s', (orders,group_id))
                expected={r['order_no']:r['quantity'] for r in cur.fetchall()}
                actual=Counter(c['order_no'] for c in cards)
                if not cards or set(expected)!=set(orders) or dict(actual)!=expected:
                    raise CreditError('原訂單卡片狀態不完整，需人工核對；不重新取卡。')
                chunks=self.split_chunks('\n'.join(f"【{c['code']}】\n{c['serial']}" for c in cards))
                if len(chunks)>4:
                    raise CreditError('原訂單內容過長，需人工處理。')
                self._event(cur,group_id,event_id,'resend',0,account['debt'],operator,reason=order_no)
            conn.commit()
            return {'duplicate':False,'serial_chunks':chunks,'order_no':order_no}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
