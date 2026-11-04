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
    def test_sale_and_case(self):
        r=self.buy();self.assertEqual(self.debt(),Decimal('905'));self.assertEqual(r['items'][0]['serials'][0],'FakeMixedCase_1\npinLower_1')
    def test_duplicate(self):
        self.buy();self.assertTrue(self.buy()['duplicate']);self.assertEqual(self.debt(),Decimal('905'))
    def test_concurrent_shared_cap(self):
        self.service.manage(GROUP,USER,'cap','limit','905',admin=True)
        def run(i):
            try:self.buy('sale'+str(i));return True
            except CreditError:return False
        with ThreadPoolExecutor(max_workers=2) as p:results=list(p.map(run,range(2)))
        self.assertEqual(sum(results),1);self.assertEqual(self.debt(),Decimal('905'))
    def test_concurrent_same_event(self):
        with ThreadPoolExecutor(max_workers=2) as p:results=list(p.map(lambda _:self.buy(),range(2)))
        self.assertEqual(sum(not r['duplicate'] for r in results),1);self.assertEqual(self.debt(),Decimal('905'))
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
        self.assertIn('FakeMixedCase_1',again['serial_chunks'][0]);self.assertEqual(self.debt(),905)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],1)
    def test_uncertain_delivery(self):
        self.buy();self.service.delivery(GROUP,'sale','uncertain');self.assertEqual(self.debt(),905)
        self.assertEqual(sql("SELECT delivery_status FROM group_credit_events WHERE kind='sale'",fetch=True)[0][0],'uncertain')

    def cancel_prompt(self):
        sale=self.buy()
        return sale,self.service.request_cancel(GROUP,USER,'request-cancel',sale['order_no'],True)
    def test_cancel_prompt_does_not_recover_or_refund(self):
        sale,prompt=self.cancel_prompt()
        self.assertEqual(self.debt(),905)
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
        self.assertEqual(self.debt(),905)
    def test_confirmation_refunds_recovers_and_preserves_order(self):
        sale,p=self.cancel_prompt()
        r=self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(r['amount'],905);self.assertEqual(self.debt(),0)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],1)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],0)
        self.assertEqual(sql("SELECT count(*) FROM serials WHERE status='available'",fetch=True)[0][0],4)
        self.assertEqual(sql('SELECT confirmed_by FROM group_credit_cancelled_orders',fetch=True)[0][0],USER)
        self.assertEqual(sql("SELECT amount FROM group_credit_events WHERE kind='cancel'",fetch=True)[0][0],-905)
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
        self.assertEqual(self.debt(),905)
    def test_cancelled_prompt_has_no_effect(self):
        sale,p=self.cancel_prompt()
        self.assertTrue(self.service.confirm_cancel(GROUP,USER,p['token'],True,True)['declined'])
        with self.assertRaises(CreditError):self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),905)
    def test_cross_group_button_rejected(self):
        sale,p=self.cancel_prompt();other='group:C'+'d'*32
        self.service.manage(other,USER,'bind-other','bind','軒',admin=True)
        with self.assertRaises(CreditError):self.service.confirm_cancel(other,USER,p['token'],True)
        self.assertEqual(sql("SELECT debt FROM group_credit_accounts WHERE group_id=%s",(GROUP,),True)[0][0],905)
    def test_missing_original_order_rejected(self):
        with self.assertRaises(CreditError):self.service.request_cancel(GROUP,USER,'r','TX_NONEXISTENT',True)
    def test_child_order_resolves_entire_parent(self):
        sale=self.buy();p=self.service.request_cancel(GROUP,USER,'r',sale['items'][0]['order_no'],True)
        self.assertEqual(p['order_no'],sale['order_no'])
    def test_mismatched_card_rolls_back(self):
        sale,p=self.cancel_prompt()
        sql("UPDATE serials SET sold_to='tampered' WHERE status='sold'")
        with self.assertRaises(CreditError):self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),905)
        self.assertEqual(sql('SELECT count(*) FROM group_credit_cancelled_orders',fetch=True)[0][0],0)
    def test_paid_card_refund_returns_prepaid_exactly_once(self):
        sale,p=self.cancel_prompt()
        self.service.manage(GROUP,USER,'pay','payment','905','已收款',True)
        self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertTrue(self.service.confirm_cancel(GROUP,USER,p['token'],True)['duplicate'])
        self.assertEqual(self.debt(),-905)
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
        self.assertEqual(self.debt(),905)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],1)
        self.assertEqual(sql('SELECT count(*) FROM transactions',fetch=True)[0][0],2)
        with self.assertRaises(CreditError):self.service.resend(GROUP,USER,'resend-old',sale['order_no'],True)
    def test_ledger_failure_rolls_back_cards_debt_and_cancel_records(self):
        sale,p=self.cancel_prompt()
        sql("ALTER TABLE group_credit_events ADD CONSTRAINT reject_cancel_test CHECK(kind<>'cancel')")
        try:
            with self.assertRaises(psycopg2.IntegrityError):self.service.confirm_cancel(GROUP,USER,p['token'],True)
            self.assertEqual(self.debt(),905)
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
        self.assertEqual(sql('SELECT sum(quantity),sum(sale_amount) FROM active_transactions',fetch=True)[0],(2,Decimal('1810')))
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
        self.assertEqual(r['kind'],'sale');self.assertEqual(self.debt(),905)
        self.assertTrue(store.confirm(GROUP,USER,token,False)['duplicate'])
        self.assertEqual(self.debt(),905)
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
        self.assertEqual(self.debt(),1810)
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
        self.assertEqual(r['previous_debt'],905);self.assertEqual(r['debt'],405)
        self.assertEqual(self.service.calculate(GROUP,USER,'query','+0')['debt'],405)
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
        self.assertEqual(self.debt(),1405)
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
        self.assertEqual(self.debt(),-4570)
        self.assertEqual(sql("SELECT count(*) FROM group_credit_events WHERE kind='calculator'",fetch=True)[0][0],2)
    def test_calculator_manual_charge_can_record_over_cap_but_sale_is_blocked(self):
        self.service.manage(GROUP,USER,'lowcap','limit','500',admin=True)
        self.service.calculate(GROUP,USER,'charge','-905',True)
        with self.assertRaises(CreditError):self.buy()
        self.assertEqual(self.debt(),905)
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
        self.buy()
        with self.assertRaises(CreditError):request_reverse(self.service,GROUP,USER,'undo',None,'撤回',True)
        self.assertEqual(self.debt(),1005)
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
        self.service.calculate(GROUP,USER,'cash','+1810',True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda i:self.buy('prepaid-sale'+str(i)),range(2)))
        self.assertEqual(self.debt(),0)
        with self.assertRaises(CreditError):self.buy('exceeds')
        self.assertEqual(sql("SELECT sum(sale_amount) FROM transactions",fetch=True)[0][0],1810)

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

if __name__=='__main__':unittest.main(verbosity=2)
