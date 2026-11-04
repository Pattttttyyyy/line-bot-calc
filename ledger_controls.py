"""Immutable ledger audit, duplicate prompts and compensating corrections."""
import json
import re
import secrets
from decimal import Decimal
from group_credit import CreditError, money, can_buy, group_context, validate_identity

MANUAL = ('calculator', 'charge', 'payment', 'adjust_credit')
PENDING = ('calculator_repeat_pending', 'calculator_reverse_pending')


def identity(group, user, event, admin):
    group_context(group)
    validate_identity(user, event)
    if not admin:
        raise CreditError('⛔ 只有管理員可以查記帳明細、確認記帳及更正帳款。')


def payload(row):
    value = row['child_orders']
    if isinstance(value, str): value = json.loads(value)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise CreditError('確認資料不完整，請重新提出申請。')
    return value[0]


def changed(cur, group, source):
    cur.execute("SELECT 1 FROM group_credit_events WHERE group_id=%s AND kind='calculator_reversal' AND child_orders @> %s::jsonb",
                (group, json.dumps([source])))
    return cur.fetchone() is not None


def original(cur, group, reference):
    if not isinstance(reference, str) or not 1 <= len(reference) <= 160:
        raise CreditError('記帳編號格式不正確。')
    cur.execute('''SELECT * FROM group_credit_events WHERE group_id=%s
        AND (order_no=%s OR event_id=%s) AND kind=ANY(%s)''', (group, reference, reference, list(MANUAL)))
    row = cur.fetchone()
    if not row:
        raise CreditError('找不到本群的手動記帳；拿卡訂單請用 /自助撤回，不可用更正記帳回收卡片。')
    if changed(cur, group, row['event_id']):
        raise CreditError('這筆記帳已更正，不會重複沖回。')
    return row


def write(svc, cur, account, user, event, amount, reason, kind='calculator', source=()):
    # Amount is the debt delta. Keep the original entry and append compensation.
    before = money(account['debt'])
    debt = money(before + amount)
    reference = ('AR' if kind == 'calculator_reversal' else 'AC') + secrets.token_hex(8)
    cur.execute('UPDATE group_credit_accounts SET debt=%s,updated_at=now() WHERE group_id=%s', (debt, account['group_id']))
    svc._event(cur, account['group_id'], event, kind, amount, debt, user,
               reason=reason, order_no=reference, child_orders=source)
    return dict(duplicate=False, customer=account['customer'], expression=reason,
                amount=-amount, previous_debt=before, debt=debt, record_id=reference,
                credit_limit=account['credit_limit'], enabled=account['enabled'], note=reason)


def pending(svc, cur, account, user, event, kind, data):
    token = 'CP' + secrets.token_hex(16)
    svc._event(cur, account['group_id'], event, kind, Decimal('0'), money(account['debt']), user,
               reason='待確認，尚未調帳', order_no=token, child_orders=[data])
    return dict(needs_confirmation=True, token=token, request_kind=kind, **data)


def resume(cur, group, event):
    cur.execute('''SELECT *,created_at > clock_timestamp()-interval '5 minutes' AS valid
        FROM group_credit_events WHERE group_id=%s AND event_id=%s''', (group, event))
    row = cur.fetchone()
    if not row:
        raise CreditError('找不到此確認申請，請重新輸入原指令。')
    cur.execute('''SELECT kind,order_no,debt_after,delivery_status FROM group_credit_events
        WHERE group_id=%s AND event_id=%s''',(group,'ledger-confirm:'+row['order_no']))
    completed = cur.fetchone()
    if completed:
        return dict(duplicate=True, **dict(completed))
    if not row['valid']:
        raise CreditError('確認申請已超過5分鐘，請重新輸入原指令。')
    return dict(needs_confirmation=True, token=row['order_no'], request_kind=row['kind'], **payload(row))


def calculate(svc, group, user, event, raw, admin=False):
    from group_calculator import parse_calculation
    expression, amount, note = parse_calculation(raw)
    group_context(group)
    validate_identity(user, event)
    if amount != 0 and not admin:
        raise CreditError('⛔ 只有管理員可以記帳；核准成員可用 +0 查帳。')
    conn = svc.get_db()
    try:
        with conn.cursor(cursor_factory=svc.dict_cursor) as cur:
            account = svc._account(cur, group)
            if amount == 0:
                can_buy(cur, account, user, admin)
                before = money(account['debt'])
                conn.rollback()
                return dict(duplicate=False, customer=account['customer'], expression=expression,
                            amount=amount, previous_debt=before, debt=before, note=note,
                            credit_limit=account['credit_limit'], enabled=account['enabled'])
            previous = svc._previous(cur, group, event)
            if previous:
                result = resume(cur, group, event) if previous['kind'] in PENDING else previous
                conn.rollback()
                return result
            # Account lock serializes both the lookback and writes with sales.
            cur.execute('''SELECT event_id FROM group_credit_events e WHERE group_id=%s
                AND operator=%s AND kind=ANY(%s) AND amount=%s
                AND created_at > clock_timestamp()-interval '120 seconds'
                AND NOT EXISTS (SELECT 1 FROM group_credit_events r WHERE r.group_id=e.group_id
                    AND r.kind='calculator_reversal' AND r.child_orders @> jsonb_build_array(e.event_id))
                ORDER BY created_at DESC,event_id DESC LIMIT 1''', (group, user, list(MANUAL), -amount))
            duplicate = cur.fetchone()
            if duplicate:
                result = pending(svc, cur, account, user, event, PENDING[0],
                                 dict(expression=expression, note=note, amount=str(amount)))
            else:
                result = write(svc, cur, account, user, event, -amount,
                               '計算機 '+expression+(' #'+note if note else ''))
                result.update(expression=expression, note=note)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally: conn.close()


