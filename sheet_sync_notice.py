"""Private, best-effort administrator alerts; never include cards or exceptions."""
import re
import threading
from time import monotonic

_sent = {}
_lock = threading.Lock()
_ttl = 300


def notify_sheet_failure(ns, order_no=None, *, credit=False):
    """Alert only configured admins, after original delivery/mirror DB locks close.

    Each recipient is independent. Failed/ambiguous push is not automatically
    retried. A five-minute in-process guard limits repeated backlog notifications.
    No notification modifies stock, balances, sale events or synchronization state.
    """
    order = str(order_no or '')
    if not re.fullmatch(r'TX[0-9]{14}[0-9A-F]{6}(?:-[0-9]{2})?', order):
        order = ''
    command = '/同步發卡紀錄' if credit else '/同步出庫 ' + order
    if not credit and not order:
        print('SHEET_ALERT_MISSING_ORDER', flush=True)
        return
    text = ('⚠️ 試算表同步未完成\n' + (f'訂單：{order}\n' if order else '') +
            '原訂單已保留，請勿重複下單或重新扣帳。\n'
            '請回原出庫聊天室重試同步：\n' + command +
            '\n此指令只同步試算表，不會再發卡或扣庫存。')
    recipients = sorted({str(user) for user in ns.get('ADMIN_LINE_USER_IDS', ())
                         if re.fullmatch(r'U[0-9a-fA-F]{32}', str(user))})
    if not recipients:
        print('SHEET_ALERT_NO_ADMIN', flush=True)
        return
    for user in recipients:
        key = (user, order, credit)
        now = monotonic()
        with _lock:
            for old in [k for k,v in _sent.items() if now-v >= _ttl]:
                del _sent[old]
            if key in _sent:
                continue
            _sent[key] = now
        try:
            with ns['ApiClient'](ns['configuration']) as client:
                ns['MessagingApi'](client).push_message(
                    ns['PushMessageRequest'](to=user, messages=[ns['TextMessage'](text=text)]),
                    _request_timeout=8)
            print('SHEET_ALERT_SENT', flush=True)
        except Exception as exc:
            with _lock:
                _sent.pop(key, None)
            print('SHEET_ALERT_FAILED', type(exc).__name__, flush=True)
