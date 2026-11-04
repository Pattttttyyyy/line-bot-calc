"""Group-bound UI. No API or database side effects on import."""
import json,re,secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from group_credit import GroupCredit,CreditError,group_context,validate_identity,quote,checked_debt,MY_FACES,customer_products

PRODUCTS=(('MY1000','MYCARD1000'),('MY3000','MYCARD3000'),('MY5000','MYCARD5000'),('MY1萬','MYCARD10000'),
 ('貝500','貝殼幣500'),('貝1000','貝殼幣1000'),('貝1490','貝殼幣1490'),('貝2990','貝殼幣2990'),('大卡','藍鑽大卡'),('小卡','藍鑽小卡'))
LABELS=dict(PRODUCTS)
ACTIVE=('product','quantity','data','confirm')

def parse_intake(text,code,ns):
    lines=text.splitlines()
    note=''
    if lines and lines[0].strip().startswith('#'):
        note=lines.pop(0).strip()[1:].strip()
        if not note:raise CreditError('供貨備註不可空白。')
    if lines and lines[0].strip().startswith('/入庫'):
        lines,n=ns['extract_stock_in_note'](lines)
        note=n or note
        explicit=lines.pop(0).strip()[len('/入庫'):].strip()
        if explicit and ns['resolve_quick_product_alias'](explicit)!=code:
            raise CreditError('貼上的入庫指令商品與已選商品不同，尚未入庫。')
    if len(note)>200:raise CreditError('供貨備註最多200字。')
    raw=['入庫 '+code]+lines
    try:
        supplier=ns['parse_supplier_stock_in'](raw,code)
        if supplier:
            serials=supplier['serials']
        elif code in MY_FACES:
            tokens=re.split(r'[\s,]+','\n'.join(lines).strip())
            if len(tokens)%2 or not tokens:raise CreditError('MY卡號與密碼未成對，尚未入庫。')
            serials=[];seen=set()
            for i in range(0,len(tokens),2):
                card,pin=tokens[i:i+2]
                if not re.fullmatch(r'MC[A-Z0-9]{10}',card,re.I) or not re.fullmatch(r'[A-Z0-9]{12}',pin,re.I):
                    raise CreditError('MY卡號或密碼格式不正確，請貼完整資料。')
                if card in seen:raise CreditError('本次有重複卡號，尚未入庫。')
                seen.add(card);serials.append(card+' '+pin)
        else:
            serials=[line.strip() for line in lines if line.strip()]
            pattern=r'[0-9]{16}' if code.startswith('貝') else r'[A-Z0-9]{20}'
            if any(not re.fullmatch(pattern,line,re.I) for line in serials):raise CreditError('序號格式與所選商品不符，尚未入庫。')
    except ValueError as exc:raise CreditError(str(exc)) from exc
    if not serials or len(serials)>1000:raise CreditError('請貼上完整入庫資料，每次最多1000張。')
    if len(set(serials))!=len(serials):raise CreditError('本次資料有重複卡片，尚未入庫。')
    return dict(serials=serials,note=note,segment=supplier.get('segment') if supplier else None)