def history(svc, group, user, admin=False):
    identity(group, user, 'history', admin)
    conn = svc.get_db()
    try:
        with conn.cursor(cursor_factory=svc.dict_cursor) as cur:
            svc._account(cur, group)
            cur.execute('''SELECT event_id,kind,amount,debt_after,reason,order_no,created_at,
                EXISTS(SELECT 1 FROM group_credit_events r WHERE r.group_id=e.group_id
                  AND r.kind='calculator_reversal' AND r.child_orders @> jsonb_build_array(e.event_id)) AS corrected
                FROM group_credit_events e WHERE group_id=%s AND kind=ANY(%s)
                ORDER BY created_at DESC,event_id DESC LIMIT 10''',
                (group, list(MANUAL)+['sale','cancel','calculator_reversal']))
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.rollback()
        conn.close()


def request_reverse(svc, group, user, event, reference, reason, admin=False):
    identity(group, user, event, admin)
    if not reason.strip() or len(reason) > 60:
        raise CreditError('格式：/更正記帳 記帳編號 原因（1～60字）；會先顯示文字確認。')
    conn = svc.get_db()
    try:
        with conn.cursor(cursor_factory=svc.dict_cursor) as cur:
            account = svc._account(cur, group)
            previous = svc._previous(cur, group, event)
            if previous:
                result = resume(cur, group, event) if previous['kind'] in PENDING else previous
                conn.rollback()
                return result
            if reference is None:
                # Choose under the same account lock; pending/query events are
                # not movements. Never silently skip a newer sale or correction.
                cur.execute('''SELECT event_id,order_no,kind FROM group_credit_events
                    WHERE group_id=%s AND kind=ANY(%s)
                    ORDER BY created_at DESC,event_id DESC LIMIT 1''',
                    (group, list(MANUAL)+['sale','cancel','calculator_reversal']))
                latest = cur.fetchone()
                if not latest:
                    raise CreditError('本群目前沒有可撤回的記帳。')
                if latest['kind'] in ('sale','cancel'):
                    raise CreditError('最近一筆是拿卡或發卡撤回，不會用記帳撤回處理。拿卡訂單請用 /自助撤回 訂單編號；指定手動帳可用 /更正記帳 記帳編號 原因。')
                if latest['kind']=='calculator_reversal':
                    raise CreditError('最近一筆已是記帳更正，不會再次沖回或往前撤回其他帳目。請用 /記帳紀錄 查明細。')
                reference = latest['order_no'] or latest['event_id']
            row = original(cur, group, reference)
            # A refund may create prepayment, but must remain a valid amount.
            money(money(account['debt']) - money(row['amount']))
            result = pending(svc, cur, account, user, event, PENDING[1],
                             dict(source=row['event_id'], reference=reference, note=reason,
                                  amount=str(money(row['amount']))))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally: conn.close()


def confirm(svc, group, user, token, admin=False, decline=False):
    identity(group, user, 'ledger-confirm', admin)
    if not re.fullmatch('CP[0-9a-f]{32}', str(token)):
        raise CreditError('記帳確認碼格式不正確。')
    event = 'ledger-confirm:'+token
    conn = svc.get_db()
    try:
        with conn.cursor(cursor_factory=svc.dict_cursor) as cur:
            account = svc._account(cur, group)
            previous = svc._previous(cur, group, event)
            if previous:
                conn.rollback()
                return previous
            cur.execute('''SELECT *,created_at > clock_timestamp()-interval '5 minutes' AS valid
                FROM group_credit_events WHERE group_id=%s AND order_no=%s AND kind=ANY(%s)''',
                (group, token, list(PENDING)))
            row = cur.fetchone()
            if not row or not row['valid']:
                raise CreditError('確認碼不屬於本群或已超過5分鐘，請重新申請。')
            if row['kind'] == PENDING[0] and row['operator'] != user:
                raise CreditError('重複記帳需由提出算式的原管理員確認。')
            data = payload(row)
            if decline:
                svc._event(cur, group, event, 'calculator_declined', Decimal('0'), money(account['debt']), user,
                           reason='取消記帳確認', child_orders=[token])
                result = dict(declined=True)
            elif row['kind'] == PENDING[0]:
                from group_calculator import parse_calculation
                expression, amount, note = parse_calculation(data['expression']+(' #'+data['note'] if data['note'] else ''))
                result = write(svc, cur, account, user, event, -amount,
                               '確認重複 '+expression+(' #'+note if note else ''))
                result.update(expression=expression, note=note)
            else:
                source = original(cur, group, data['source'])
                result = write(svc, cur, account, user, event, -money(source['amount']),
                               '更正 '+data['reference']+' #'+data['note'],
                               'calculator_reversal', [source['event_id']])
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally: conn.close()
