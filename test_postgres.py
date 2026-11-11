"""Real PostgreSQL tests. Only accepts the explicitly isolated local test DSN."""
import os,uuid,unittest,ast
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import psycopg2
from psycopg2.extras import RealDictCursor
from group_credit import GroupCredit,CreditError

DSN=os.environ['GROUP_CREDIT_TEST_DSN']
assert 'dbname=group_credit_isolated' in DSN and 'host=/tmp/group-credit-pg-' in DSN
GROUP='group:C'+'a'*32
USER='U'+'b'*32
def connect():return psycopg2.connect(DSN)
def sql(query,args=(),fetch=False):
    with connect() as c:
        with c.cursor() as cur:
            cur.execute(query,args) if args else cur.execute(query)
            return cur.fetchall() if fetch else None
def product(conn,text):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM products WHERE code=%s',(text,))
        return cur.fetchone()

class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sql('''CREATE TABLE products(id SERIAL PRIMARY KEY,code TEXT UNIQUE NOT NULL,active BOOLEAN DEFAULT TRUE,name TEXT DEFAULT '');
        CREATE TABLE serials(id SERIAL PRIMARY KEY,product_id INTEGER REFERENCES products(id),serial TEXT UNIQUE NOT NULL,status TEXT NOT NULL,sold_to TEXT,order_no TEXT,sold_at TIMESTAMPTZ,operator TEXT,batch_no TEXT);
        CREATE TABLE stock_actions(id SERIAL PRIMARY KEY,group_id TEXT,action_type TEXT,ref_no TEXT,operator TEXT,note TEXT);
        CREATE TABLE transactions(id SERIAL PRIMARY KEY,order_no TEXT UNIQUE,product_id INTEGER REFERENCES products(id),sold_to TEXT,quantity INTEGER,operator TEXT,group_id TEXT,sale_batch_no TEXT,unit_price NUMERIC,sale_amount NUMERIC,created_at TIMESTAMPTZ DEFAULT now());''')
        sql(Path(__file__).with_name('migration.sql').read_text())
        sql(Path(__file__).with_name('migration_cancel.sql').read_text())
        sql(Path(__file__).with_name('migration_ui.sql').read_text())
    def setUp(self):
        sql('TRUNCATE stock_actions,bot_ui_sessions,bot_ui_admin_groups,group_credit_events,group_credit_members,group_credit_accounts,transactions,serials,products RESTART IDENTITY CASCADE')
        sql("INSERT INTO products(code) VALUES ('MY1000'),('MY3000'),('貝1000')")
        sql("INSERT INTO serials(product_id,serial,status) SELECT 1,'FakeMixedCase_'||g||E'\\npinLower_'||g,'available' FROM generate_series(1,4) g")
        self.service=GroupCredit(connect,RealDictCursor,product,lambda:'TX_TEST_'+uuid.uuid4().hex,lambda t:[t])
        self.service.manage(GROUP,USER,'bind','bind','軒',admin=True)
        self.service.manage(GROUP,USER,'limit','limit','100000',admin=True)
        self.service.manage(GROUP,USER,'approve','approve',USER,admin=True)
        self.service.manage(GROUP,USER,'enable','enable',admin=True)
    def buy(self,event='sale',qty=1):
        return self.service.buy(GROUP,USER,event,[dict(product_text='MY1000',quantity=qty)])
    def debt(self):return sql('SELECT debt FROM group_credit_accounts',fetch=True)[0][0]
    def test_historical_sale_refund_keeps_original_905_after_rate_change(self):
        old=self.buy('historical')
        sql("UPDATE transactions SET unit_price=905,sale_amount=905 WHERE sold_to='軒'")
        sql("UPDATE group_credit_events SET amount=905,debt_after=905 WHERE kind='sale'")
        sql("UPDATE group_credit_accounts SET debt=905 WHERE group_id=%s",(GROUP,))
        replay=self.buy('historical')
        self.assertTrue(replay['duplicate']);self.assertEqual(self.debt(),905)
        self.assertEqual(replay['order_no'],old['order_no'])
        request=self.service.request_cancel(GROUP,USER,'cancel-old',old['order_no'],True)
        refunded=self.service.confirm_cancel(GROUP,USER,request['token'],True)
        self.assertEqual(refunded['amount'],905);self.assertEqual(self.debt(),0)
        self.assertEqual(self.buy('new-price')['amount'],900)

    def test_sale_and_case(self):
        r=self.buy();self.assertEqual(self.debt(),Decimal('900'));self.assertEqual(r['items'][0]['serials'][0],'FakeMixedCase_1\npinLower_1')
    def test_duplicate(self):
        self.buy();self.assertTrue(self.buy()['duplicate']);self.assertEqual(self.debt(),Decimal('900'))
    def test_concurrent_shared_cap(self):
        self.service.manage(GROUP,USER,'cap','limit','900',admin=True)
        def run(i):
            try:self.buy('sale'+str(i));return True
            except CreditError:return False
        with ThreadPoolExecutor(max_workers=2) as p:results=list(p.map(run,range(2)))
        self.assertEqual(sum(results),1);self.assertEqual(self.debt(),Decimal('900'))
    def test_concurrent_same_event(self):
        with ThreadPoolExecutor(max_workers=2) as p:results=list(p.map(lambda _:self.buy(),range(2)))
        self.assertEqual(sum(not r['duplicate'] for r in results),1);self.assertEqual(self.debt(),Decimal('900'))
    def test_insufficient_stock(self):
        with self.assertRaises(CreditError):self.buy(qty=5)
        self.assertEqual(self.debt(),0)
    def test_multiple_items_rollback(self):
        with self.assertRaises(CreditError):self.service.buy(GROUP,USER,'multi',[dict(product_text='MY1000',quantity=1),dict(product_text='MY3000',quantity=1)])
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],0)
        self.assertEqual(self.debt(),0)
    def test_revoked(self):
        self.service.manage(GROUP,USER,'rev','revoke',USER,admin=True)
        with self.assertRaises(CreditError):self.buy()
        self.assertEqual(self.debt(),0)
    def test_old_debt_payment(self):
        self.service.manage(GROUP,USER,'charge','charge','20000','舊帳',True)
        self.service.manage(GROUP,USER,'payment','payment','5000','收款',True)
        self.assertEqual(self.debt(),15000)
    def test_resend(self):
        r=self.buy();again=self.service.resend(GROUP,USER,'resend',r['order_no'],True)
        self.assertIn('FakeMixedCase_1',again['serial_chunks'][0]);self.assertEqual(self.debt(),900)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],1)
    def test_uncertain_delivery(self):
        self.buy();self.service.delivery(GROUP,'sale','uncertain');self.assertEqual(self.debt(),900)
        self.assertEqual(sql("SELECT delivery_status FROM group_credit_events WHERE kind='sale'",fetch=True)[0][0],'uncertain')

    def cancel_prompt(self):
        sale=self.buy()
        return sale,self.service.request_cancel(GROUP,USER,'request-cancel',sale['order_no'],True)
    def test_cancel_prompt_does_not_recover_or_refund(self):
        sale,prompt=self.cancel_prompt()
        self.assertEqual(self.debt(),900)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],1)
        self.assertEqual(len(prompt['token']),32)
        again=self.service.request_cancel(GROUP,USER,'request-cancel',sale['order_no'],True)
        self.assertEqual(prompt['token'],again['token'])
    def test_cancel_request_admin_only(self):
        sale=self.buy()
        with self.assertRaises(CreditError):self.service.request_cancel(GROUP,USER,'r',sale['order_no'])
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancel_requests',fetch=True)[0][0],0)
    def test_confirmation_admin_only(self):
        sale,p=self.cancel_prompt()
        with self.assertRaises(CreditError):self.service.confirm_cancel(GROUP,USER,p['token'])
        self.assertEqual(self.debt(),900)
    def test_confirmation_refunds_recovers_and_preserves_order(self):
        sale,p=self.cancel_prompt()
        r=self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(r['amount'],900);self.assertEqual(self.debt(),0)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],1)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],0)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='available'",fetch=True)[0][0],4)
        self.assertEqual(sql('SELECT confirmed_by FROM group_credit_cancelled_orders',fetch=True)[0][0],USER)
        self.assertEqual(sql("SELECT amount FROM group_credit_events WHERE kind='cancel'",fetch=True)[0][0],-900)
    def test_confirmation_duplicate_never_refunds_twice(self):
        sale,p=self.cancel_prompt()
        self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertTrue(self.service.confirm_cancel(GROUP,USER,p['token'],True)['duplicate'])
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='cancel'",fetch=True)[0][0],1)
    def test_expired_button_rejected(self):
        sale,p=self.cancel_prompt()
        sql("UPDATE group_credit_cancel_requests SET expires_at=now()-interval '1 minute'")
        with self.assertRaises(CreditError):self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),900)
    def test_cancelled_prompt_has_no_effect(self):
        sale,p=self.cancel_prompt()
        self.assertTrue(self.service.confirm_cancel(GROUP,USER,p['token'],True,True)['declined'])
        with self.assertRaises(CreditError):self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),900)
    def test_cross_group_button_rejected(self):
        sale,p=self.cancel_prompt();other='group:C'+'d'*32
        self.service.manage(other,USER,'bind-other','bind','軒',admin=True)
        with self.assertRaises(CreditError):self.service.confirm_cancel(other,USER,p['token'],True)
        self.assertEqual(sql("SELECT debt FROM group_credit_accounts WHERE group_id=%s",(GROUP,),True)[0][0],900)
    def test_missing_original_order_rejected(self):
        with self.assertRaises(CreditError):self.service.request_cancel(GROUP,USER,'r','TX_NONEXISTENT',True)
    def test_child_order_resolves_entire_parent(self):
        sale=self.buy();p=self.service.request_cancel(GROUP,USER,'r',sale['items'][0]['order_no'],True)
        self.assertEqual(p['order_no'],sale['order_no'])
    def test_mismatched_card_rolls_back(self):
        sale,p=self.cancel_prompt()
        sql("UPDATE serials SET sold_to='tampered' WHERE status='sold'")
        with self.assertRaises(CreditError):self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),900)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancelled_orders',fetch=True)[0][0],0)
    def test_paid_card_refund_returns_prepaid_exactly_once(self):
        sale,p=self.cancel_prompt()
        self.service.manage(GROUP,USER,'pay','payment','900','已收款',True)
        self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertTrue(self.service.confirm_cancel(GROUP,USER,p['token'],True)['duplicate'])
        self.assertEqual(self.debt(),-900)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],0)
    def test_concurrent_admin_confirmations_only_one_refund(self):
        sale,p=self.cancel_prompt()
        q=self.service.request_cancel(GROUP,USER,'another-request',sale['order_no'],True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda token:self.service.confirm_cancel(GROUP,USER,token,True),[p['token'],q['token']]))
        self.assertEqual(sum(not r['duplicate'] for r in results),1)
        self.assertEqual(self.debt(),0)
    def test_recovered_card_can_sell_again_without_restoring_old_order(self):
        sale,p=self.cancel_prompt();self.service.confirm_cancel(GROUP,USER,p['token'],True)
        second=self.buy('new-sale')
        self.assertEqual(second['items'][0]['serials'][0],sale['items'][0]['serials'][0])
        self.assertEqual(self.debt(),900)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],1)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],2)
        with self.assertRaises(CreditError):self.service.resend(GROUP,USER,'resend-old',sale['order_no'],True)
    def test_ledger_failure_rolls_back_cards_debt_and_cancel_records(self):
        sale,p=self.cancel_prompt()
        sql("ALTER TABLE group_credit_events ADD CONSTRAINT reject_cancel_test CHECK(kind<>'cancel')")
        try:
            with self.assertRaises(psycopg2.IntegrityError):self.service.confirm_cancel(GROUP,USER,p['token'],True)
            self.assertEqual(self.debt(),900)
            self.assertEqual(sql('SELECT count(*) FROM group_credit_cancelled_orders',fetch=True)[0][0],0)
            self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],1)
        finally:sql('ALTER TABLE group_credit_events DROP CONSTRAINT reject_cancel_test')
    def test_multiproduct_cancel_is_complete(self):
        sql("INSERT INTO serials(product_id,serial,status) VALUES(2,'Fake3000','available')")
        sale=self.service.buy(GROUP,USER,'multi',[dict(product_text='MY1000',quantity=1),dict(product_text='MY3000',quantity=1)])
        p=self.service.request_cancel(GROUP,USER,'r',sale['order_no'],True)
        self.assertEqual(p['quantity'],2)
        self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancelled_orders',fetch=True)[0][0],2)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],0)
    def test_reporting_view_does_not_change_unrelated_orders(self):
        self.buy()
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],1)
        self.assertEqual(sql("SELECT reloptions FROM pg_class WHERE relname='active_transactions'",fetch=True)[0][0],['security_invoker=true'])
    def test_active_view_supports_existing_order_locks(self):
        self.buy()
        self.assertEqual(len(sql('SELECT t.id FROM active_transactions t JOIN products p ON p.id=t.product_id FOR UPDATE OF t',fetch=True)),1)
    def test_report_amount_excludes_cancelled_order(self):
        sale,p=self.cancel_prompt();self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.buy('second',qty=2)
        self.assertEqual(sql('SELECT sum(quantity),sum(sale_amount) FROM active_transactions',fetch=True)[0],(2,Decimal('1800')))
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],2)
    def test_cancel_new_tables_have_rls(self):
        rows=sql("SELECT relrowsecurity FROM pg_class WHERE relname IN ('group_credit_cancel_requests','group_credit_cancelled_orders')",fetch=True)
        self.assertEqual(rows,[(True,),(True,)])

    def ui(self):
        from bot_ui import UIStore
        from test_ui import parser_namespace
        root=Path(__file__).parent
        path=root/'app.py'
        if not path.exists():path=root/'runtime/app.py'
        ns=parser_namespace(path)
        ns.update(get_db=connect,RealDictCursor=RealDictCursor,find_product=product,
                  create_order_no=lambda:'TX_UI_'+uuid.uuid4().hex,split_text_chunks=lambda t:[t],
                  allocate_stock_batch_no=lambda conn,code:code+'-FAKE-'+uuid.uuid4().hex[:8])
        fn=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='stock_in_serials')
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<stock-function>','exec'),ns)
        return UIStore(ns)
    def ui_sale_prompt(self):
        store=self.ui();s=store.begin(GROUP,USER,'ui-start','sale',False)
        store.change(GROUP,USER,s['token'],'product','MY1000',False)
        store.change(GROUP,USER,s['token'],'qty','1',False)
        return store,s['token']
    def ui_stock_prompt(self):
        group='group:C'+'d'*32;store=self.ui();store.bind_admin(group,USER,True)
        s=store.begin(group,USER,'ui-stock','stock',True)
        store.change(group,USER,s['token'],'product','MY1000',True)
        store.paste(group,USER,s['token'],'#供貨者測試\nMC9999999999\naBcDeF123456',True)
        return store,group,s['token']
    def test_ui_product_and_quantity_do_not_sell(self):
        store,token=self.ui_sale_prompt()
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],0)
    def test_ui_sale_confirm_is_once(self):
        store,token=self.ui_sale_prompt()
        r=store.confirm(GROUP,USER,token,False)
        self.assertEqual(r['kind'],'sale');self.assertEqual(self.debt(),900)
        self.assertTrue(store.confirm(GROUP,USER,token,False)['duplicate'])
        self.assertEqual(self.debt(),900)
    def test_ui_foreign_member_cannot_confirm(self):
        store,token=self.ui_sale_prompt();other='U'+'c'*32
        self.service.manage(GROUP,USER,'approve-other','approve',other,admin=True)
        with self.assertRaises(CreditError):store.confirm(GROUP,other,token,False)
        self.assertEqual(self.debt(),0)
    def test_ui_membership_rechecked_at_confirmation(self):
        store,token=self.ui_sale_prompt()
        self.service.manage(GROUP,USER,'revoke-now','revoke',USER,admin=True)
        with self.assertRaises(CreditError):store.confirm(GROUP,USER,token,False)
        self.assertEqual(self.debt(),0)
    def test_ui_expired_confirmation_rejected(self):
        store,token=self.ui_sale_prompt()
        sql("UPDATE bot_ui_sessions SET expires_at=now()-interval '1 minute'")
        with self.assertRaises(CreditError):store.confirm(GROUP,USER,token,False)
    def test_ui_stock_not_inserted_before_confirm(self):
        store,group,token=self.ui_stock_prompt()
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE serial LIKE 'MC999%'",fetch=True)[0][0],0)
    def test_ui_stock_confirm_atomic_and_once(self):
        store,group,token=self.ui_stock_prompt()
        r=store.confirm(group,USER,token,True)
        self.assertEqual(r['result']['added'],['MC9999999999 aBcDeF123456'])
        self.assertTrue(store.confirm(group,USER,token,True)['duplicate'])
        self.assertEqual(sql('SELECT count(*) FROM stock_actions',fetch=True)[0][0],1)
        payload,receipt=sql('SELECT payload,receipt FROM bot_ui_sessions',fetch=True)[0]
        self.assertEqual(payload,{})
        self.assertNotIn('aBcDeF',str(receipt))
    def test_ui_non_admin_stock_rejected(self):
        store,group,token=self.ui_stock_prompt()
        with self.assertRaises(CreditError):store.confirm(group,USER,token,False)
        self.assertEqual(sql('SELECT count(*) FROM stock_actions',fetch=True)[0][0],0)
    def test_ui_stock_state_failure_rolls_back_inventory(self):
        store,group,token=self.ui_stock_prompt()
        sql("ALTER TABLE bot_ui_sessions ADD CONSTRAINT ui_fail_test CHECK(state<>'done')")
        try:
            with self.assertRaises(psycopg2.IntegrityError):store.confirm(group,USER,token,True)
            self.assertEqual(sql('SELECT count(*) FROM stock_actions',fetch=True)[0][0],0)
            self.assertEqual(sql("SELECT count(*) FROM serials WHERE serial LIKE 'MC999%'",fetch=True)[0][0],0)
        finally:sql('ALTER TABLE bot_ui_sessions DROP CONSTRAINT ui_fail_test')
    def test_ui_sale_state_failure_rolls_back_cards_and_debt(self):
        store,token=self.ui_sale_prompt()
        sql("ALTER TABLE bot_ui_sessions ADD CONSTRAINT ui_fail_test CHECK(state<>'done')")
        try:
            with self.assertRaises(psycopg2.IntegrityError):store.confirm(GROUP,USER,token,False)
            self.assertEqual(self.debt(),0)
            self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],0)
        finally:sql('ALTER TABLE bot_ui_sessions DROP CONSTRAINT ui_fail_test')
    def test_ui_inventory_is_scoped_and_contains_no_credentials(self):
        rows=self.ui().inventory(GROUP,USER,False)
        self.assertEqual(dict(rows),{'MY1000':4,'MY3000':0})
        self.assertNotIn('pinLower',str(rows))
    def test_ui_admin_group_and_customer_group_are_separate(self):
        store=self.ui()
        with self.assertRaises(CreditError):store.bind_admin(GROUP,USER,True)
        group='group:C'+'d'*32;store.bind_admin(group,USER,True)
        with self.assertRaises(CreditError):self.service.manage(group,USER,'bind-customer','bind','軒',admin=True)
    def test_ui_concurrent_stock_confirmation_only_once(self):
        store,group,token=self.ui_stock_prompt()
        with ThreadPoolExecutor(max_workers=2) as p:results=list(p.map(lambda _:store.confirm(group,USER,token,True),range(2)))
        self.assertEqual(sum(not r['duplicate'] for r in results),1)
        self.assertEqual(sql('SELECT count(*) FROM stock_actions',fetch=True)[0][0],1)
    def test_ui_text_cancel_also_invalidates_confirmation(self):
        store,token=self.ui_sale_prompt()
        pending=store.pending(GROUP,USER,all_states=True)
        self.assertEqual(pending['token'],token)
        store.change(GROUP,USER,token,'cancel',None,False)
        with self.assertRaises(CreditError):store.confirm(GROUP,USER,token,False)
        self.assertEqual(self.debt(),0)
    def test_ui_old_confirmation_cannot_confirm_new_quantity(self):
        store,token=self.ui_sale_prompt()
        old=store.pending(GROUP,USER,all_states=True)['revision']
        new=store.change(GROUP,USER,token,'qty','2',False)
        with self.assertRaises(CreditError):store.confirm(GROUP,USER,token,False,expected_revision=old)
        self.assertEqual(self.debt(),0)
        store.confirm(GROUP,USER,token,False,expected_revision=new['revision'])
        self.assertEqual(self.debt(),1800)
    def test_ui_old_stock_confirmation_cannot_confirm_replacement_data(self):
        store,group,token=self.ui_stock_prompt()
        old=store.pending(group,USER,all_states=True)['revision']
        store.change(group,USER,token,'product','MY1000',True)
        new=store.paste(group,USER,token,'MC8888888888\nABCDEF123456',True)
        with self.assertRaises(CreditError):store.confirm(group,USER,token,True,expected_revision=old)
        self.assertEqual(sql('SELECT count(*) FROM stock_actions',fetch=True)[0][0],0)
        store.confirm(group,USER,token,True,expected_revision=new['revision'])
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE serial='MC9999999999 aBcDeF123456'",fetch=True)[0][0],0)
    def test_quick_role_rechecks_membership_without_money_reads(self):
        from bottom_quick import context_role
        self.assertEqual(context_role(GROUP,USER,False,dict(get_db=connect)),'customer')
        self.service.manage(GROUP,USER,'revoke-for-quick','revoke',USER,admin=True)
        self.assertIsNone(context_role(GROUP,USER,False,dict(get_db=connect)))
    def test_quick_admin_group_only_for_admins(self):
        from bottom_quick import context_role
        group='group:C'+'d'*32;self.ui().bind_admin(group,USER,True)
        self.assertEqual(context_role(group,USER,True,dict(get_db=connect)),'admin')
        self.assertIsNone(context_role(group,USER,False,dict(get_db=connect)))

    def setup_test_group(self):
        other='group:C'+'e'*32
        sql("INSERT INTO products(code) VALUES ('test100')")
        sql("INSERT INTO serials(product_id,serial,status) SELECT id,'FakeTestOnly_'||g,'available' FROM products CROSS JOIN generate_series(1,3) g WHERE code='test100'")
        self.service.manage(other,USER,'bind','bind','測試',admin=True)
        self.service.manage(other,USER,'limit','limit','1810',admin=True)
        self.service.manage(other,USER,'approve','approve',USER,admin=True)
        self.service.manage(other,USER,'enable','enable',admin=True)
        return other
    def test_test_group_only_test_stock_and_own_debt(self):
        other=self.setup_test_group()
        r=self.service.buy(other,USER,'test-sale',[dict(product_text='test100',quantity=2)])
        self.assertEqual(r['amount'],Decimal('1810'))
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE product_id=1 AND status='available'",fetch=True)[0][0],4)
        self.assertEqual(sql("SELECT debt FROM group_credit_accounts WHERE group_id=%s",(GROUP,),True)[0][0],0)
        self.assertEqual(sql("SELECT debt FROM group_credit_accounts WHERE group_id=%s",(other,),True)[0][0],1810)
        self.assertTrue(self.service.buy(other,USER,'test-sale',[dict(product_text='test100',quantity=2)])['duplicate'])
    def test_test_group_blocks_real_cards_even_root(self):
        other=self.setup_test_group()
        with self.assertRaises(CreditError):self.service.buy(other,USER,'bad',[dict(product_text='MY1000',quantity=1)],True)
        self.assertEqual(sql("SELECT count(*) FROM transactions",fetch=True)[0][0],0)
    def test_real_group_blocks_test_card(self):
        self.setup_test_group()
        with self.assertRaises(CreditError):self.service.buy(GROUP,USER,'bad',[dict(product_text='test100',quantity=1)],True)
    def test_mixed_test_real_request_rolls_back(self):
        other=self.setup_test_group()
        with self.assertRaises(CreditError):self.service.buy(other,USER,'mixed',[dict(product_text='test100',quantity=1),dict(product_text='MY1000',quantity=1)],True)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],0)
    def test_test_cap_membership_and_revocation_are_enforced(self):
        other=self.setup_test_group()
        with self.assertRaises(CreditError):self.service.buy(other,'U'+'f'*32,'unknown',[dict(product_text='test100',quantity=1)])
        self.service.buy(other,USER,'first',[dict(product_text='test100',quantity=2)])
        with self.assertRaises(CreditError):self.service.buy(other,USER,'over',[dict(product_text='test100',quantity=1)])
        self.service.manage(other,USER,'revoke','revoke',USER,admin=True)
        with self.assertRaises(CreditError):self.service.buy(other,USER,'revoked',[dict(product_text='test100',quantity=1)])
    def test_test_cancel_keeps_real_stock_and_account(self):
        other=self.setup_test_group()
        r=self.service.buy(other,USER,'first',[dict(product_text='test100',quantity=1)])
        prompt=self.service.request_cancel(other,USER,'cancel',r['order_no'],True)
        c=self.service.confirm_cancel(other,USER,prompt['token'],True)
        self.assertEqual(c['customer'],'測試');self.assertEqual(c['debt'],0)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE product_id=1 AND status='available'",fetch=True)[0][0],4)
    def test_rebind_cannot_convert_real_to_test(self):
        with self.assertRaises(CreditError):self.service.manage(GROUP,USER,'switch','bind','測試',admin=True)
        self.assertEqual(sql("SELECT customer FROM group_credit_accounts WHERE group_id=%s",(GROUP,),True)[0][0],'軒')
    def test_test_bind_starts_closed_without_limit(self):
        other='group:C'+'e'*32
        self.service.manage(other,USER,'bind','bind','測試',admin=True)
        self.assertEqual(sql("SELECT customer,debt,credit_limit,enabled FROM group_credit_accounts WHERE group_id=%s",(other,),True)[0],('測試',0,None,False))
        with self.assertRaises(CreditError):self.service.manage(other,USER,'enable','enable',admin=True)


    def test_calculator_charge_payment_and_zero_share_sale_balance(self):
        self.buy()
        r=self.service.calculate(GROUP,USER,'payment','+500 #匯款',True)
        self.assertEqual(r['previous_debt'],900);self.assertEqual(r['debt'],400)
        self.assertEqual(self.service.calculate(GROUP,USER,'query','+0')['debt'],400)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator'",fetch=True)[0][0],1)
        self.assertEqual(sql("SELECT amount FROM group_credit_events WHERE kind='calculator'",fetch=True)[0][0],-500)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],1)
    def test_calculator_approved_nonadmin_never_changes_debt(self):
        for raw in ('-905','+905','/計算 -1000*0.905'):
            with self.assertRaises(CreditError):self.service.calculate(GROUP,USER,'try',raw)
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator'",fetch=True)[0][0],0)
    def test_calculator_zero_foreign_or_revoked_member_denied(self):
        with self.assertRaises(CreditError):self.service.calculate(GROUP,'U'+'f'*32,'query','+0')
        self.service.manage(GROUP,USER,'revoke','revoke',USER,admin=True)
        with self.assertRaises(CreditError):self.service.calculate(GROUP,USER,'query','+0')
    def test_calculator_zero_no_ledger_write(self):
        count=sql('SELECT count(*) FROM group_credit_events',fetch=True)[0][0]
        self.service.calculate(GROUP,USER,'query','+0')
        self.assertEqual(sql('SELECT count(*) FROM group_credit_events',fetch=True)[0][0],count)
    def test_calculator_concurrent_same_event_only_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.service.calculate(GROUP,USER,'same','-905',True),range(2)))
        self.assertEqual(sum(not r.get('duplicate') for r in results),1)
        self.assertEqual(self.debt(),905)
        self.assertEqual(sql("SELECT count(*) FROM transactions",fetch=True)[0][0],0)
    def test_calculator_serializes_concurrent_sale_and_payment(self):
        self.service.calculate(GROUP,USER,'opening','-1000',True)
        def run(i):
            return self.buy('sale') if i==0 else self.service.calculate(GROUP,USER,'payment','+500',True)
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(run,range(2)))
        self.assertEqual(self.debt(),1400)
    def test_calculator_test_group_ledger_is_isolated(self):
        other=self.setup_test_group()
        r=self.service.calculate(other,USER,'charge','-1000*0.905',True)
        self.assertEqual(r['customer'],'測試');self.assertEqual(r['debt'],905)
        self.assertEqual(sql("SELECT debt FROM group_credit_accounts WHERE group_id=%s",(GROUP,),True)[0][0],0)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],0)
    def test_calculator_overpayment_persists_prepaid(self):
        self.service.calculate(GROUP,USER,'charge','-14525',True)
        self.service.calculate(GROUP,USER,'over','+20000',True)
        self.assertEqual(self.debt(),-5475)
        self.buy()
        self.assertEqual(self.debt(),-4575)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator'",fetch=True)[0][0],2)
    def test_calculator_manual_charge_can_record_over_cap_but_sale_is_blocked(self):
        self.service.manage(GROUP,USER,'lowcap','limit','500',admin=True)
        self.service.calculate(GROUP,USER,'charge','-900',True)
        with self.assertRaises(CreditError):self.buy()
        self.assertEqual(self.debt(),900)
    def test_calculator_unbound_group_cannot_create_account(self):
        with self.assertRaises(CreditError):self.service.calculate('group:C'+'f'*32,USER,'charge','-905',True)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_accounts',fetch=True)[0][0],1)

    def test_repeat_new_message_prompts_without_adjusting(self):
        self.service.calculate(GROUP,USER,'first','-905',True)
        p=self.service.calculate(GROUP,USER,'again','-1000*0.905 #第二筆',True)
        self.assertTrue(p['needs_confirmation']);self.assertEqual(self.debt(),905)
        r=self.service.calculate(GROUP,USER,'again','-1000*0.905 #第二筆',True)
        self.assertEqual(p['token'],r['token'])
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator'",fetch=True)[0][0],1)
    def test_repeat_confirmation_exactly_once(self):
        from ledger_controls import confirm
        self.service.calculate(GROUP,USER,'first','-905',True)
        p=self.service.calculate(GROUP,USER,'again','-905',True)
        r=confirm(self.service,GROUP,USER,p['token'],True)
        self.assertEqual(r['debt'],1810)
        self.assertTrue(confirm(self.service,GROUP,USER,p['token'],True)['duplicate'])
        self.assertEqual(self.debt(),1810)
    def test_repeat_rejects_wrong_admin_group_nonadmin_and_expiry(self):
        from ledger_controls import confirm
        self.service.calculate(GROUP,USER,'first','-905',True)
        p=self.service.calculate(GROUP,USER,'again','-905',True)
        with self.assertRaises(CreditError):confirm(self.service,GROUP,'U'+'f'*32,p['token'],True)
        with self.assertRaises(CreditError):confirm(self.service,GROUP,USER,p['token'])
        other=self.setup_test_group()
        with self.assertRaises(CreditError):confirm(self.service,other,USER,p['token'],True)
        sql("UPDATE group_credit_events SET created_at=now()-interval '6 minutes' WHERE order_no=%s",(p['token'],))
        with self.assertRaises(CreditError):confirm(self.service,GROUP,USER,p['token'],True)
        self.assertEqual(sql("SELECT debt FROM group_credit_accounts WHERE group_id=%s",(GROUP,),True)[0][0],905)
    def test_repeat_decline_is_final_and_preserves_debt(self):
        from ledger_controls import confirm
        self.service.calculate(GROUP,USER,'first','-905',True)
        p=self.service.calculate(GROUP,USER,'again','-905',True)
        self.assertTrue(confirm(self.service,GROUP,USER,p['token'],True,True)['declined'])
        self.assertTrue(confirm(self.service,GROUP,USER,p['token'],True)['duplicate'])
        self.assertEqual(self.debt(),905)
    def test_repeat_confirmation_concurrent_only_once(self):
        from ledger_controls import confirm
        self.service.calculate(GROUP,USER,'first','-905',True)
        p=self.service.calculate(GROUP,USER,'again','-905',True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows=list(pool.map(lambda _:confirm(self.service,GROUP,USER,p['token'],True),range(2)))
        self.assertEqual(sum(not r.get('duplicate') for r in rows),1);self.assertEqual(self.debt(),1810)
    def test_repeat_different_admin_or_old_amount_does_not_trigger(self):
        self.service.calculate(GROUP,USER,'first','-905',True)
        self.assertFalse(self.service.calculate(GROUP,'U'+'f'*32,'other-admin','-905',True).get('needs_confirmation'))
        sql("UPDATE group_credit_events SET created_at=now()-interval '121 seconds'")
        self.assertFalse(self.service.calculate(GROUP,USER,'later','-905',True).get('needs_confirmation'))
        self.assertEqual(self.debt(),2715)
    def test_manual_correction_preserves_original_and_has_no_stock_effect(self):
        from ledger_controls import request_reverse,confirm,history
        r=self.service.calculate(GROUP,USER,'first','-905',True)
        p=request_reverse(self.service,GROUP,USER,'request',r['record_id'],'金額輸入錯誤',True)
        self.assertEqual(self.debt(),905)
        c=confirm(self.service,GROUP,'U'+'f'*32,p['token'],True)
        self.assertEqual(c['debt'],0)
        self.assertEqual(sql("SELECT amount FROM group_credit_events WHERE event_id='first'",fetch=True)[0][0],905)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],0)
        self.assertEqual(sql("SELECT count(*) FROM transactions",fetch=True)[0][0],0)
        self.assertTrue(next(row for row in history(self.service,GROUP,USER,True) if row['event_id']=='first')['corrected'])
    def test_two_correction_requests_cannot_refund_twice(self):
        from ledger_controls import request_reverse,confirm
        r=self.service.calculate(GROUP,USER,'first','-905',True)
        p=request_reverse(self.service,GROUP,USER,'r1',r['record_id'],'錯帳',True)
        q=request_reverse(self.service,GROUP,USER,'r2',r['record_id'],'錯帳',True)
        confirm(self.service,GROUP,USER,p['token'],True)
        with self.assertRaises(CreditError):confirm(self.service,GROUP,USER,q['token'],True)
        self.assertTrue(confirm(self.service,GROUP,USER,p['token'],True)['duplicate'])
        self.assertEqual(self.debt(),0)
    def test_correction_rejects_sale_and_another_group(self):
        from ledger_controls import request_reverse
        sale=self.buy()
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'r',sale['order_no'],'錯帳',True)
        r=self.service.calculate(GROUP,USER,'manual','-100',True)
        other=self.setup_test_group()
        with self.assertRaises(CreditError):request_reverse(self.service,other,USER,'r',r['record_id'],'錯帳',True)
    def test_correction_permission_and_reason(self):
        from ledger_controls import request_reverse,history
        r=self.service.calculate(GROUP,USER,'first','-905',True)
        with self.assertRaises(CreditError):history(self.service,GROUP,USER)
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'r',r['record_id'],'錯帳')
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'r',r['record_id'],'',True)
    def test_correction_rechecks_current_balance_after_prompt(self):
        from ledger_controls import request_reverse,confirm
        r=self.service.calculate(GROUP,USER,'first','-905',True)
        p=request_reverse(self.service,GROUP,USER,'r',r['record_id'],'錯帳',True)
        self.service.calculate(GROUP,USER,'payment','+905',True)
        confirm(self.service,GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),-905)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator_reversal'",fetch=True)[0][0],1)
    def test_history_limits_and_excludes_pending_entries(self):
        from ledger_controls import history
        for i in range(12):self.service.calculate(GROUP,USER,'e'+str(i),'-'+str(i+1),True)
        self.service.calculate(GROUP,USER,'pending','-12',True)
        rows=history(self.service,GROUP,USER,True)
        self.assertEqual(len(rows),10);self.assertTrue(all(r['kind']=='calculator' for r in rows))
    def test_test_account_history_and_correction_stay_isolated(self):
        from ledger_controls import request_reverse,confirm,history
        other=self.setup_test_group()
        r=self.service.calculate(other,USER,'first','-905',True)
        p=request_reverse(self.service,other,USER,'r',r['record_id'],'測試更正',True)
        confirm(self.service,other,USER,p['token'],True)
        self.assertEqual(history(self.service,GROUP,USER,True),[])
        self.assertEqual(sql("SELECT debt FROM group_credit_accounts WHERE group_id=%s",(other,),True)[0][0],0)
    def test_other_admin_can_still_confirm_stock_withdrawal(self):
        sale,p=self.cancel_prompt()
        r=self.service.confirm_cancel(GROUP,'U'+'f'*32,p['token'],True)
        self.assertEqual(r['debt'],0)
        self.assertEqual(sql('SELECT confirmed_by FROM group_credit_cancelled_orders',fetch=True)[0][0],'U'+'f'*32)
    def test_stock_summary_anonymous_access_revoked_owner_retained(self):
        sql("CREATE ROLE anon")
        sql("CREATE VIEW public.inventory_summary AS SELECT code FROM products")
        sql("GRANT SELECT ON public.inventory_summary TO PUBLIC,anon")
        sql(Path(__file__).with_name('lock_inventory_summary.sql').read_text())
        self.assertFalse(sql("SELECT has_table_privilege('anon','public.inventory_summary','SELECT')",fetch=True)[0][0])
        self.assertEqual(sql('SELECT count(*) FROM public.inventory_summary',fetch=True)[0][0],3)

    def test_undo_latest_manual_matches_screenshot_and_preserves_stock(self):
        from ledger_controls import request_reverse,confirm
        self.service.manage(GROUP,USER,'opening','charge','4525','既有欠款',True)
        manual=self.service.calculate(GROUP,USER,'wrong','-10000',True)
        pending=request_reverse(self.service,GROUP,USER,'undo',None,'管理員撤回手動記帳',True)
        self.assertEqual(pending['reference'],manual['record_id']);self.assertEqual(self.debt(),14525)
        result=confirm(self.service,GROUP,'U'+'f'*32,pending['token'],True)
        self.assertEqual(result['debt'],4525)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='available'",fetch=True)[0][0],4)
        self.assertEqual(sql("SELECT amount FROM group_credit_events WHERE event_id='wrong'",fetch=True)[0][0],10000)
    def test_undo_latest_is_admin_only(self):
        from ledger_controls import request_reverse
        self.service.calculate(GROUP,USER,'manual','-905',True)
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'undo',None,'撤回')
        self.assertEqual(self.debt(),905)
    def test_undo_latest_never_skips_a_newer_sale(self):
        from ledger_controls import request_reverse
        self.service.calculate(GROUP,USER,'manual','-100',True)
        sale=self.buy()
        p=request_reverse(self.service,GROUP,USER,'undo',None,'撤回',True)
        self.assertEqual(p['undo_kind'],'sale');self.assertEqual(p['order_no'],sale['order_no'])
        self.assertEqual(self.debt(),1000)
        self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),100)
    def test_undo_latest_never_walks_back_after_correction(self):
        from ledger_controls import request_reverse,confirm
        self.service.calculate(GROUP,USER,'first','-100',True)
        self.service.calculate(GROUP,USER,'last','-200',True)
        p=request_reverse(self.service,GROUP,USER,'undo',None,'撤回',True)
        confirm(self.service,GROUP,USER,p['token'],True)
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'undo-again',None,'撤回',True)
        self.assertEqual(self.debt(),100)
    def test_undo_latest_ignores_pending_and_readonly_queries(self):
        from ledger_controls import request_reverse
        r=self.service.calculate(GROUP,USER,'manual','-905',True)
        self.service.calculate(GROUP,USER,'pending','-905',True)
        self.service.calculate(GROUP,USER,'query','+0',True)
        p=request_reverse(self.service,GROUP,USER,'undo',None,'撤回',True)
        self.assertEqual(p['reference'],r['record_id']);self.assertEqual(self.debt(),905)
    def test_undo_latest_redelivery_keeps_original_target(self):
        from ledger_controls import request_reverse
        r=self.service.calculate(GROUP,USER,'manual','-100',True)
        p=request_reverse(self.service,GROUP,USER,'undo',None,'撤回',True)
        self.service.calculate(GROUP,USER,'later','-200',True)
        again=request_reverse(self.service,GROUP,USER,'undo',None,'撤回',True)
        self.assertEqual(again['token'],p['token']);self.assertEqual(again['reference'],r['record_id'])
    def test_undo_latest_empty_group_is_noop(self):
        from ledger_controls import request_reverse
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'undo',None,'撤回',True)
        self.assertEqual(self.debt(),0)

    def test_prepaid_concurrent_sales_and_limit(self):
        self.service.manage(GROUP,USER,'cap','limit','100',admin=True)
        self.service.calculate(GROUP,USER,'cash','+1800',True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda i:self.buy('prepaid-sale'+str(i)),range(2)))
        self.assertEqual(self.debt(),0)
        with self.assertRaises(CreditError):self.buy('exceeds')
        self.assertEqual(sql("SELECT sum(sale_amount) FROM transactions",fetch=True)[0][0],1800)

    def test_prepaid_constraint_upgrade_preserves_existing_balance(self):
        sql("ALTER TABLE group_credit_accounts DROP CONSTRAINT group_credit_accounts_debt_check; ALTER TABLE group_credit_accounts ADD CONSTRAINT group_credit_accounts_debt_check CHECK (debt>=0)")
        sql(Path(__file__).with_name('enable_group_prepay.sql').read_text())
        self.assertEqual(self.debt(),0)
        self.service.calculate(GROUP,USER,'prepay','+1000',True)
        self.assertEqual(self.debt(),-1000)
        for invalid in ('NaN','10000000000000000','-10000000000000000'):
            with self.assertRaises(psycopg2.Error):
                sql('UPDATE group_credit_accounts SET debt=%s',(invalid,))
        self.assertEqual(self.debt(),-1000)

    def test_undo_card_replay_keeps_target_after_newer_manual_and_sale(self):
        from ledger_controls import request_reverse
        first=self.buy('first')
        p=request_reverse(self.service,GROUP,USER,'undo',None,'撤回上一動',True)
        self.service.calculate(GROUP,USER,'later-manual','-100',True)
        self.buy('second')
        again=request_reverse(self.service,GROUP,USER,'undo',None,'撤回上一動',True)
        self.assertEqual(again['token'],p['token']);self.assertEqual(again['order_no'],first['order_no'])
        self.assertEqual(self.debt(),1900)

    def test_undo_card_other_admin_refunds_prepaid_once(self):
        from ledger_controls import request_reverse
        self.service.calculate(GROUP,USER,'cash','+2000',True)
        self.buy()
        p=request_reverse(self.service,GROUP,USER,'undo',None,'撤回上一動',True)
        self.assertEqual(self.debt(),-1100)
        r=self.service.confirm_cancel(GROUP,'U'+'f'*32,p['token'],True)
        self.assertEqual(r['debt'],-2000)
        self.assertTrue(self.service.confirm_cancel(GROUP,USER,p['token'],True)['duplicate'])
        self.assertEqual(self.debt(),-2000)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='available'",fetch=True)[0][0],4)

    def test_undo_card_requires_admin_before_pending_write(self):
        from ledger_controls import request_reverse
        self.buy()
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'undo',None,'撤回上一動')
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancel_requests',fetch=True)[0][0],0)
        self.assertEqual(self.debt(),900)

    def test_undo_after_card_recovery_does_not_skip_to_older_sale(self):
        from ledger_controls import request_reverse
        self.buy('old');self.buy('new')
        p=request_reverse(self.service,GROUP,USER,'undo',None,'撤回上一動',True)
        self.service.confirm_cancel(GROUP,USER,p['token'],True)
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'again',None,'撤回上一動',True)
        self.assertEqual(self.debt(),900)

    def test_undo_test_card_does_not_touch_real_account(self):
        from ledger_controls import request_reverse
        other=self.setup_test_group()
        sale=self.service.buy(other,USER,'test-sale',[dict(product_text='test100',quantity=1)])
        p=request_reverse(self.service,other,USER,'undo',None,'撤回上一動',True)
        self.assertEqual(p['order_no'],sale['order_no'])
        self.service.confirm_cancel(other,USER,p['token'],True)
        self.assertEqual(sql('SELECT debt FROM group_credit_accounts WHERE group_id=%s',(other,),True)[0][0],0)
        self.assertEqual(sql('SELECT debt FROM group_credit_accounts WHERE group_id=%s',(GROUP,),True)[0][0],0)

    def test_undo_card_expiry_and_decline_preserve_sale(self):
        from ledger_controls import request_reverse
        self.buy()
        p=request_reverse(self.service,GROUP,USER,'undo',None,'撤回上一動',True)
        self.service.confirm_cancel(GROUP,USER,p['token'],True,True)
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'undo',None,'撤回上一動',True)
        q=request_reverse(self.service,GROUP,USER,'new-request',None,'撤回上一動',True)
        sql("UPDATE group_credit_cancel_requests SET expires_at=now()-interval '1 second' WHERE token=%s",(q['token'],))
        with self.assertRaises(CreditError):self.service.confirm_cancel(GROUP,USER,q['token'],True)
        self.assertEqual(self.debt(),900)

    def test_direct_undo_manual_atomic_audit_and_redelivery(self):
        from ledger_controls import undo_previous
        self.service.manage(GROUP,USER,'opening','charge','4525','既有帳',True)
        source=self.service.calculate(GROUP,USER,'manual','-10000',True)
        r=undo_previous(self.service,GROUP,USER,'direct',True)
        self.assertEqual(self.debt(),4525);self.assertTrue(r['record_id'].startswith('AR'))
        self.assertEqual(r['source_reference'],source['record_id'])
        self.assertTrue(undo_previous(self.service,GROUP,USER,'direct',True)['duplicate'])
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator_reversal'",fetch=True)[0][0],1)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator_reverse_pending'",fetch=True)[0][0],0)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='available'",fetch=True)[0][0],4)

    def test_direct_undo_card_refund_and_stock_commit_together(self):
        from ledger_controls import undo_previous
        self.service.calculate(GROUP,USER,'prepaid','+2000',True)
        sale=self.buy()
        r=undo_previous(self.service,GROUP,'U'+'f'*32,'direct',True)
        self.assertEqual(r['order_no'],sale['order_no']);self.assertTrue(r['record_id'].startswith('RC'))
        self.assertEqual(self.debt(),-2000)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='available'",fetch=True)[0][0],4)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],0)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],1)
        self.assertEqual(sql("SELECT reason FROM group_credit_events WHERE kind='cancel'",fetch=True)[0][0],'管理員直接撤回發卡 '+sale['order_no'])
        self.assertTrue(undo_previous(self.service,GROUP,USER,'direct',True)['duplicate'])
        self.assertEqual(self.debt(),-2000)

    def test_direct_undo_bad_card_rolls_back_request_and_balance(self):
        from ledger_controls import undo_previous
        self.buy()
        sql("UPDATE serials SET sold_to='tampered' WHERE status='sold'")
        with self.assertRaises(CreditError):undo_previous(self.service,GROUP,USER,'direct',True)
        self.assertEqual(self.debt(),900)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancel_requests',fetch=True)[0][0],0)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancelled_orders',fetch=True)[0][0],0)

    def test_direct_undo_root_only_and_repeat_until_empty(self):
        from ledger_controls import undo_previous
        self.buy('first');self.buy('second')
        with self.assertRaises(CreditError):undo_previous(self.service,GROUP,USER,'forbidden')
        undo_previous(self.service,GROUP,USER,'direct',True)
        undo_previous(self.service,GROUP,USER,'another',True)
        self.assertEqual(self.debt(),0)
        with self.assertRaises(CreditError):undo_previous(self.service,GROUP,USER,'empty',True)

    def test_direct_undo_cash_receipt_reverses_prepaid(self):
        from ledger_controls import undo_previous
        self.service.calculate(GROUP,USER,'payment','+20000',True)
        undo_previous(self.service,GROUP,USER,'direct',True)
        self.assertEqual(self.debt(),0)

    def test_direct_undo_concurrent_redelivery_one_refund(self):
        from ledger_controls import undo_previous
        self.buy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:undo_previous(self.service,GROUP,USER,'same',True),range(2)))
        self.assertEqual(sum(not r.get('duplicate') for r in results),1)
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='cancel'",fetch=True)[0][0],1)

    def test_direct_undo_test_account_isolated_from_real_stock(self):
        from ledger_controls import undo_previous
        other=self.setup_test_group()
        self.service.buy(other,USER,'test',[dict(product_text='test100',quantity=1)])
        undo_previous(self.service,other,USER,'direct',True)
        self.assertEqual(sql('SELECT debt FROM group_credit_accounts WHERE group_id=%s',(other,),True)[0][0],0)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE product_id=1 AND status='available'",fetch=True)[0][0],4)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],0)

    def test_direct_undo_replay_does_not_target_newer_action(self):
        from ledger_controls import undo_previous
        self.buy('first')
        r=undo_previous(self.service,GROUP,USER,'direct',True)
        self.buy('second')
        again=undo_previous(self.service,GROUP,USER,'direct',True)
        self.assertTrue(again['duplicate']);self.assertEqual(again['record_id'],r['record_id'])
        self.assertEqual(self.debt(),900)

    def test_direct_undo_audit_failure_rolls_back_card_and_money(self):
        from ledger_controls import undo_previous
        from unittest.mock import patch
        self.buy()
        with patch.object(self.service,'_event',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):undo_previous(self.service,GROUP,USER,'direct',True)
        self.assertEqual(self.debt(),900)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],1)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancelled_orders',fetch=True)[0][0],0)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancel_requests',fetch=True)[0][0],0)

    def test_direct_undo_manual_audit_failure_rolls_back_balance(self):
        from ledger_controls import undo_previous
        from unittest.mock import patch
        self.service.calculate(GROUP,USER,'manual','-1000',True)
        with patch.object(self.service,'_event',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):undo_previous(self.service,GROUP,USER,'direct',True)
        self.assertEqual(self.debt(),1000)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator_reversal'",fetch=True)[0][0],0)

    def test_repeat_undo_mixed_sale_payment_and_charge_in_order(self):
        from ledger_controls import undo_previous
        charge=self.service.calculate(GROUP,USER,'charge','-1000',True)
        first=self.buy('first')
        payment=self.service.calculate(GROUP,USER,'cash','+5000',True)
        second=self.buy('second')
        results=[undo_previous(self.service,GROUP,USER,'u'+str(i),True) for i in range(4)]
        self.assertEqual([r.get('order_no') or r.get('source_reference') for r in results],
                         [second['order_no'],payment['record_id'],first['order_no'],charge['record_id']])
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='available'",fetch=True)[0][0],4)
        with self.assertRaises(CreditError):undo_previous(self.service,GROUP,USER,'empty',True)
        self.assertTrue(undo_previous(self.service,GROUP,USER,'u0',True)['duplicate'])
        self.assertEqual(self.debt(),0)

    def test_repeat_undo_skips_previously_corrected_manual(self):
        from ledger_controls import undo_previous,request_reverse,confirm
        old=self.service.calculate(GROUP,USER,'old','-100',True)
        newer=self.service.calculate(GROUP,USER,'new','-200',True)
        prompt=request_reverse(self.service,GROUP,USER,'correction',newer['record_id'],'錯帳',True)
        confirm(self.service,GROUP,USER,prompt['token'],True)
        r=undo_previous(self.service,GROUP,USER,'undo',True)
        self.assertEqual(r['source_reference'],old['record_id']);self.assertEqual(self.debt(),0)

    def test_repeat_undo_concurrent_distinct_commands_each_undo_one(self):
        from ledger_controls import undo_previous
        self.buy('first');self.buy('second')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda i:undo_previous(self.service,GROUP,USER,'u'+str(i),True),range(2)))
        self.assertEqual(len({r['order_no'] for r in results}),2)
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='cancel'",fetch=True)[0][0],2)

    def test_repeat_undo_more_than_two_manual_entries(self):
        from ledger_controls import undo_previous
        entries=[self.service.calculate(GROUP,USER,'e'+str(i),'-'+str(i+1),True) for i in range(12)]
        results=[undo_previous(self.service,GROUP,USER,'u'+str(i),True) for i in range(12)]
        self.assertEqual([r['source_reference'] for r in results],[r['record_id'] for r in reversed(entries)])
        self.assertEqual(self.debt(),0)
        with self.assertRaises(CreditError):undo_previous(self.service,GROUP,USER,'empty',True)

    def test_mention_approve_revoke_is_group_scoped_and_not_ledger_admin(self):
        from types import SimpleNamespace as N
        from unittest.mock import MagicMock
        from group_credit_handler import handle_group_credit
        target='U'+'f'*32
        ns=dict(get_context_id=lambda e:GROUP,get_operator=lambda e:USER,is_admin=lambda u:True,
                get_db=connect,RealDictCursor=RealDictCursor,find_product=product,create_order_no=lambda:'TX_FAKE',split_text_chunks=lambda t:[t],reply=MagicMock())
        for command,event in (('/自助核准','mention-approve'),('/自助停權','mention-revoke')):
            msg=N(text=command+' @客人',id=event,mention=dict(mentionees=[dict(type='user',index=6,length=3,userId=target)]))
            handle_group_credit(N(message=msg),ns)
            if command=='/自助核准':
                self.assertTrue(sql('SELECT active FROM group_credit_members WHERE group_id=%s AND line_user_id=%s',(GROUP,target),True)[0][0])
                with self.assertRaises(CreditError):self.service.calculate(GROUP,target,'charge','-1',False)
                handle_group_credit(N(message=msg),ns)
                self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE event_id='mention-approve'",fetch=True)[0][0],1)
        self.assertFalse(sql('SELECT active FROM group_credit_members WHERE group_id=%s AND line_user_id=%s',(GROUP,target),True)[0][0])
        self.assertEqual(self.debt(),0)

    def test_mention_nonadmin_does_not_grant_membership(self):
        from types import SimpleNamespace as N
        from unittest.mock import MagicMock
        from group_credit_handler import handle_group_credit
        target='U'+'f'*32
        ns=dict(get_context_id=lambda e:GROUP,get_operator=lambda e:USER,is_admin=lambda u:False,get_db=connect,
                RealDictCursor=RealDictCursor,find_product=product,create_order_no=lambda:'TX_FAKE',split_text_chunks=lambda t:[t],reply=MagicMock())
        msg=N(text='/自助核准 @客人',id='forbidden',mention=dict(mentionees=[dict(type='user',index=6,length=3,userId=target)]))
        handle_group_credit(N(message=msg),ns)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_members WHERE line_user_id=%s',(target,),True)[0][0],0)

    def test_mention_grant_cannot_open_group_or_change_limit(self):
        from types import SimpleNamespace as N
        from unittest.mock import MagicMock
        from group_credit_handler import handle_group_credit
        other=self.setup_test_group()
        self.service.manage(other,USER,'disable','disable',admin=True)
        target='U'+'f'*32
        ns=dict(get_context_id=lambda e:other,get_operator=lambda e:USER,is_admin=lambda u:True,get_db=connect,
                RealDictCursor=RealDictCursor,find_product=product,create_order_no=lambda:'TX_FAKE',split_text_chunks=lambda t:[t],reply=MagicMock())
        msg=N(text='/自助核准 @客人',id='grant',mention=dict(mentionees=[dict(type='user',index=6,length=3,userId=target)]))
        handle_group_credit(N(message=msg),ns)
        row=sql('SELECT enabled,credit_limit,debt FROM group_credit_accounts WHERE group_id=%s',(other,),True)[0]
        self.assertEqual(row,(False,1810,0))
        with self.assertRaises(CreditError):self.service.buy(other,target,'sale',[dict(product_text='test100',quantity=1)])


    def sheet_namespace(self):
        from unittest.mock import MagicMock
        return {key:MagicMock(return_value=True) for key in
            ('append_sales_rows','mark_product_sale','mark_sales_rows_undone',
             'mark_sales_rows_undone_in_sheet','get_sales_sheet')}

    def sync_sales(self,namespace,limit=1):
        from group_sheet_queue import sync_pending_sales
        return sync_pending_sales(self.service,namespace,GROUP,limit)

    def sync_state(self):
        return sql("SELECT reason FROM group_credit_events WHERE kind='sale' ORDER BY created_at",fetch=True)

    def test_sheet_backlog_committed_with_sale_and_drained_once(self):
        sale=self.buy();ns=self.sheet_namespace()
        self.assertEqual(self.sync_state(),[('sheet:pending:v1',)])
        before=sql('SELECT count(*) FROM transactions',fetch=True)
        self.assertEqual(self.sync_sales(ns),dict(completed=1,failed=False))
        self.assertEqual(self.sync_state(),[('sheet:done:v1',)])
        self.assertEqual(self.sync_sales(ns),dict(completed=0,failed=False))
        ns['append_sales_rows'].assert_called_once()
        ns['mark_product_sale'].assert_called_once()
        self.assertEqual(self.debt(),900)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True),before)
        self.assertEqual(ns['append_sales_rows'].call_args.args[1][0]['order_no'],sale['items'][0]['order_no'])

    def test_sheet_backlog_rollback_does_not_publish_failed_sale(self):
        with self.assertRaises(CreditError):self.buy(qty=5)
        self.assertEqual(self.sync_state(),[])
        ns=self.sheet_namespace();self.sync_sales(ns)
        ns['append_sales_rows'].assert_not_called()

    def test_sheet_failure_retries_original_order_without_charging_again(self):
        self.buy();ns=self.sheet_namespace()
        ns['mark_product_sale'].return_value=False
        self.assertEqual(self.sync_sales(ns),dict(completed=0,failed=True))
        self.assertEqual(self.sync_state(),[('sheet:pending:v1',)])
        first=ns['append_sales_rows'].call_args
        ns['mark_product_sale'].return_value=True
        self.service=GroupCredit(connect,RealDictCursor,product,lambda:'NEVER_ALLOCATE',lambda t:[t])
        self.assertEqual(self.sync_sales(ns),dict(completed=1,failed=False))
        self.assertEqual(ns['append_sales_rows'].call_args,first)
        self.assertEqual(self.debt(),900)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],1)

    def test_sheet_exception_is_durable(self):
        self.buy();ns=self.sheet_namespace()
        ns['append_sales_rows'].side_effect=TimeoutError('fake transport')
        self.assertEqual(self.sync_sales(ns),dict(completed=0,failed=True))
        self.assertEqual(self.sync_state(),[('sheet:pending:v1',)])
        self.assertEqual(self.debt(),900)

    def test_sheet_mirror_cancelled_before_sync_preserves_history(self):
        sale=self.buy()
        prompt=self.service.request_cancel(GROUP,USER,'cancel',sale['order_no'],True)
        self.service.confirm_cancel(GROUP,USER,prompt['token'],True)
        ns=self.sheet_namespace()
        self.assertEqual(self.sync_sales(ns),dict(completed=1,failed=False))
        ns['append_sales_rows'].assert_called_once()
        ns['mark_product_sale'].assert_not_called()
        ns['mark_sales_rows_undone'].assert_called_once_with([sale['items'][0]['order_no']])
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='available'",fetch=True)[0][0],4)

    def test_sheet_cancelled_test_sale_only_updates_test_sheet(self):
        # Isolated fixture starts as a test account; production rebinding remains forbidden.
        sql("UPDATE group_credit_accounts SET customer='測試' WHERE group_id=%s",(GROUP,))
        sql("INSERT INTO products(code) VALUES ('test100')")
        sql("INSERT INTO serials(product_id,serial,status) SELECT id,'FakeTestCard','available' FROM products WHERE code='test100'")
        sale=self.service.buy(GROUP,USER,'test-sale',[dict(product_text='test100',quantity=1)])
        prompt=self.service.request_cancel(GROUP,USER,'cancel',sale['order_no'],True)
        self.service.confirm_cancel(GROUP,USER,prompt['token'],True)
        ns=self.sheet_namespace();self.sync_sales(ns)
        self.assertEqual(ns['append_sales_rows'].call_args.args[0],'測試')
        ns['mark_product_sale'].assert_not_called()
        ns['mark_sales_rows_undone'].assert_not_called()
        ns['mark_sales_rows_undone_in_sheet'].assert_called_once()
        self.assertEqual(self.debt(),0)

    def test_concurrent_sheet_workers_serialize_without_duplicate_write(self):
        self.buy();ns=self.sheet_namespace()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.sync_sales(ns),range(2)))
        self.assertEqual(sum(r['completed'] for r in results),1)
        ns['append_sales_rows'].assert_called_once()
        self.assertEqual(self.debt(),900)

    def test_sheet_retry_uses_original_taipei_date_after_midnight(self):
        self.buy();ns=self.sheet_namespace()
        sql("UPDATE transactions SET created_at='2026-11-03 16:05:00+00'")
        self.sync_sales(ns)
        self.assertEqual(ns['append_sales_rows'].call_args.args[1][0]['sale_date'],'2026/11/04')

    def test_sheet_rebinding_does_not_change_original_customer(self):
        self.buy()
        with self.assertRaises(CreditError):
            self.service.manage(GROUP,USER,'rebind','bind','測試',admin=True)
        ns=self.sheet_namespace();self.sync_sales(ns)
        self.assertEqual(ns['append_sales_rows'].call_args.args[0],'軒')

    def test_sheet_historical_orders_not_automatically_backfilled(self):
        self.buy();sql("UPDATE group_credit_events SET reason='' WHERE kind='sale'")
        ns=self.sheet_namespace();self.sync_sales(ns)
        ns['append_sales_rows'].assert_not_called()

    def fast_namespace(self):
        from types import SimpleNamespace as N
        from unittest.mock import MagicMock
        return dict(get_context_id=lambda e:GROUP,get_operator=lambda e:USER,is_admin=lambda u:False,
            get_db=MagicMock(side_effect=connect),RealDictCursor=RealDictCursor,find_product=product,
            create_order_no=lambda:'TX_FAST_'+uuid.uuid4().hex,split_text_chunks=lambda t:[t],reply=MagicMock(),
            parse_quick_sale_message=lambda t:dict(ok=True,items=[dict(product_text='MY1000',quantity=1)]))

    def test_fast_route_commits_actual_stock_and_debt_before_line(self):
        from types import SimpleNamespace as N
        from unittest.mock import patch
        from group_credit_handler import try_fast_group_sale
        ns=self.fast_namespace()
        def delivered(*args):
            self.assertEqual(self.debt(),900)
            self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='sold'",fetch=True)[0][0],1)
            return True
        with patch('group_credit_handler.deliver_sale',side_effect=delivered):
            self.assertTrue(try_fast_group_sale(N(message=N(text='/MY1000*1',id='fast')),ns))
        ns['get_db'].assert_called_once()
        self.assertEqual(self.sync_state(),[('sheet:pending:v1',)])

    def test_fast_route_duplicate_issues_one_order_only(self):
        from types import SimpleNamespace as N
        from unittest.mock import patch
        from group_credit_handler import try_fast_group_sale
        ns=self.fast_namespace();event=N(message=N(text='/MY1000*1',id='fast-duplicate'))
        with patch('group_credit_handler.deliver_sale',return_value=True) as delivery:
            try_fast_group_sale(event,ns);try_fast_group_sale(event,ns)
        delivery.assert_called_once()
        self.assertEqual(self.debt(),900)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],1)

    def test_fast_route_unapproved_member_cannot_get_cards(self):
        from types import SimpleNamespace as N
        from unittest.mock import patch
        from group_credit_handler import try_fast_group_sale
        ns=self.fast_namespace();ns['get_operator']=lambda e:'U'+'f'*32
        with patch('group_credit_handler.deliver_sale') as delivery:
            self.assertTrue(try_fast_group_sale(N(message=N(text='/MY1000*1',id='denied-fast')),ns))
        delivery.assert_not_called();self.assertEqual(self.debt(),0)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],0)

    def test_fast_route_concurrent_requests_share_the_original_cap_lock(self):
        from types import SimpleNamespace as N
        from unittest.mock import patch
        from group_credit_handler import try_fast_group_sale
        self.service.manage(GROUP,USER,'fast-cap','limit','900',admin=True)
        ns=self.fast_namespace()
        with patch('group_credit_handler.deliver_sale',return_value=True) as delivery:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda i:try_fast_group_sale(N(message=N(text='/MY1000*1',id='fast-'+str(i))),ns),range(2)))
        delivery.assert_called_once();self.assertEqual(self.debt(),900)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],1)

class DailyReportPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sql("""CREATE SCHEMA report_fixture;
            CREATE TABLE report_fixture.products(id integer PRIMARY KEY,code text,active boolean);
            CREATE TABLE report_fixture.serials(product_id integer,created_at timestamptz,status text);
            CREATE TABLE report_fixture.transactions(product_id integer,group_id text,quantity integer,
                sale_amount numeric,created_at timestamptz,cancelled boolean DEFAULT false);
            CREATE VIEW report_fixture.active_transactions AS
                SELECT * FROM report_fixture.transactions WHERE NOT cancelled;
            CREATE TABLE report_fixture.manual_sales(product_code text,group_id text,quantity integer,
                sale_amount numeric,sale_date date,state text);""")
    def setUp(self):
        sql("""TRUNCATE report_fixture.products,report_fixture.serials,
                report_fixture.transactions,report_fixture.manual_sales;
            INSERT INTO report_fixture.products VALUES (1,'MY1000',true),(2,'貝500',true),(3,'test100',true);
            INSERT INTO report_fixture.transactions VALUES
                (1,'management',2,1800,'2026-11-11 16:05:00+00',false),
                (1,'xuan',3,2700,'2026-11-11 17:00:00+00',false),
                (1,'other-group',1,NULL,'2026-11-11 18:00:00+00',false),
                (2,'fei',4,1790,'2026-11-11 19:00:00+00',false),
                (1,'xuan',99,89100,'2026-11-11 17:00:00+00',true),
                (1,'xuan',99,89100,'2026-11-11 15:59:59+00',false),
                (1,'xuan',99,89100,'2026-11-12 15:59:00+00',false),
                (3,'test-group',8,7240,'2026-11-11 19:00:00+00',false);
            INSERT INTO report_fixture.manual_sales VALUES
                ('MY1000','another-group',1,900,'2026-11-12','active'),
                ('service-only','another-group',2,200,'2026-11-12','active'),
                ('MY1000','another-group',99,89100,'2026-11-12','cancelled');
            INSERT INTO report_fixture.serials VALUES
                (1,'2026-11-11 16:00:00+00','available'),
                (1,'2026-11-11 17:00:00+00','sold'),
                (2,'2026-11-11 17:00:00+00','available');""")
    def report(self):
        from datetime import datetime
        from unittest.mock import patch
        from daily_report import read_report,TAIPEI
        real_connect=psycopg2.connect
        def isolated_connect(*args,**kwargs):
            return real_connect(DSN,options='-c search_path=report_fixture')
        with patch.dict(os.environ,DATABASE_URL='unused-isolated-fixture'),patch(
                'psycopg2.connect',side_effect=isolated_connect):
            return read_report(datetime(2026,11,12,23,59,tzinfo=TAIPEI))
    def test_all_groups_are_included_once_and_cancelled_sales_excluded(self):
        _,rows=self.report()
        by_code={r['code']:r for r in rows}
        self.assertEqual(by_code['MY1000']['outgoing'],7)
        self.assertEqual(by_code['MY1000']['sales_amount'],Decimal('5400'))
        self.assertEqual(by_code['MY1000']['unpriced_orders'],1)
        self.assertEqual(by_code['貝500']['outgoing'],4)
        self.assertEqual(by_code['service-only']['outgoing'],2)
        self.assertNotIn('test100',by_code)
        self.assertEqual(by_code['MY1000']['incoming'],2)
        self.assertEqual(by_code['MY1000']['available'],1)
    def test_report_labels_all_groups_and_keeps_unpriced_notice(self):
        from datetime import datetime
        from daily_report import format_report,TAIPEI
        start,rows=self.report()
        text=format_report(start,rows,datetime(2026,11,12,23,59,tzinfo=TAIPEI))
        self.assertIn('銷貨：所有群組',text)
        self.assertIn('NT$ 7,390.00',text)
        self.assertIn('另 1 筆未填售價',text)


if __name__=='__main__':unittest.main(verbosity=2)
