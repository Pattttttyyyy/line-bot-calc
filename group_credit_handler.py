"""Optional LINE adapter; dispatch only when the global feature flag is enabled."""
from group_credit import CreditError, GroupCredit, TEST_CUSTOMER, balance_text

COMMANDS = {
    '/自助綁定':'bind', '/欠款上限':'limit', '/增加欠款':'charge',
    '/登記收款':'payment', '/減少欠款':'adjust_credit',
    '/開放自助':'enable', '/關閉自助':'disable',
    '/自助核准':'approve', '/自助停權':'revoke', '/自助帳況':'status',
}


def handle_group_credit(event, ns):
    group_id = ns['get_context_id'](event)
    text = event.message.text.strip()
    command = text.split(maxsplit=1)[0]
    if not group_id.startswith('group:') or command == '/我的ID':
        return False
    service = GroupCredit(ns['get_db'],ns['RealDictCursor'],ns['find_product'],
                          ns['create_order_no'],ns['split_text_chunks'])
    known = command in COMMANDS or command in ('/自助補發','/自助撤回')
    if not known and not service.registered(group_id):
        return False
    operator = ns['get_operator'](event)
    admin = ns['is_admin'](operator)
    event_id = str(getattr(event.message,'id','') or '')
    try:
        if command in COMMANDS:
            action = COMMANDS[command]
            parts = text.split(maxsplit=2)
            if action in ('enable','disable','status'):
                if len(parts)!=1:
                    raise CreditError('這個指令不需附加參數。')
                value,reason=None,''
            else:
                if len(parts)<2:
                    raise CreditError(f'請在{command}後填入'+('金額與原因。' if action in ('charge','payment','adjust_credit') else '設定值。'))
                value = parts[1]
                reason = parts[2] if len(parts)==3 else ''
                if action not in ('charge','payment','adjust_credit') and reason:
                    raise CreditError('參數格式不正確。')
            result=service.manage(group_id,operator,event_id,action,value,reason,admin)
            if result.get('duplicate'):
                ns['reply'](event,'這則管理指令已處理，不會重複調帳。')
            elif action=='status':
                limit=result['credit_limit']
                ns['reply'](event,f"群組客戶：{result['customer']}\n{balance_text(result['debt'])}\n欠款上限："+
                            (f'{limit:,.2f}元' if limit is not None else '尚未設定')+
                            ('\n自助拿卡：開放' if result['enabled'] else '\n自助拿卡：關閉'))
            else:
                labels={'bind':'綁定完成；新帳預設0，重新綁定不會歸零',
                        'limit':f'欠款上限已設為{value}元','charge':'已增加欠款',
                        'payment':'已登記收款並減少欠款','adjust_credit':'已減少欠款',
                        'enable':'自助拿卡已開放','disable':'自助拿卡已關閉',
                        'approve':'已核准此群組拿卡權限','revoke':'已停止此群組拿卡權限'}
                hint = ('\n測試群僅可 /test100*數量；每張原價1000、售價905元。\n請設定 /欠款上限 金額、/自助核准 成員ID，最後 /開放自助。'
                        if action=='bind' and value==TEST_CUSTOMER else '')
                ns['reply'](event,f"✅ {labels[action]}\n{balance_text(result['debt'])}"+hint)
            return True
        if command=='/自助撤回':
            parts=text.split()
            if len(parts)!=2:
                raise CreditError('格式：/自助撤回 訂單編號；會先顯示文字確認指令，不會立即退帳或回收。')
            result=service.request_cancel(group_id,operator,event_id,parts[1],admin)
            reply_cancel_prompt(event,result,ns)
            return True
        if command=='/自助補發':
            parts=text.split()
            if len(parts)!=2:
                raise CreditError('格式：/自助補發 訂單編號（只補發同群組原訂單，不再扣款或取新卡）。')
            result=service.resend(group_id,operator,event_id,parts[1],admin)
            if result.get('duplicate'):
                ns['reply'](event,'這則補發指令已處理；不再次發送。')
            else:
                ns['reply_messages'](event,result['serial_chunks']+[f"原訂單補發：{result['order_no']}\n未新增欠款、未取新卡。"])
            return True
        # A bound group never falls through into legacy sales, customer aliases,
        # generic undo, balance commands, stock administration or other customers.
        parsed=ns['parse_quick_sale_message'](text)
        if not parsed or not parsed.get('ok'):
            raise CreditError('軒群請用 /MY1000*數量、/MY3000*數量、/MY5000*數量、/1萬*數量；測試群請用 /test100*數量。查帳用 /自助帳況。其他出庫、撤回及調帳格式不適用。')
        result=service.buy(group_id,operator,event_id,parsed['items'],admin)
        if result.get('duplicate'):
            ns['reply'](event,f"這則訊息已處理，不會重複出庫或扣帳。\n訂單：{result.get('order_no') or '請管理員查核'}\n如沒收到卡片，請管理員使用 /自助補發 原訂單編號。")
            return True
        return deliver_sale(event,ns,service,group_id,event_id,result)
    except CreditError as exc:
        ns['reply'](event,str(exc))
        return True