class UIStore:
    def __init__(self,ns):
        self.ns=ns
        self.credit=GroupCredit(ns['get_db'],ns['RealDictCursor'],ns['find_product'],ns['create_order_no'],ns['split_text_chunks'])
    def admin_group(self,group):
        if not str(group).startswith('group:'):return False
        conn=self.ns['get_db']()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT 1 FROM bot_ui_admin_groups WHERE group_id=%s',(group,))
                return cur.fetchone() is not None
        finally:conn.close()
    def bind_admin(self,group,user,admin):
        group_context(group)
        validate_identity(user,'bind-admin')
        if not admin:raise CreditError('只有既有管理員能設定管理群組。')
        conn=self.ns['get_db']()
        try:
            with conn.cursor() as cur:
                # Same advisory key is used by the customer bind path.
                cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(group,))
                cur.execute('SELECT 1 FROM group_credit_accounts WHERE group_id=%s',(group,))
                if cur.fetchone():raise CreditError('這個群組已綁定客人帳戶，請用另一個群組作管理群組。')
                cur.execute('INSERT INTO bot_ui_admin_groups(group_id,configured_by) VALUES(%s,%s) ON CONFLICT DO NOTHING',(group,user))
            conn.commit()
        except Exception:conn.rollback();raise
        finally:conn.close()
    def authorize(self,group,user,admin,kind=None):
        group_context(group);validate_identity(user,'ui')
        management=self.admin_group(group)
        if management:
            if not admin:raise CreditError('⛔ 此管理群組的機器人功能僅供既有管理員使用。')
            if kind=='sale':raise CreditError('此為管理群組，請在客人自助群組拿卡。')
            return True,None
        if kind=='stock':raise CreditError('此群組未設定為管理群組，不能使用按鈕入庫。')
        account=self.credit.manage(group,user,'status','status',admin=admin)
        return False,account
    def inventory(self,group,user,admin):
        management,account=self.authorize(group,user,admin)
        codes=list(LABELS if management else customer_products(account['customer']))
        conn=self.ns['get_db']()
        try:
            with conn.cursor(cursor_factory=self.ns['RealDictCursor']) as cur:
                cur.execute('''SELECT p.code,count(s.id) AS stock FROM products p
                    LEFT JOIN serials s ON s.product_id=p.id AND s.status='available'
                    WHERE p.active=TRUE AND p.code=ANY(%s) GROUP BY p.id,p.code''',(codes,))
                rows={r['code']:int(r['stock']) for r in cur.fetchall()}
            return [(code,rows[code]) for code in codes if code in rows]
        finally:conn.close()
    def _purge(self,cur):
        # Pending card credentials are temporary, never kept in audit receipts.
        cur.execute("UPDATE bot_ui_sessions SET state='cancelled',payload='{}'::jsonb WHERE expires_at<clock_timestamp() AND state=ANY(%s)",(list(ACTIVE),))
    def begin(self,group,user,event_id,kind,admin):
        management,account=self.authorize(group,user,admin,kind)
        if kind=='sale' and (not account['enabled'] or account['credit_limit'] is None):raise CreditError('此群組尚未開放自助拿卡或未設定上限。')
        validate_identity(user,event_id)
        conn=self.ns['get_db']()
        try:
            with conn.cursor(cursor_factory=self.ns['RealDictCursor']) as cur:
                cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(group+user,))
                self._purge(cur)
                cur.execute('SELECT * FROM bot_ui_sessions WHERE group_id=%s AND user_id=%s AND start_id=%s',(group,user,str(event_id)))
                previous=cur.fetchone()
                if previous:
                    if previous['state']!='product':raise CreditError('此選單已處理，請重新輸入 /選單。')
                    return dict(previous)
                cur.execute("UPDATE bot_ui_sessions SET state='cancelled',payload='{}'::jsonb WHERE group_id=%s AND user_id=%s AND state=ANY(%s)",(group,user,list(ACTIVE)))
                token=secrets.token_hex(16)
                cur.execute("INSERT INTO bot_ui_sessions(token,group_id,user_id,start_id,kind,state) VALUES(%s,%s,%s,%s,%s,'product') RETURNING *",(token,group,user,str(event_id),kind))
                result=dict(cur.fetchone())
            conn.commit();return result
        except Exception:conn.rollback();raise
        finally:conn.close()
    def _session(self,cur,group,user,token):
        cur.execute('SELECT *,expires_at>clock_timestamp() AS valid FROM bot_ui_sessions WHERE token=%s AND group_id=%s AND user_id=%s FOR UPDATE',(token,group,user))
        s=cur.fetchone()
        if not s:raise CreditError('這個按鈕不屬於你或此群組，請使用自己的 /選單。')
        if not s['valid'] and s['state']!='done':raise CreditError('操作已逾10分鐘，請重新輸入 /選單。')
        return s
    def change(self,group,user,token,action,value,admin):
        self.authorize(group,user,admin)
        conn=self.ns['get_db']()
        try:
            with conn.cursor(cursor_factory=self.ns['RealDictCursor']) as cur:
                s=self._session(cur,group,user,token)
                if s['kind']=='stock':self.authorize(group,user,admin,'stock')
                if action=='cancel':
                    if s['state']=='done':raise CreditError('此操作已完成，不能用取消按鈕撤銷。')
                    cur.execute("UPDATE bot_ui_sessions SET state='cancelled',payload='{}'::jsonb WHERE token=%s",(token,))
                    result=dict(s,state='cancelled')
                elif action=='product':
                    if s['state'] not in ('product','quantity','confirm','data'):raise CreditError('此操作已失效。')
                    if value not in (LABELS if s['kind']=='stock' else MY_FACES):raise CreditError('商品不適用此操作。')
                    product=self.ns['find_product'](conn,value)
                    if not product:raise CreditError('此商品未啟用。')
                    state='data' if s['kind']=='stock' else 'quantity'
                    revision=s['revision']+1
                    cur.execute("UPDATE bot_ui_sessions SET product_code=%s,quantity=NULL,payload='{}'::jsonb,state=%s,revision=%s WHERE token=%s",(value,state,revision,token))
                    result=dict(s,product_code=value,state=state,quantity=None,payload={},revision=revision)
                elif action=='qty':
                    if s['kind']!='sale' or s['state'] not in ('quantity','confirm'):raise CreditError('請先重新選擇商品。')
                    if not str(value).isdigit() or not 1<=int(value)<=500:raise CreditError('數量須為1～500的整數。')
                    quantity=int(value);unit,amount=quote(s['product_code'],quantity)
                    payload=dict(unit=str(unit),amount=str(amount))
                    revision=s['revision']+1
                    cur.execute("UPDATE bot_ui_sessions SET quantity=%s,payload=%s::jsonb,state='confirm',revision=%s WHERE token=%s",(quantity,json.dumps(payload),revision,token))
                    result=dict(s,quantity=quantity,payload=payload,state='confirm',revision=revision)
                else:raise CreditError('無效操作。')
            conn.commit();return result
        except Exception:conn.rollback();raise
        finally:conn.close()
    def pending(self,group,user,all_states=False):
        conn=self.ns['get_db']()
        try:
            with conn.cursor(cursor_factory=self.ns['RealDictCursor']) as cur:
                self._purge(cur)
                cur.execute("SELECT * FROM bot_ui_sessions WHERE group_id=%s AND user_id=%s AND state=ANY(%s) AND expires_at>clock_timestamp()",(group,user,list(ACTIVE) if all_states else ['data','quantity']))
                row=cur.fetchone()
            conn.commit();return dict(row) if row else None
        except Exception:conn.rollback();raise
        finally:conn.close()
    def paste(self,group,user,token,text,admin):
        self.authorize(group,user,admin,'stock')
        conn=self.ns['get_db']()
        try:
            with conn.cursor(cursor_factory=self.ns['RealDictCursor']) as cur:
                s=self._session(cur,group,user,token)
                if s['kind']!='stock' or s['state']!='data':raise CreditError('請先選入庫商品，再貼資料。')
                payload=parse_intake(text,s['product_code'],self.ns)
                revision=s['revision']+1
                cur.execute("UPDATE bot_ui_sessions SET quantity=%s,payload=%s::jsonb,state='confirm',revision=%s WHERE token=%s",(len(payload['serials']),json.dumps(payload),revision,token))
                result=dict(s,state='confirm',quantity=len(payload['serials']),payload=payload,revision=revision)
            conn.commit();return result
        except Exception:conn.rollback();raise
        finally:conn.close()
    def confirm(self,group,user,token,admin,expected_revision=None):
        self.authorize(group,user,admin)
        conn=self.ns['get_db']()
        try:
            with conn.cursor(cursor_factory=self.ns['RealDictCursor']) as cur:
                s=self._session(cur,group,user,token)
                if s['state']=='done':
                    conn.rollback();return dict(duplicate=True,**s['receipt'])
                if expected_revision is not None and s['revision']!=expected_revision:
                    raise CreditError('商品、數量或資料已重新選擇，這張舊確認畫面已失效；請使用最新確認按鈕。')
                if s['state']!='confirm':raise CreditError('請先選擇商品與數量，或重新貼入庫資料。')
                if s['kind']=='sale':
                    _,amount=quote(s['product_code'],s['quantity'])
                    if str(amount)!=s['payload']['amount']:raise CreditError('售價已變更，請重新選擇並確認。')
                    result=self.credit.buy(group,user,'ui-sale:'+token,[dict(product_text=s['product_code'],quantity=s['quantity'])],admin,transaction=conn)
                    receipt=dict(kind='sale',order_no=result['order_no'])
                else:
                    self.authorize(group,user,admin,'stock')
                    result=self.ns['stock_in_serials'](user,group,s['product_code'],s['payload']['serials'],note=s['payload'].get('note',''),transaction=conn)
                    if not result['ok']:raise CreditError('商品未啟用，未入庫。')
                    result.update(note=s['payload'].get('note',''),segment=s['payload'].get('segment'))
                    receipt=dict(kind='stock',batch_no=result['batch_no'],added=len(result['added']),duplicates=len(result['duplicates']),stock=result['stock'])
                cur.execute("UPDATE bot_ui_sessions SET state='done',payload='{}'::jsonb,receipt=%s::jsonb WHERE token=%s",(json.dumps(receipt),token))
            conn.commit();return dict(kind=s['kind'],result=result,duplicate=result.get('duplicate',False),**{k:v for k,v in receipt.items() if k!='kind'})
        except Exception:conn.rollback();raise
        finally:conn.close()

