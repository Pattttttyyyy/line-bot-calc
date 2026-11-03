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
    def test_prepaid_or_already_adjusted_balance_rejected(self):
        sale,p=self.cancel_prompt()
        self.service.manage(GROUP,USER,'pay','payment','905','已收款',True)
        with self.assertRaises(CreditError):self.service.confirm_cancel(GROUP,USER,p['token'],True)
        self.assertEqual(self.debt(),0)
        self.assertEqual(sql('SELECT count(*) FROM active_transactions',fetch=True)[0][0],1)
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

if __name__=='__main__':unittest.main(verbosity=2)
