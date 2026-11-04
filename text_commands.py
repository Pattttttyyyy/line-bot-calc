"""Text-only routing; never promotes legacy operators to account administrators."""
import re
from types import SimpleNamespace
from bot_ui import UIStore, inventory_reply
from group_credit import CreditError
from group_calculator import looks_like_calculation, handle_calculator

HELP = ('已改回文字指令，不再顯示按鈕。\n'
        '查庫存：/查庫存\n'
        '管理人員請沿用原本出入庫指令；/入庫 須附商品與卡片資料。\n'
        '客人自助群：/MY1000*數量、/MY3000*數量、/MY5000*數量、/1萬*數量\n'
        '測試自助群：/test100*數量（原價1000、售價905元，須先綁定測試帳戶）\n'
        '群組計算機：+0 查帳；僅管理員可用正數收款、負數增加欠款（支援算式）\n'
        '管理員帳目：/記帳紀錄、/更正記帳 記帳編號 原因；重複或更正均使用文字確認\n'
        '客人群管理員：/撤回（或 /撤回上一動）直接撤回上一筆記帳或發卡，一併調帳並回覆撤回編號\n'
        '核准拿卡：/自助核准 @成員；停權：/自助停權 @成員（僅管理員）\n自助帳況：/自助帳況；仍依原群組、核准成員與額度限制。')

def handle_ui_text(event, ns):
    text = event.message.text.strip()
    if looks_like_calculation(text):
        return handle_calculator(event, ns)
    if not text.startswith('/'):
        return False
    group = ns['get_context_id'](event)
    if not str(group).startswith('group:'):
        return False
    user = ns['get_operator'](event)
    admin = ns['is_admin'](user)
    store = UIStore(ns)
    management = store.admin_group(group)
    try:
        # Existing authorized_users may operate inventory, never gain is_admin.
        if management and text != '/我的ID' and not ns['can_use_bot'](user):
            raise CreditError('⛔ 你沒有操作權限，請管理員使用 /加權限。')
        command = text.split(maxsplit=1)[0]
        if command in ('/確認未使用並回收', '/取消撤回'):
            parts = text.split()
            if len(parts) != 2 or not re.fullmatch('[0-9a-f]{32}', parts[1]):
                raise CreditError('請複製撤回申請回覆中的完整確認或取消指令。')
            from group_credit_handler import handle_group_credit_postback
            data = ('gc_cancel_confirm:' if command == '/確認未使用並回收' else 'gc_cancel_decline:') + parts[1]
            # Reuse the original group/user/token/expiry/admin/idempotency checks.
            proxy = SimpleNamespace(source=event.source, reply_token=event.reply_token,
                                    postback=SimpleNamespace(data=data))
            return handle_group_credit_postback(proxy, ns)
        if text == '/設定管理群組':
            store.bind_admin(group, user, admin)
            ns['reply'](event, '管理群組設定完成。\n' + HELP)
            return True
        if text in ('/選單', '/入庫選單'):
            if not management:
                store.authorize(group, user, admin)
            ns['reply'](event, HELP)
            return True
        if text == '/取消操作':
            ns['reply'](event, '按鈕操作已停用；舊確認畫面不會執行。請使用完整文字指令。')
            return True
        if text == '/入庫' and management:
            ns['reply'](event, '請使用 /入庫 商品，下一行貼入卡片資料；不再使用按鈕入庫。')
            return True
        if text in ('/自助庫存', '/庫存', '/查庫存') and (management or store.credit.registered(group)):
            inventory_reply(event, ns, store, group, user,
                            ns['can_use_bot'](user) if management else admin)
            return True
        # All legacy commands reach their original permission checks unchanged.
        return False
    except CreditError as exc:
        ns['reply'](event, str(exc))
        return True

def handle_ui_postback(event, ns):
    data = str(getattr(event.postback, 'data', '') or '')
    if not data.startswith(('bu:', 'gc_cancel_confirm:', 'gc_cancel_decline:')):
        return False
    ns['reply'](event, '按鈕已停用，未執行操作。請使用文字指令；撤回請重新輸入 /自助撤回 訂單編號。')
    return True