def send_buttons(event,ns,text,actions,carousel=False):
    from linebot.v3.messaging import ApiClient,MessagingApi,ReplyMessageRequest,TextMessage
    from bottom_quick import decorate_reply
    messages=decorate_reply(event,[TextMessage(text=text[:4900])],ns,primary=actions)
    with ApiClient(ns['configuration']) as client:
        MessagingApi(client).reply_message(ReplyMessageRequest(reply_token=event.reply_token,messages=messages))

def show_session(event,ns,store,s):
    token=s['token'];code=s.get('product_code');label=LABELS.get(code,code)
    if s['state']=='product':
        if s['kind']=='sale':
            ns['reply'](event,'請點下方 MY1000、MY3000、MY5000 或 MY10000 快捷，再選擇數量。')
            return
        codes=LABELS if s['kind']=='stock' else MY_FACES
        send_buttons(event,ns,'請選擇入庫商品' if s['kind']=='stock' else '請選擇要拿的商品',[(LABELS[c],f'bu:product:{token}:{c}') for c in codes],carousel=s['kind']=='stock')
    elif s['state']=='quantity':
        send_buttons(event,ns,f'{label}\n選擇張數，或直接輸入1～500的整數。',[(str(n)+'張',f'bu:qty:{token}:{n}') for n in (1,5,10)]+[('取消',f'bu:cancel:{token}')])
    elif s['state']=='data':
        ns['reply'](event,f'已選擇：{label}\n請直接貼完整廠商文字或序號，不用刪空白或改品名。可在第一行加 #供貨者。\n選錯商品請重開 /入庫選單；取消用 /取消操作。10分鐘有效。')
    elif s['state']=='confirm':
        if s['kind']=='sale':
            _,account=store.authorize(s['group_id'],s['user_id'],ns['is_admin'](s['user_id']))
            _,amount=quote(code,s['quantity'])
            after=checked_debt(account['debt'],account['credit_limit'],amount,account['enabled'])
            stocks=dict(store.inventory(s['group_id'],s['user_id'],ns['is_admin'](s['user_id'])))
            if stocks.get(code,0)<s['quantity']:raise CreditError('目前庫存不足，尚未出庫。請重新選擇數量。')
            text=f"{label} × {s['quantity']}張\n單價：{s['payload']['unit']}元\n總額：{amount:,.2f}元\n出貨後欠款：{after:,.2f}元\n按確認才會發卡。"
            caption='確認拿卡'
        else:
            text=f"入庫：{label}\n本段：{s['quantity']}張\n供貨備註：{s['payload'].get('note') or '未填'}\n按確認才會入庫；重複卡片會跳過。"
            caption='確認入庫'
        send_buttons(event,ns,text,[(caption,f"bu:confirm:{token}:{s['revision']}"),('重新選擇',f'bu:restart:{token}'),('取消',f'bu:cancel:{token}')])
    elif s['state']=='cancelled':ns['reply'](event,'已取消本次操作，未新增出入庫或帳款。')