def reply_cancel_prompt(event,result,ns):
    token=result['token']
    text=(f"撤回整筆訂單：{result['order_no']}\n{result['quantity']}張／退帳{result['amount']:,.2f}元\n"
          '請管理員確認所有卡片未使用。機器人無法驗證兌換狀態。5分鐘內有效。\n'
          f'確認後恢復可售並退欠款，請複製整行：\n/確認未使用並回收 {token}\n'
          f'取消請輸入：\n/取消撤回 {token}')
    ns['reply'](event,text)


def handle_group_credit_postback(event,ns):
    data=str(getattr(event.postback,'data','') or '')
    if not data.startswith(('gc_cancel_confirm:','gc_cancel_decline:')):
        return False
    group_id=ns['get_context_id'](event)
    operator=ns['get_operator'](event)
    service=GroupCredit(ns['get_db'],ns['RealDictCursor'],ns['find_product'],
                        ns['create_order_no'],ns['split_text_chunks'])
    try:
        result=service.confirm_cancel(group_id,operator,data.split(':',1)[1],
                                      ns['is_admin'](operator),decline=data.startswith('gc_cancel_decline:'))
        if result.get('duplicate'):
            ns['reply'](event,'此訂單已撤回，不會再次退帳或回收。\n訂單：'+result['order_no'])
            return True
        if result.get('declined'):
            ns['reply'](event,'已取消撤回申請；訂單、欠款與庫存均未變更。')
            return True
        sales_ok,stock_ok=True,True
        if result.get('customer') != TEST_CUSTOMER:
            try:
                sales_ok=ns['mark_sales_rows_undone']([i['order_no'] for i in result['items']])
            except Exception:
                sales_ok=False
            try:
                from group_credit_sheet import clear_recovered_product_sales
                stock_ok=clear_recovered_product_sales(result['items'],ns)
            except Exception:
                stock_ok=False
        text=(f"↩️ 管理員已確認未使用並撤回\n訂單：{result['order_no']}\n"
              f"回收：{result['quantity']}張\n退款入帳：{result['amount']:,.2f}元\n"
              f"{balance_text(result['debt'])}")
        if not sales_ok or not stock_ok:
            text+='\n⚠️ 資料庫已撤回，試算表尚未同步完整；勿再次調帳，請管理員核對。'
        ns['reply'](event,text)
        return True
    except CreditError as exc:
        ns['reply'](event,str(exc))
        return True


def deliver_sale(event,ns,service,group_id,event_id,result):
    testing = result['customer'] == TEST_CUSTOMER
    sheet_ok,stock_ok=True,True
    if not testing:
        try:
            sheet_ok=ns['append_sales_rows'](result['customer'],result['items'])
        except Exception:
            sheet_ok=False
        try:
            stock_ok=ns['mark_product_sale'](result['items'],result['customer'])
        except Exception:
            stock_ok=False
    summary=(f"✅ 出庫並記帳完成\n客戶：{result['customer']}\n訂單：{result['order_no']}\n"
             f"本筆金額：{result['amount']:,.2f}元\n{balance_text(result['debt'])}\n上限：{result['credit_limit']:,.2f}元")
    if testing:
        summary='【測試】\n'+summary+'\n僅記錄測試帳，不寫入正式銷售試算表。'
    if result['debt']>=result['credit_limit']:
        summary+='\n⛔ 已達上限，請管理員登記收款或調帳後再拿卡。'
    if not sheet_ok or not stock_ok:
        summary+='\n⚠️ 試算表同步未完整；資料庫訂單及欠款已記錄，勿重複下單。'
    try:
        from receipt_cards import reply_receipt
        reply_receipt(event,result,summary,ns,sheet_ok,stock_ok)
    except Exception:
        try:
            service.delivery(group_id,event_id,'uncertain')
        except Exception:
            print('GROUP_CREDIT_DELIVERY_STATE_SAVE_FAILED')
        # A transport timeout does not prove delivery failed. Never release
        # these cards or automatically refund a possibly delivered order.
        raise
    else:
        try:
            service.delivery(group_id,event_id,'delivered')
        except Exception:
            print('GROUP_CREDIT_DELIVERY_STATE_SAVE_FAILED')
    return True
