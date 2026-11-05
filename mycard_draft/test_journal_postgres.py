"""Real isolated PostgreSQL tests; no HTTP or production credentials."""
import os
from pathlib import Path
import unittest
from decimal import Decimal as D
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import psycopg2
from psycopg2.extras import RealDictCursor
from .journal import Journal,Policy,SCHEMA_SQL,reserved_amount
from .flow import Intent,PaymentDetails,State,Refused,QRQuote,Confirmation,execute
from unittest.mock import MagicMock

DSN=os.environ['MYCARD_ISOLATED_DSN']
assert 'dbname=mycard_isolated' in DSN and 'host=/tmp/mycard-isolated-' in DSN
GROUP='group:FAKE_A'
USER='FAKE_ADMIN'

def connect():return psycopg2.connect(DSN)
def sql(query,args=None):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(query,args)
            return cur.fetchall() if cur.description else None

def fingerprint(text):return sha256(text.encode()).hexdigest()

def intent(message='first',group=GROUP,wallet='walletA',points=65,order=None):
    return Intent(group,USER,message,'image:'+message,wallet,'軒',points,D('.905'),
                  PaymentDetails(order or 'ORDER:'+message,'FAKE_PAYEE',D(points),'TWD'))

class JournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sql("CREATE ROLE anon; CREATE ROLE authenticated")
        sql((Path(__file__).parents[1]/'migration.sql').read_text())
        sql(SCHEMA_SQL)
    def setUp(self):
        sql('TRUNCATE mycard_private.payments,group_credit_events,group_credit_accounts CASCADE')
        sql("INSERT INTO group_credit_accounts(group_id,customer,debt,credit_limit,enabled) VALUES(%s,'軒',0,100,true)",(GROUP,))
        self.policy=Policy('walletA','軒',D('.905'),10000,D('10000'))
        self.j=Journal(connect,RealDictCursor,{GROUP:self.policy})
        self.i=intent();self.f=fingerprint('first QR')
    def debt(self):return sql('SELECT debt FROM group_credit_accounts WHERE group_id=%s',(GROUP,))[0][0]
    def hold(self):
        with connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:return reserved_amount(cur,GROUP)
    def claim(self):return self.j.claim(self.i,self.f)
    def confirmed(self):
        c=self.claim();self.j.sending(c.reference);self.j.confirmed(c.reference,'PROVIDER_FAKE');return c
    def test_atomic_claim_reserves_without_adding_debt(self):
        c=self.claim();self.assertTrue(c.new);self.assertEqual(c.state,State.CHECKING)
        self.assertEqual(self.debt(),0);self.assertEqual(self.hold(),D('58.83'))
    def test_default_configuration_denies_every_group(self):
        with self.assertRaises(Refused):Journal(connect,RealDictCursor).claim(self.i,self.f)
        self.assertEqual(self.hold(),0)
    def test_duplicate_message_returns_original(self):
        a=self.claim();b=self.claim()
        self.assertFalse(b.new);self.assertEqual(a.reference,b.reference);self.assertEqual(self.hold(),D('58.83'))
    def test_same_message_cannot_be_rebound(self):
        self.claim()
        with self.assertRaises(Refused):self.j.claim(replace(self.i,image_message='OTHER'),self.f)
    def test_same_qr_new_image_or_message_never_creates_second_claim(self):
        a=self.claim();b=self.j.claim(intent('second',order=self.i.expected_payment.order_ref),self.f)
        self.assertFalse(b.new);self.assertEqual(a.reference,b.reference)
        self.assertEqual(sql('SELECT count(*) FROM mycard_private.payments')[0][0],1)
    def test_same_provider_order_with_new_qr_fingerprint_is_duplicate(self):
        a=self.claim();b=self.j.claim(intent('second',order=self.i.expected_payment.order_ref),fingerprint('another image of same trade'))
        self.assertFalse(b.new);self.assertEqual(a.reference,b.reference)
    def test_wallet_shared_across_groups_allows_only_one_outstanding(self):
        other='group:FAKE_B'
        sql("INSERT INTO group_credit_accounts(group_id,customer,credit_limit,enabled) VALUES(%s,'軒',100,true)",(other,))
        self.j.policies[other]=self.policy
        def attempt(group):
            try:return self.j.claim(intent(group,group),fingerprint(group)).new
            except Refused:return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,[GROUP,other]))
        self.assertEqual(sum(results),1)
    def test_same_message_concurrent_claim_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(lambda _:self.claim(),range(6)))
        self.assertEqual(sum(c.new for c in results),1)
        self.assertEqual(len({c.reference for c in results}),1)
    def test_group_credit_holds_cover_two_wallet_instances(self):
        other=Journal(connect,RealDictCursor,{GROUP:replace(self.policy,wallet='walletB')})
        self.claim()
        with self.assertRaises(Refused):other.claim(intent('second',wallet='walletB'),fingerprint('second'))
        self.assertEqual(self.hold(),D('58.83'))
    def test_prepaid_is_available_to_new_payment(self):
        sql('UPDATE group_credit_accounts SET debt=-100 WHERE group_id=%s',(GROUP,))
        c=self.j.claim(intent(points=150),self.f)
        self.assertTrue(c.new);self.assertEqual(self.hold(),D('135.75'))
        self.assertEqual(self.debt(),-100)
    def test_declined_before_payment_releases_hold_and_allows_new_command(self):
        c=self.claim();self.j.rejected(c.reference)
        self.assertEqual(self.hold(),0)
        self.assertTrue(self.j.claim(intent('retry',order=self.i.expected_payment.order_ref),self.f).new)
    def test_unknown_survives_new_process_and_cannot_be_released(self):
        c=self.claim();self.j.sending(c.reference);self.j.unknown(c.reference)
        new=Journal(connect,RealDictCursor,{GROUP:self.policy})
        old=new.claim(self.i,self.f);self.assertFalse(old.new);self.assertEqual(old.state,State.UNKNOWN)
        with self.assertRaises(Refused):new.rejected(c.reference)
        with self.assertRaises(Refused):new.confirmed(c.reference,'MANUAL_CLAIM')
        self.assertEqual(self.hold(),D('58.83'));self.assertEqual(self.debt(),0)
    def test_rejected_cannot_be_paid(self):
        c=self.claim();self.j.rejected(c.reference)
        with self.assertRaises(Refused):self.j.sending(c.reference)
    def test_success_and_atomic_bookkeeping_once(self):
        c=self.confirmed();self.assertEqual(self.debt(),0);self.assertEqual(self.hold(),D('58.83'))
        self.j.book_once(c.reference,self.i,'PROVIDER_FAKE');self.j.booked(c.reference)
        self.j.book_once(c.reference,self.i,'PROVIDER_FAKE')
        self.assertEqual(self.debt(),D('58.83'));self.assertEqual(self.hold(),0)
        self.assertEqual(sql("SELECT kind,amount FROM group_credit_events"),[('mycard_sale',D('58.83'))])
    def test_concurrent_bookkeeping_only_one_audit_and_charge(self):
        c=self.confirmed()
        with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(lambda _:self.j.book_once(c.reference,self.i,'PROVIDER_FAKE'),range(6)))
        self.assertEqual(self.debt(),D('58.83'));self.assertEqual(sql('SELECT count(*) FROM group_credit_events')[0][0],1)
    def test_audit_failure_rolls_back_money_and_preserves_confirmed_receipt(self):
        c=self.confirmed()
        sql("ALTER TABLE group_credit_events ADD CONSTRAINT fake_fail CHECK(kind<>'mycard_sale') NOT VALID")
        try:
            with self.assertRaises(psycopg2.IntegrityError):self.j.book_once(c.reference,self.i,'PROVIDER_FAKE')
            self.assertEqual(self.debt(),0);self.assertEqual(self.hold(),D('58.83'))
        finally:sql('ALTER TABLE group_credit_events DROP CONSTRAINT fake_fail')
        self.j.book_once(c.reference,self.i,'PROVIDER_FAKE')
        self.assertEqual(self.debt(),D('58.83'));self.assertEqual(self.hold(),0)
    def test_booked_transition_cannot_fake_a_posted_ledger(self):
        c=self.confirmed()
        with self.assertRaises(Refused):self.j.booked(c.reference)
        self.assertEqual(self.debt(),0);self.assertEqual(self.hold(),D('58.83'))
    def test_forged_reference_or_amount_never_posts(self):
        c=self.confirmed()
        with self.assertRaises(Refused):self.j.book_once(c.reference,self.i,'FORGED')
        with self.assertRaises(Refused):self.j.book_once(c.reference,replace(self.i,points=66),'PROVIDER_FAKE')
        self.assertEqual(self.debt(),0)
    def test_paid_order_uses_original_rate_after_policy_change(self):
        c=self.confirmed();self.j.policies[GROUP]=replace(self.policy,rate=D('.9'))
        self.j.book_once(c.reference,self.i,'PROVIDER_FAKE');self.assertEqual(self.debt(),D('58.83'))
    def test_confirmed_payment_posts_even_if_admin_closes_group(self):
        c=self.confirmed();sql('UPDATE group_credit_accounts SET enabled=false,credit_limit=1 WHERE group_id=%s',(GROUP,))
        self.j.book_once(c.reference,self.i,'PROVIDER_FAKE');self.assertEqual(self.debt(),D('58.83'))
    def test_closing_before_payment_blocks_sending(self):
        c=self.claim();sql('UPDATE group_credit_accounts SET enabled=false WHERE group_id=%s',(GROUP,))
        with self.assertRaises(Refused):self.j.sending(c.reference)
    def test_unconfirmed_cannot_be_booked(self):
        c=self.claim()
        with self.assertRaises(Refused):self.j.book_once(c.reference,self.i,'FORGED')
        self.assertEqual(self.debt(),0)
    def test_complete_flow_with_durable_journal_and_fake_provider(self):
        import time
        now=time.time()
        quote=QRQuote(65,now+60,self.f,object(),self.i.expected_payment)
        provider=MagicMock();provider.refresh.return_value=quote
        provider.confirm.return_value=Confirmation(True,'PROVIDER_FAKE',self.i.expected_payment)
        policy=dict(admin=True,enabled=True,max_points=10000,image_group=GROUP,image_operator=USER,
                    image_time=now-2,command_time=now-1,now=now,group_enabled=True,available_credit=D('100'))
        self.assertEqual(execute(self.i,quote,self.j,provider,self.j,**policy),State.BOOKED)
        self.assertEqual(execute(self.i,quote,self.j,provider,self.j,**policy),State.BOOKED)
        provider.confirm.assert_called_once()
        self.assertEqual(self.debt(),D('58.83'))
        self.assertEqual(self.hold(),0)
    def test_private_schema_and_rls(self):
        self.assertEqual(sql("SELECT relrowsecurity FROM pg_class WHERE oid='mycard_private.payments'::regclass")[0][0],True)
        for role in ['anon','authenticated']:
            with self.subTest(role=role),self.assertRaises(psycopg2.errors.InsufficientPrivilege):
                sql('SET ROLE '+role+'; SELECT count(*) FROM mycard_private.payments')

if __name__=='__main__':unittest.main(verbosity=2)