def show_menu(event,ns,store,group,user,admin):
    management,_=store.authorize(group,user,admin)
    if management:
        eid=getattr(event,'webhook_event_id',None) or getattr(getattr(event,'message',None),'id',None)
        if not eid:raise CreditError('缺少訊息識別碼，請重開 /選單。')
        show_session(event,ns,store,store.begin(group,user,eid,'stock',admin))
    else:
        ns['reply'](event,'請點下方商品快捷，再選數量並確認拿卡。也可查庫存或查欠款。')

def deliver_result(event,ns,store,group,user,result):
    if result.get('duplicate'):
        ns['reply'](event,'此操作已完成，不會重複出入庫或扣帳。\n'+('訂單：'+result['order_no']+'\n若未收到卡片，請管理員用 /自助補發 訂單編號。' if result['kind']=='sale' else '入庫編號：'+result['batch_no']))
    elif result['kind']=='sale':
        from group_credit_handler import deliver_sale
        deliver_sale(event,ns,store.credit,group,'ui-sale:'+result.get('token',''),result['result'])
    else:
        r=result['result']
        try:ok=ns['append_product_stock_in'](r['product']['code'],r['added'],batch_no=r['batch_no'],note=r['note'])
        except Exception:ok=False
        ns['reply'](event,f"✅ 入庫完成\n商品：{LABELS[r['product']['code']]}\n入庫編號：{r['batch_no']}\n新增：{len(r['added'])}張\n重複：{len(r['duplicates'])}張\n目前庫存：{r['stock']}張"
            + ('\n供貨備註：'+r['note'] if r['note'] else '')
            + (f"\n廠商第{r['segment'][0]}/{r['segment'][1]}段，本次只處理本段，請逐段貼上。" if r.get('segment') else '')
            + ('\n⚠️ 商品分頁尚未同步完整，勿重複入庫。' if not ok else ''))

