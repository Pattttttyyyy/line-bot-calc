"""Optional LINE adapter; dispatch only when the global feature flag is enabled."""
import re
from group_credit import CreditError, GroupCredit, TEST_CUSTOMER, balance_text

COMMANDS = {
    '/自助綁定':'bind', '/欠款上限':'limit', '/增加欠款':'charge',
    '/登記收款':'payment', '/減少欠款':'adjust_credit',
    '/開放自助':'enable', '/關閉自助':'disable',
    '/自助核准':'approve', '/自助停權':'revoke', '/自助帳況':'status',
}


def mentioned_buyer(message, command):
    """Read only LINE-provided structured mention; never resolve display names."""
    def field(obj,name,default=None):
        if isinstance(obj,dict):
            return obj.get(name,obj.get({'user_id':'userId','is_self':'isSelf'}.get(name,name),default))
        actual=getattr(obj,'actual_instance',None)
        return getattr(actual if actual is not None else obj,name,default)
    mention=field(message,'mention')
    mentions=field(mention,'mentionees',[]) or []
    if len(mentions)!=1:
        raise CreditError('請一次標記一位成員：'+command+' @某某某；不能標記全體。')
    target=mentions[0]
    if field(target,'type')!='user' or field(target,'is_self',False):
        raise CreditError('請標記要拿卡的成員，不能選全體或機器人。')
    user=field(target,'user_id','')
    if not isinstance(user,str) or not re.fullmatch('U[0-9a-fA-F]{32}',user):
        raise CreditError('LINE未提供這位成員的ID；請對方先加機器人好友或傳 /我的ID，再由管理員核准。')
    raw=field(message,'text','').encode('utf-16-le')
    index,length=field(target,'index'),field(target,'length')
    if type(index) is not int or type(length) is not int or index<0 or length<=0 or 2*(index+length)>len(raw):
        raise CreditError('標記資料不完整，請重新選取成員。')
    try:
        before=raw[:2*index].decode('utf-16-le').strip()
        label=raw[2*index:2*(index+length)].decode('utf-16-le')
        after=raw[2*(index+length):].decode('utf-16-le').strip()
    except UnicodeError:
        raise CreditError('標記位置無效，請重新選取成員。') from None
    if before!=command or after or not label.startswith('@'):
        raise CreditError('格式：'+command+' @某某某；請從LINE名單選取成員，不要另加文字。')
    return user


def normalize_group_sale(text):
    """Normalize only complete product/quantity lines, never IDs or card data."""
    products = {code.casefold(): code for code in
                ('MY1000', 'MY3000', 'MY5000', 'MY1萬', 'test100', '1萬')}
    for canonical, aliases in {
        'MY1000': ('1000', '1千', '一千'),
        'MY3000': ('3000', '3千', '三千'),
        'MY5000': ('5000', '5千', '五千'),
        'MY1萬': ('10000', '1萬', '一萬', 'MY10000'),
    }.items():
        products.update({alias.casefold(): canonical for alias in aliases})
    lines = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        match = re.fullmatch(r'/([^\s*xX]+)(?:\s*[*xX]\s*|\s+)([0-9]+)', raw.strip())
        if not match or match[1].casefold() not in products:
            return text
        lines.append('/' + products[match[1].casefold()] + '*' + match[2])
    return '\n'.join(lines) if lines else text


