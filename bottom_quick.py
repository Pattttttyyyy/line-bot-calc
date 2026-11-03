"""Bottom quick replies. No stock, accounting, or permission writes."""
PRODUCT_SHORTCUTS=(('MY1000','MY1000'),('MY3000','MY3000'),('MY5000','MY5000'),('MY10000','MY1萬'))

def context_role(group,user,admin,ns):
    # Optional UI lookup is read-only and never locks the financial account.
    if ns.get('DATABASE_URL') and ns.get('psycopg2'):
        conn=ns['psycopg2'].connect(ns['DATABASE_URL'],sslmode='require',connect_timeout=2,
            options='-c default_transaction_read_only=on -c statement_timeout=1500')
    else:conn=ns['get_db']()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT EXISTS(SELECT 1 FROM bot_ui_admin_groups WHERE group_id=%s),
                EXISTS(SELECT 1 FROM group_credit_accounts WHERE group_id=%s)
                AND (%s OR EXISTS(SELECT 1 FROM group_credit_members
                    WHERE group_id=%s AND line_user_id=%s AND active=TRUE))''',
                (group,group,admin,group,user))
            management,customer=cur.fetchone()
        if management:return 'admin' if admin else None
        return 'customer' if customer else None
    finally:
        try:conn.rollback()
        finally:conn.close()

def navigation_actions(event,ns):
    if ns.get('os') is not None and ns['os'].getenv('GROUP_SELF_SERVICE_ENABLED','0')!='1':return None
    group=ns['get_context_id'](event)
    if not str(group).startswith('group:'):return None
    user=ns['get_operator'](event)
    role=context_role(group,user,ns['is_admin'](user),ns)
    if role is None:return None
    if role=='admin':return [('入庫','bu:begin:stock'),('查庫存','bu:inventory'),('選單','bu:menu')]
    return [(label,'bu:quick:'+code) for label,code in PRODUCT_SHORTCUTS]+[('查庫存','bu:inventory'),('查欠款','bu:balance'),('選單','bu:menu')]

def merge_actions(primary,navigation):
    result=[];seen=set()
    for label,data in list(primary)+list(navigation):
        if data in seen:continue
        if len(label)>20 or len(data)>300:raise ValueError('Quick reply action too long')
        seen.add(data);result.append((label,data))
    if len(result)>13:raise ValueError('Quick reply exceeds 13 actions')
    return result

def decorate_reply(event,messages,ns,primary=()):
    """Text-only release: do not query UI roles or attach any quick replies."""
    return messages