def handle_ui_text(event,ns):
    group=ns['get_context_id'](event)
    if not str(group).startswith('group:'):return False
    text=event.message.text.strip();user=ns['get_operator'](event);admin=ns['is_admin'](user)
    store=UIStore(ns)
    known=text in ('/設定管理群組','/選單','/入庫選單','/自助庫存','/取消操作')
    try:
        if text=='/設定管理群組':
            store.bind_admin(group,user,admin);show_menu(event,ns,store,group,user,admin);return True
        if text=='/我的ID':return False
        management=store.admin_group(group)
        if management and text.startswith('/') and not admin:
            raise CreditError('⛔ 此管理群組的機器人功能僅供既有管理員使用。')
        if text=='/選單':show_menu(event,ns,store,group,user,admin);return True
        if text=='/入庫選單' or (management and text=='/入庫'):
            s=store.begin(group,user,event.message.id,'stock',admin);show_session(event,ns,store,s);return True
        if text=='/自助庫存' or (text in ('/庫存','/查庫存') and (management or store.credit.registered(group))):
            inventory_reply(event,ns,store,group,user,admin);return True
        if not management and not text.startswith('/') and (not text.isdigit() or not store.credit.registered(group)):
            return False
        if known or not text.startswith('/') or text.startswith('/入庫'):
            pending=store.pending(group,user,all_states=text=='/取消操作')
            if text=='/取消操作':
                if pending:store.change(group,user,pending['token'],'cancel',None,admin)
                ns['reply'](event,'已取消待輸入操作。');return True
            if not pending:return False
            if pending['state']=='data':s=store.paste(group,user,pending['token'],event.message.text,admin)
            elif text.isdigit():s=store.change(group,user,pending['token'],'qty',text,admin)
            else:return False
            show_session(event,ns,store,s);return True
        return False
    except CreditError as exc:ns['reply'](event,str(exc));return True

