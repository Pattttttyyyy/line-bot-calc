"""PostgreSQL payment journal draft. No live app imports, credentials or HTTP.

Only the isolated test harness creates SCHEMA_SQL. Production migration,
shared stock-sale credit holds and authenticated provider integration are NOT wired.
Lock order: wallet advisory transaction lock -> group account -> payment row.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
import json
import re
import uuid
from .flow import Refused, State

ACTIVE=('checking','sending','unknown','confirmed_pending_ledger')
SCHEMA_SQL='''
CREATE SCHEMA IF NOT EXISTS mycard_private;
REVOKE ALL ON SCHEMA mycard_private FROM PUBLIC;
CREATE TABLE IF NOT EXISTS mycard_private.payments(
    reference TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES group_credit_accounts(group_id),
    message_id TEXT NOT NULL,
    image_id TEXT NOT NULL,
    wallet_alias TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    provider_order TEXT NOT NULL,
    intent JSONB NOT NULL,
    amount NUMERIC(18,2) NOT NULL CHECK(amount>0),
    state TEXT NOT NULL CHECK(state IN ('checking','rejected_before_payment','sending','unknown','confirmed_pending_ledger','booked')),
    provider_ref TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(group_id,message_id),
    CHECK(state NOT IN ('confirmed_pending_ledger','booked') OR provider_ref IS NOT NULL)
);
ALTER TABLE mycard_private.payments ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON mycard_private.payments FROM PUBLIC;
CREATE UNIQUE INDEX IF NOT EXISTS mycard_one_active_wallet
 ON mycard_private.payments(wallet_alias) WHERE state IN ('checking','sending','unknown','confirmed_pending_ledger');
CREATE UNIQUE INDEX IF NOT EXISTS mycard_unique_qr
 ON mycard_private.payments(fingerprint) WHERE state<>'rejected_before_payment';
CREATE UNIQUE INDEX IF NOT EXISTS mycard_unique_order
 ON mycard_private.payments(wallet_alias,provider_order) WHERE state<>'rejected_before_payment';
CREATE UNIQUE INDEX IF NOT EXISTS mycard_unique_image
 ON mycard_private.payments(group_id,image_id) WHERE state<>'rejected_before_payment';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
  REVOKE ALL ON SCHEMA mycard_private FROM anon;
  REVOKE ALL ON mycard_private.payments FROM anon;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
  REVOKE ALL ON SCHEMA mycard_private FROM authenticated;
  REVOKE ALL ON mycard_private.payments FROM authenticated;
 END IF;
END $$;
CREATE INDEX IF NOT EXISTS mycard_group_holds
 ON mycard_private.payments(group_id,state) INCLUDE(amount);
'''


@dataclass(frozen=True)
class Policy:
    wallet: str
    customer: str
    rate: Decimal
    max_points: int
    max_face: Decimal


def payload(intent):
    payment=intent.expected_payment
    if payment is None:raise Refused('缺少已核對的交易資料。')
    if (not all(isinstance(value,str) and 0<len(value)<=256 for value in
            (intent.group,intent.operator,intent.message,intent.image_message,intent.wallet_alias,intent.customer,payment.order_ref,payment.payee_ref))
        or payment.currency!='TWD' or not isinstance(payment.face_amount,Decimal)
        or not payment.face_amount.is_finite() or not Decimal(0)<payment.face_amount<=Decimal('9999999999999999.99')
        or payment.face_amount!=payment.face_amount.quantize(Decimal('.01'))):
        raise Refused('交易金額、幣別或識別不正確。')
    number=lambda value:format(value.normalize(),'f')
    return dict(group=intent.group,operator=intent.operator,message=intent.message,
        image_message=intent.image_message,wallet_alias=intent.wallet_alias,
        customer=intent.customer,points=intent.points,rate=number(intent.rate),
        payment=dict(order=payment.order_ref,payee=payment.payee_ref,
                     amount=number(payment.face_amount),currency=payment.currency))


def reserved_amount(cur,group):
    """Caller must hold group_credit_accounts row lock; no extra connection."""
    cur.execute('SELECT COALESCE(sum(amount),0) AS reserved FROM mycard_private.payments WHERE group_id=%s AND state=ANY(%s)',(group,list(ACTIVE)))
    return cur.fetchone()['reserved']


class Journal:
    def __init__(self,get_db,dict_cursor,policies=None):
        self.get_db=get_db;self.dict_cursor=dict_cursor
        self.policies=dict(policies or {})  # No groups authorized by default.

    @contextmanager
    def transaction(self):
        conn=self.get_db()
        try:
            with conn.cursor(cursor_factory=self.dict_cursor) as cur:
                cur.execute("SET LOCAL lock_timeout='5s'")
                yield cur
            conn.commit()
        except Exception:
            conn.rollback();raise
        finally:conn.close()

    def lock(self,cur,wallet,group):
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('mycard-wallet:'+wallet,))
        cur.execute('SELECT * FROM group_credit_accounts WHERE group_id=%s FOR UPDATE',(group,))
        account=cur.fetchone()
        if account is None:raise Refused('群組帳戶不存在。')
        return account

    def lock_reference(self,cur,reference):
        cur.execute('SELECT wallet_alias,group_id FROM mycard_private.payments WHERE reference=%s',(reference,))
        identity=cur.fetchone()
        if identity is None:raise Refused('找不到扣點紀錄。')
        account=self.lock(cur,identity['wallet_alias'],identity['group_id'])
        cur.execute('SELECT * FROM mycard_private.payments WHERE reference=%s FOR UPDATE',(reference,))
        return account,cur.fetchone()

    def policy(self,intent):
        policy=self.policies.get(intent.group)
        if policy is None or policy.wallet!=intent.wallet_alias or policy.customer!=intent.customer:
            raise Refused('此群尚未開放雲端扣點帳戶。')
        if (not isinstance(intent.rate,Decimal) or not intent.rate.is_finite()
            or intent.rate!=policy.rate or not 0<intent.rate<=1
            or type(intent.points) is not int or not 1<=intent.points<=policy.max_points):
            raise Refused('點數或扣點售價不符合群組設定。')
        face=intent.expected_payment.face_amount
        if not isinstance(face,Decimal) or not face.is_finite() or not 0<face<=policy.max_face:
            raise Refused('交易金額超過設定上限。')
        return policy

    def credit(self,cur,account,extra=Decimal(0)):
        if account['enabled'] is not True or account['credit_limit'] is None:
            raise Refused('群組未開放或尚未設定額度。')
        if account['debt']+reserved_amount(cur,account['group_id'])+extra>account['credit_limit']:
            raise Refused('群組可用額度不足，含待核對扣點的保留額度。')

    def claim(self,intent,fingerprint):
        data=payload(intent);self.policy(intent)
        if not re.fullmatch('[0-9a-f]{64}',fingerprint):raise Refused('QR識別不正確。')
        try:
            with self.transaction() as cur:
                account=self.lock(cur,intent.wallet_alias,intent.group)
                cur.execute('SELECT * FROM mycard_private.payments WHERE group_id=%s AND message_id=%s FOR UPDATE',(intent.group,intent.message))
                old=cur.fetchone()
                if old:
                    if old['intent']!=data or old['fingerprint']!=fingerprint:raise Refused('原訊息已綁定另一筆交易。')
                    return SimpleNamespace(new=False,reference=old['reference'],state=State(old['state']))
                cur.execute("SELECT * FROM mycard_private.payments WHERE state<>'rejected_before_payment' AND (fingerprint=%s OR (wallet_alias=%s AND provider_order=%s))",(fingerprint,intent.wallet_alias,intent.expected_payment.order_ref))
                old=cur.fetchone()
                if old:
                    if old['group_id']!=intent.group:raise Refused('此QR已有處理紀錄。')
                    return SimpleNamespace(new=False,reference=old['reference'],state=State(old['state']))
                cur.execute('SELECT reference FROM mycard_private.payments WHERE wallet_alias=%s AND state=ANY(%s)',(intent.wallet_alias,list(ACTIVE)))
                if cur.fetchone():raise Refused('此扣點帳戶尚有處理中或待核對交易。')
                if account['customer']!=intent.customer:raise Refused('群組客戶已變更，請核對。')
                self.credit(cur,account,intent.amount)
                reference='MP'+uuid.uuid4().hex
                cur.execute('''INSERT INTO mycard_private.payments
                  (reference,group_id,message_id,image_id,wallet_alias,fingerprint,provider_order,intent,amount,state)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'checking')''',
                  (reference,intent.group,intent.message,intent.image_message,intent.wallet_alias,
                   fingerprint,intent.expected_payment.order_ref,json.dumps(data,ensure_ascii=False),intent.amount))
                return SimpleNamespace(new=True,reference=reference,state=State.CHECKING)
        except Exception as exc:
            if getattr(exc,'pgcode',None)=='23505':raise Refused('此QR或交易已有處理紀錄。') from None
            raise

    def transition(self,reference,expected,state):
        with self.transaction() as cur:
            account,row=self.lock_reference(cur,reference)
            if row['state']==state:return
            if row['state'] not in expected:raise Refused('此扣點狀態不允許這個操作。')
            if state==State.SENDING.value:
                data=row['intent'];policy=self.policies.get(row['group_id'])
                if (policy is None or policy.wallet!=row['wallet_alias'] or policy.customer!=account['customer']
                    or policy.rate!=Decimal(data['rate']) or data['points']>policy.max_points
                    or Decimal(data['payment']['amount'])>policy.max_face):
                    raise Refused('付款前設定已變更，需重新核對。')
                self.credit(cur,account)
            cur.execute('UPDATE mycard_private.payments SET state=%s,updated_at=now() WHERE reference=%s',(state,reference))

    def sending(self,reference):self.transition(reference,('checking',),'sending')
    def rejected(self,reference):self.transition(reference,('checking',),'rejected_before_payment')
    def unknown(self,reference):self.transition(reference,('sending',),'unknown')

    def confirmed(self,reference,provider_ref):
        if not isinstance(provider_ref,str) or not 1<=len(provider_ref)<=200:raise Refused('缺少付款交易參照。')
        with self.transaction() as cur:
            _,row=self.lock_reference(cur,reference)
            if row['state'] in ('confirmed_pending_ledger','booked') and row['provider_ref']==provider_ref:return
            if row['state']!='sending':raise Refused('此紀錄沒有可驗證的成功回應。')
            cur.execute("UPDATE mycard_private.payments SET state='confirmed_pending_ledger',provider_ref=%s,updated_at=now() WHERE reference=%s",(provider_ref,reference))

    def verify(self,row,intent,provider_ref):
        if (row['state'] not in ('confirmed_pending_ledger','booked') or row['intent']!=payload(intent)
            or row['provider_ref']!=provider_ref):raise Refused('付款參照或原交易資料不一致。')

    def assert_confirmed(self,reference,intent,provider_ref):
        with self.transaction() as cur:
            _,row=self.lock_reference(cur,reference);self.verify(row,intent,provider_ref)

    def book_once(self,reference,intent,provider_ref):
        """Confirmed debit, audit event and reservation release commit together."""
        with self.transaction() as cur:
            account,row=self.lock_reference(cur,reference);self.verify(row,intent,provider_ref)
            if row['state']=='booked':return
            if account['customer']!=intent.customer:raise Refused('群組客戶與原交易不符，需人工核對。')
            debt=account['debt']+row['amount']
            # Already-paid transactions must be recorded even if the group is
            # now disabled or its credit limit was reduced after payment.
            cur.execute('UPDATE group_credit_accounts SET debt=%s,updated_at=now() WHERE group_id=%s',(debt,intent.group))
            cur.execute('''INSERT INTO group_credit_events
              (group_id,event_id,kind,amount,debt_after,operator,reason,order_no,child_orders,delivery_status)
              VALUES(%s,%s,'mycard_sale',%s,%s,%s,%s,%s,'[]'::jsonb,'not_applicable')''',
              (intent.group,'mycard:'+reference,row['amount'],debt,intent.operator,
               'MyCard扣點 '+str(intent.points)+'點',reference))
            cur.execute("UPDATE mycard_private.payments SET state='booked',updated_at=now() WHERE reference=%s",(reference,))

    def booked(self,reference):
        # book_once performs the atomic transition. Never release a hold merely
        # because a caller claims bookkeeping succeeded.
        with self.transaction() as cur:
            _,row=self.lock_reference(cur,reference)
            if row['state']!='booked':raise Refused('尚未完成原子記帳。')