def handle_group_credit(event, ns):
    group_id = ns['get_context_id'](event)
    text = event.message.text.strip()
    command = text.split(maxsplit=1)[0]
    if not group_id.startswith('group:') or command == '/我的ID':
        return False
    service = GroupCredit(ns['get_db'],ns['RealDictCursor'],ns['find_product'],
                          ns['create_order_no'],ns['split_text_chunks'])
    known = command in COMMANDS or command in ('/自助補發','/自助撤回','/同步發卡紀錄')
    if not known and not service.registered(group_id):
        return False
    operator = ns['get_operator'](event)
    admin = ns['is_admin'](operator)
    event_id = str(getattr(event.message,'id','') or '')
    try:
        if command=='/同步發卡紀錄':
            if not admin:
                raise CreditError('⛔ 只有管理員可以補同步發卡紀錄。')
            if text!=command:
                raise CreditError('請直接輸入 /同步發卡紀錄。')
            from group_sheet_queue import sync_pending_sales
            outcome=sync_pending_sales(service,ns,group_id,limit=10)
            ns['reply'](event,f"已補同步 {outcome['completed']} 筆。"+('仍有同步失敗，待同步狀態已保留；不會重複發卡或扣帳。' if outcome['failed'] else '每次最多10筆；可再次執行檢查剩餘。'))
            return True
        if command in COMMANDS:
            action = COMMANDS[command]
            parts = text.split(maxsplit=2)
            if action in ('approve','revoke') and getattr(event.message,'mention',None):
                if not admin:
                    raise CreditError('⛔ 只有管理員可以核准或停止本群拿卡權限。')
                value,reason=mentioned_buyer(event.message,command),''
            elif action in ('enable','disable','status'):
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
        parsed=ns['parse_quick_sale_message'](normalize_group_sale(text))
        if not parsed or not parsed.get('ok'):
            raise CreditError('軒群請用 /MY1000*數量、/MY3000*數量、/MY5000*數量、/1萬*數量；測試群請用 /test100*數量。商品英文不分大小寫，數量可用 *、x 或空白分隔。查帳用 /自助帳況，撤回用 /撤回。')
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
    text=(f"發卡撤回申請（尚未執行）\n訂單：{result['order_no']}\n{result['quantity']}張／退帳{result['amount']:,.2f}元\n"
          '請管理員確認所有卡片未使用。機器人無法驗證兌換狀態。5分鐘內有效。\n'
          f'確認後恢復可售並退款入帳（多餘轉預付款），請複製整行：\n/確認未使用並回收 {token}\n'
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
        return reply_cancel_result(event,result,ns)
    except CreditError as exc:
        ns['reply'](event,str(exc))
        return True


def reply_cancel_result(event,result,ns):
    if result.get('duplicate'):
        ns['reply'](event,'此訂單已撤回，不會再次退帳或回收。\n原訂單：'+result['order_no']+'\n撤回編號：'+str(result.get('record_id','請查記帳紀錄')))
        return True
    if result.get('declined'):
        ns['reply'](event,'已取消撤回申請；訂單、欠款與庫存均未變更。')
        return True
    sales_ok,stock_ok=True,True
    if result.get('customer') == TEST_CUSTOMER:
        try:
            sales_ok=ns['mark_sales_rows_undone_in_sheet']([i['order_no'] for i in result['items']],ns['get_sales_sheet'](TEST_CUSTOMER))
        except Exception:
            sales_ok=False
    else:
        try:
            sales_ok=ns['mark_sales_rows_undone']([i['order_no'] for i in result['items']])
        except Exception:
            sales_ok=False
        try:
            from group_credit_sheet import clear_recovered_product_sales
            stock_ok=clear_recovered_product_sales(result['items'],ns)
        except Exception:
            stock_ok=False
    text=(f"↩️ 已撤回發卡\n撤回編號：{result.get('record_id','請查記帳紀錄')}\n原訂單：{result['order_no']}\n"
          f"回收：{result['quantity']}張\n退款入帳：{result['amount']:,.2f}元\n"
          f"{balance_text(result['debt'])}\n原卡密訊息仍保留；請管理員核對卡片未使用。")
    if not sales_ok or not stock_ok:
        text+='\n⚠️ 資料庫已撤回，試算表尚未同步完整；勿再次調帳，請管理員核對。'
    ns['reply'](event,text)
    return True


def deliver_sale(event,ns,service,group_id,event_id,result):
    testing = result['customer'] == TEST_CUSTOMER
    pending_result=dict(result,sheet_pending=True)
    summary=(f"✅ 出庫並記帳完成\n客戶：{result['customer']}\n訂單：{result['order_no']}\n"
             f"本筆金額：{result['amount']:,.2f}元\n{balance_text(result['debt'])}\n上限：{result['credit_limit']:,.2f}元")
    if testing:
        summary='【測試】\n'+summary+'\n已記錄測試帳；「測試銷售紀錄」接續同步，不計入正式銷售。'
    if result['debt']>=result['credit_limit']:
        summary+='\n⛔ 已達上限，請管理員登記收款或調帳後再拿卡。'
    summary+='\n試算表接續同步；資料庫已記帳，勿重複下單。'
    try:
        from receipt_cards import reply_receipt
        reply_receipt(event,pending_result,summary,ns)
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
    finally:
        try:
            from group_sheet_queue import sync_pending_sales
            sync_pending_sales(service,ns,group_id,limit=1)
        except Exception:
            print('GROUP_SHEET_SYNC_PENDING')
    return True