def inventory_reply(event,ns,store,group,user,admin):
    rows=store.inventory(group,user,admin)
    time=datetime.now(ZoneInfo('Asia/Taipei')).strftime('%m/%d %H:%M:%S')
    ns['reply'](event,'📦 即時可售庫存\n'+'\n'.join(f'{LABELS.get(c,c)}：{n}張' for c,n in rows)+'\n查詢時間：'+time+'\n確認拿卡時會再檢查庫存。')

def handle_ui_postback(event,ns):
    data=str(getattr(event.postback,'data','') or '')
    if not data.startswith('bu:'):return False
    group=ns['get_context_id'](event);user=ns['get_operator'](event);admin=ns['is_admin'](user);store=UIStore(ns)
    try:
        parts=data.split(':');action=parts[1]
        if action=='menu':show_menu(event,ns,store,group,user,admin)
        elif action=='quick':
            if len(parts)!=3 or parts[2] not in MY_FACES:raise CreditError('此商品未開放自助拿卡。')
            eid=getattr(event,'webhook_event_id',None)
            if not eid:raise CreditError('缺少訊息識別碼，請重開選單。')
            session=store.begin(group,user,eid,'sale',admin)
            session=store.change(group,user,session['token'],'product',parts[2],admin)
            show_session(event,ns,store,session)
        elif action=='inventory':inventory_reply(event,ns,store,group,user,admin)
        elif action=='balance':
            _,a=store.authorize(group,user,admin)
            if a is None:raise CreditError('管理群組沒有客人共用欠款。')
            limit=a['credit_limit']
            ns['reply'](event,f"群組欠款：{a['debt']:,.2f}元\n"+(f"上限：{limit:,.2f}元\n剩餘額度：{max(0,limit-a['debt']):,.2f}元" if limit is not None else '上限尚未設定'))
        elif action=='begin':
            if len(parts)!=3 or parts[2] not in ('sale','stock'):raise CreditError('無效選單。')
            eid=getattr(event,'webhook_event_id',None)
            if not eid:raise CreditError('缺少訊息識別碼，請重開選單。')
            show_session(event,ns,store,store.begin(group,user,eid,parts[2],admin))
        else:
            if len(parts)<3 or not re.fullmatch('[0-9a-f]{32}',parts[2]):raise CreditError('按鈕無效。')
            token=parts[2]
            if action=='confirm':
                if len(parts)!=4 or not parts[3].isdigit():raise CreditError('確認畫面已失效，請重開 /選單。')
                result=store.confirm(group,user,token,admin,expected_revision=int(parts[3]));result['token']=token
                deliver_result(event,ns,store,group,user,result)
            elif action=='restart':
                # Validate ownership first; no one can cancel another member's pending request.
                old=store.change(group,user,token,'cancel',None,admin)
                eid=getattr(event,'webhook_event_id',None)
                if not eid:raise CreditError('請重開 /選單。')
                show_session(event,ns,store,store.begin(group,user,eid,old['kind'],admin))
            elif action in ('product','qty','cancel'):
                show_session(event,ns,store,store.change(group,user,token,action,parts[3] if len(parts)==4 else None,admin))
            else:raise CreditError('無效按鈕。')
        return True
    except CreditError as exc:ns['reply'](event,str(exc));return True
