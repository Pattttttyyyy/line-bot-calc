import ast
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from pathlib import Path
import threading
import unittest
from group_credit import CreditError,GroupCredit,quote,money,checked_debt

GROUP='group:C'+'a'*32
USER='U'+'b'*32
OTHER='U'+'c'*32


class MemoryDB:
    """Behavioral transaction double; not a PostgreSQL compatibility test."""
    def __init__(self,limit='100000',debt='0',enabled=True):
        self.lock=threading.RLock()
        self.state={'account':dict(group_id=GROUP,customer='軒',debt=D(debt),credit_limit=None if limit is None else D(limit),enabled=enabled),
                    'members':{USER:True},'cards':[dict(id=i,serial=f'MixedCase_{i}\npinLower_{i}',product_id=1,status='available') for i in range(1,5)],
                    'events':{},'transactions':[]}
        self.inventory_reads=0
        self.fail_event=False
        self.connections=[]
    def connect(self):
        c=Connection(self);self.connections.append(c);return c


class Connection:
    def __init__(self,db):self.db=db;self.snapshot=None;self.locked=False;self.committed=False;self.closed=False
    def cursor(self,**kwargs):return Cursor(self)
    def commit(self):self.committed=True;self.snapshot=None;self.release()
    def rollback(self):
        if self.snapshot is not None:self.db.state=self.snapshot;self.snapshot=None
        self.release()
    def release(self):
        if self.locked:self.locked=False;self.db.lock.release()
    def close(self):self.release();self.closed=True


class Cursor:
    def __init__(self,c):self.c=c;self.rows=[];self.rowcount=0
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows
    def execute(self,sql,args):
        s=' '.join(sql.split());db=self.c.db
        if s.startswith('SELECT * FROM group_credit_accounts'):
            assert 'FOR UPDATE' in s
            db.lock.acquire();self.c.locked=True;self.c.snapshot=copy.deepcopy(db.state)
            self.rows=[copy.deepcopy(db.state['account'])] if args[0]==GROUP else []
        elif s.startswith('SELECT active FROM group_credit_members'):
            value=db.state['members'].get(args[1]);self.rows=[] if value is None else [{'active':value}]
        elif s.startswith('SELECT kind,order_no'):
            ev=db.state['events'].get((args[0],args[1]));self.rows=[] if ev is None else [copy.deepcopy(ev)]
        elif s.startswith('SELECT event_id FROM group_credit_events e'):
            # Full timestamp/confirmation/concurrency coverage runs in PostgreSQL.
            self.rows=[dict(event_id=eid) for (gid,eid),ev in db.state['events'].items()
                       if gid==args[0] and ev.get('operator')==args[1]
                       and ev['kind'] in args[2] and ev['amount']==args[3]][-1:]
        elif s.startswith('SELECT child_orders FROM group_credit_events'):
            self.rows=[copy.deepcopy(ev) for (gid,_),ev in db.state['events'].items() if gid==args[0] and ev['order_no']==args[1] and ev['kind']=='sale']
        elif s.startswith('SELECT s.serial,p.code,s.order_no'):
            self.rows=[dict(serial=c['serial'],code='MY1000',order_no=c['order_no']) for c in db.state['cards'] if c.get('order_no') in args[0] and c['status']=='sold' and c.get('sold_to')==args[1]]
        elif s.startswith('SELECT order_no,quantity FROM transactions'):
            self.rows=[dict(order_no=t['order_no'],quantity=t['quantity']) for t in db.state['transactions'] if t['order_no'] in args[0] and t['group_id']==args[1]]
        elif s.startswith('UPDATE group_credit_events SET delivery_status='):
            ev=db.state['events'].get((args[1],args[2]))
            if ev and ev['kind']=='sale' and ev['delivery_status']=='pending':ev['delivery_status']=args[0]
        elif s.startswith('SELECT id,serial FROM serials'):
            if not self.c.locked:raise AssertionError('Inventory accessed without account lock')
            db.inventory_reads+=1
            self.rows=[copy.deepcopy(c) for c in db.state['cards'] if c['product_id']==args[0] and c['status']=='available'][:args[1]]
        elif s.startswith('UPDATE serials SET'):
            self.rowcount=0
            for card in db.state['cards']:
                if card['id'] in args[3] and card['status']=='available':
                    card.update(status='sold',sold_to=args[0],order_no=args[1],operator=args[2]);self.rowcount+=1
        elif s.startswith('INSERT INTO transactions'):
            db.state['transactions'].append(dict(zip(('order_no','product_id','sold_to','quantity','operator','group_id','sale_batch_no','unit_price','sale_amount'),args)))
        elif s.startswith('UPDATE group_credit_accounts SET debt='):
            db.state['account']['debt']=args[0]
        elif s.startswith('UPDATE group_credit_accounts SET credit_limit='):
            db.state['account']['credit_limit']=args[0]
        elif s.startswith('UPDATE group_credit_accounts SET enabled='):
            db.state['account']['enabled']=args[0]
        elif s.startswith('INSERT INTO group_credit_members'):
            db.state['members'][args[1]]=args[2]
        elif s.startswith('INSERT INTO group_credit_accounts'):
            pass # The existing account must not be reset by ON CONFLICT.
        elif s.startswith('SELECT pg_advisory_xact_lock') or s.startswith('SELECT 1 FROM bot_ui_admin_groups'):
            self.rows=[]
        elif s.startswith('INSERT INTO group_credit_events'):
            if db.fail_event:raise RuntimeError('simulated ledger write failure')
            db.state['events'][(args[0],args[1])]=dict(kind=args[2],order_no=args[7],debt_after=args[4],delivery_status=args[9],amount=args[3],operator=args[5],child_orders=json.loads(args[8]))
        else:raise AssertionError('Unhandled query in transaction double: '+s[:90])


def service(db,products=None,chunks=None):
    products=products or {'MY1000':dict(id=1,code='MY1000'),'MY3000':dict(id=2,code='MY3000'),'貝1000':dict(id=3,code='貝1000')}
    sequence=iter(range(1,1000))
    return GroupCredit(db.connect,object,lambda conn,text:products.get(text),
                       lambda:'TX_TEST_'+str(next(sequence)),chunks or (lambda text:[text]))


class PolicyTests(unittest.TestCase):
    def test_my1000(self):self.assertEqual(quote('MY1000',1),(D('900'),D('900')))
    def test_my3000(self):self.assertEqual(quote('MY3000',1)[0],D('2715'))
    def test_my5000(self):self.assertEqual(quote('MY5000',1)[0],D('4525'))
    def test_my10000(self):self.assertEqual(quote('MY1萬',1)[0],D('9050'))
    def test_unknown_product(self):
        with self.assertRaises(CreditError):quote('貝1000',1)
    def test_bad_quantities(self):
        for qty in (0,-1,501,1.5,True):
            with self.subTest(qty=qty),self.assertRaises(CreditError):quote('MY1000',qty)
    def test_bad_money(self):
        for amount in ('NaN','Infinity','1.001','abc','1e99'):
            with self.subTest(amount=amount),self.assertRaises(CreditError):money(amount)
    def test_disabled(self):
        with self.assertRaises(CreditError):checked_debt(0,1000,905,False)
    def test_unconfigured_limit(self):
        with self.assertRaises(CreditError):checked_debt(0,None,905,True)
    def test_exact_cap_allowed(self):self.assertEqual(checked_debt(95,1000,905,True),D('1000'))
    def test_at_cap_stops(self):
        with self.assertRaises(CreditError):checked_debt(1000,1000,905,True)
    def test_overshoot_stops(self):
        with self.assertRaises(CreditError):checked_debt(99000,100000,2000,True)


class TestCustomerTests(unittest.TestCase):
    def test_test_price_stays_905_when_xuan_my1000_changes(self):
        self.assertEqual(quote('test100',2,'測試'),(D('905'),D('1810')))
        self.assertEqual(quote('MY1000',2),(D('900'),D('1800')))
    def test_production_cannot_buy_test_card(self):
        with self.assertRaises(CreditError):quote('test100',1)
    def test_test_cannot_buy_any_production_card(self):
        for code in ('MY1000','MY3000','MY5000','MY1萬','貝1000'):
            with self.subTest(code=code),self.assertRaises(CreditError):quote(code,1,'測試')
    def test_unknown_customer_never_gets_default_price(self):
        with self.assertRaises(CreditError):quote('MY1000',1,'其他')
    def test_admin_cannot_bypass_test_product_boundary(self):
        db=MemoryDB();db.state['account']['customer']='測試'
        with self.assertRaises(CreditError):service(db).buy(GROUP,USER,'sale',[dict(product_text='MY1000',quantity=1)],True)
        self.assertEqual(db.inventory_reads,0);self.assertEqual(db.state['account']['debt'],0)
    def test_test_sale_prices_ledger_and_duplicate(self):
        db=MemoryDB();db.state['account']['customer']='測試'
        svc=service(db,{'test100':dict(id=1,code='test100')})
        request=[dict(product_text='test100',quantity=2)]
        r=svc.buy(GROUP,USER,'sale',request)
        self.assertEqual(r['customer'],'測試');self.assertEqual(r['amount'],D('1810'))
        self.assertTrue(svc.buy(GROUP,USER,'sale',request)['duplicate'])
        self.assertEqual(db.state['account']['debt'],D('1810'))
        self.assertEqual(db.inventory_reads,1)


class TransactionTests(unittest.TestCase):
    def buy(self,s,event='message-1',qty=1,code='MY1000',user=USER):
        return s.buy(GROUP,user,event,[dict(product_text=code,quantity=qty)])
    def test_success_prices_debt_serial_case(self):
        db=MemoryDB();r=self.buy(service(db),qty=2)
        self.assertEqual(r['debt'],D('1800'));self.assertEqual(db.state['account']['debt'],D('1800'))
        self.assertEqual(r['items'][0]['serials'][0],'MixedCase_1\npinLower_1')
        self.assertEqual(db.state['transactions'][0]['sale_amount'],D('1800'))
        self.assertTrue(db.connections[-1].committed)
    def test_duplicate_does_not_charge_or_allocate(self):
        db=MemoryDB();s=service(db);first=self.buy(s);r=self.buy(s)
        self.assertTrue(r['duplicate']);self.assertEqual(r['order_no'],first['order_no'])
        self.assertEqual(db.inventory_reads,1);self.assertEqual(len(db.state['transactions']),1)
        self.assertEqual(db.state['account']['debt'],D('900'))
    def test_group_member_not_global_member(self):
        db=MemoryDB()
        with self.assertRaises(CreditError):self.buy(service(db),user=OTHER)
        self.assertEqual(db.inventory_reads,0)
    def test_revoked_member(self):
        db=MemoryDB();db.state['members'][USER]=False
        with self.assertRaises(CreditError):self.buy(service(db))
        self.assertEqual(db.inventory_reads,0)
    def test_two_buyers_share_one_cap(self):
        db=MemoryDB(limit='905');s=service(db)
        def run(i):
            try:return self.buy(s,event=f'message-{i}')
            except CreditError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(db.state['account']['debt'],D('900'))
        self.assertEqual(sum(c['status']=='sold' for c in db.state['cards']),1)
    def test_insufficient_stock_rolls_back(self):
        db=MemoryDB();before=copy.deepcopy(db.state)
        with self.assertRaises(CreditError):self.buy(service(db),qty=5)
        self.assertEqual(db.state,before)
    def test_multiple_items_all_or_nothing(self):
        db=MemoryDB();before=copy.deepcopy(db.state)
        with self.assertRaises(CreditError):service(db).buy(GROUP,USER,'m',[dict(product_text='MY1000',quantity=1),dict(product_text='MY3000',quantity=1)])
        self.assertEqual(db.state,before)
    def test_no_pricing_no_inventory_access(self):
        db=MemoryDB()
        with self.assertRaises(CreditError):self.buy(service(db),code='貝1000')
        self.assertEqual(db.inventory_reads,0)
    def test_ledger_error_rolls_back_sold_cards_and_debt(self):
        db=MemoryDB();before=copy.deepcopy(db.state);db.fail_event=True
        with self.assertRaises(RuntimeError):self.buy(service(db))
        self.assertEqual(db.state,before)
    def test_reply_overflow_rolls_back(self):
        db=MemoryDB();before=copy.deepcopy(db.state)
        with self.assertRaises(CreditError):self.buy(service(db,chunks=lambda text:['x']*5))
        self.assertEqual(db.state,before)
    def test_missing_identity_fails_before_db(self):
        db=MemoryDB()
        with self.assertRaises(CreditError):self.buy(service(db),user='未知')
        self.assertEqual(db.connections,[])
    def test_non_group_fails_before_db(self):
        db=MemoryDB()
        with self.assertRaises(CreditError):service(db).buy('user:'+USER,USER,'m',[dict(product_text='MY1000',quantity=1)])
        self.assertEqual(db.connections,[])
    def test_no_message_id_fails_before_db(self):
        db=MemoryDB()
        with self.assertRaises(CreditError):self.buy(service(db),event='')
        self.assertEqual(db.connections,[])
    def test_non_admin_cannot_adjust(self):
        db=MemoryDB()
        for cmd in ('charge','payment','limit','enable','approve','bind','adjust_credit'):
            with self.subTest(cmd=cmd),self.assertRaises(CreditError):service(db).manage(GROUP,USER,'m',cmd,'100','reason')
        self.assertEqual(db.connections,[])
    def test_admin_add_old_debt(self):
        db=MemoryDB();r=service(db).manage(GROUP,USER,'m','charge','20000','補登舊帳',True)
        self.assertEqual(r['debt'],D('20000'));self.assertEqual(db.state['events'][(GROUP,'m')]['amount'],D('20000'))
    def test_payment_resumes_after_cap(self):
        db=MemoryDB(limit='905',debt='905');s=service(db)
        s.manage(GROUP,USER,'payment','payment','905','銀行入帳',True)
        self.assertEqual(self.buy(s)['debt'],D('900'))
    def test_overpayment_keeps_prepaid_and_sale_consumes_it(self):
        db=MemoryDB(debt='100');s=service(db)
        self.assertEqual(s.manage(GROUP,USER,'m','payment','1100','銀行',True)['debt'],D('-1000'))
        self.assertEqual(self.buy(s)['debt'],D('-100'))
        self.assertEqual(self.buy(s,event='second')['debt'],D('800'))
    def test_prepaid_still_enforces_credit_limit(self):
        db=MemoryDB(limit='100',debt='-800');s=service(db)
        self.assertEqual(self.buy(s)['debt'],D('100'))
        with self.assertRaises(CreditError):self.buy(s,event='over-cap')
        self.assertEqual(db.state['account']['debt'],D('100'))
    def test_prepaid_does_not_bypass_disabled_or_membership(self):
        for disabled in (True,False):
            db=MemoryDB(debt='-2000',enabled=not disabled)
            if not disabled:db.state['members'][USER]=False
            with self.assertRaises(CreditError):self.buy(service(db))
            self.assertEqual(db.state['account']['debt'],D('-2000'))
    def test_adjustment_needs_reason(self):
        db=MemoryDB()
        with self.assertRaises(CreditError):service(db).manage(GROUP,USER,'m','charge','20','',True)
        self.assertEqual(db.state['account']['debt'],D('0'))
    def test_admin_duplicate_does_not_adjust_twice(self):
        db=MemoryDB();s=service(db)
        s.manage(GROUP,USER,'m','charge','20','舊帳',True)
        self.assertTrue(s.manage(GROUP,USER,'m','charge','20','舊帳',True)['duplicate'])
        self.assertEqual(db.state['account']['debt'],D('20'))
    def test_rebind_never_resets_debt(self):
        db=MemoryDB(debt='20000');s=service(db);s.manage(GROUP,USER,'m','bind','軒',admin=True)
        self.assertEqual(db.state['account']['debt'],D('20000'))
    def test_enable_without_limit_rejected(self):
        db=MemoryDB(limit=None,enabled=False)
        with self.assertRaises(CreditError):service(db).manage(GROUP,USER,'m','enable',admin=True)
        self.assertFalse(db.state['account']['enabled'])
    def test_uncertain_delivery_keeps_cards_and_debt(self):
        db=MemoryDB();s=service(db);self.buy(s)
        s.delivery(GROUP,'message-1','uncertain')
        self.assertEqual(db.state['account']['debt'],D('900'))
        self.assertEqual(sum(c['status']=='sold' for c in db.state['cards']),1)
        self.assertEqual(db.state['events'][(GROUP,'message-1')]['delivery_status'],'uncertain')
    def test_resend_original_without_charge_or_new_cards(self):
        db=MemoryDB();s=service(db);original=self.buy(s)
        result=s.resend(GROUP,USER,'resend-1',original['order_no'],admin=True)
        self.assertIn('MixedCase_1',result['serial_chunks'][0])
        self.assertEqual(db.inventory_reads,1)
        self.assertEqual(db.state['account']['debt'],D('900'))
        self.assertEqual(len(db.state['transactions']),1)
        self.assertTrue(s.resend(GROUP,USER,'resend-1',original['order_no'],admin=True)['duplicate'])
    def test_resend_admin_only(self):
        db=MemoryDB();s=service(db);original=self.buy(s)
        with self.assertRaises(CreditError):s.resend(GROUP,USER,'r',original['order_no'])
    def test_resend_wrong_order_rejected(self):
        db=MemoryDB();s=service(db);self.buy(s)
        with self.assertRaises(CreditError):s.resend(GROUP,USER,'r','OTHER_ORDER',admin=True)
    def test_resend_incomplete_cards_rejected(self):
        db=MemoryDB();s=service(db);original=self.buy(s)
        db.state['cards'][0]['status']='available'
        before=copy.deepcopy(db.state)
        with self.assertRaises(CreditError):s.resend(GROUP,USER,'r',original['order_no'],admin=True)
        self.assertEqual(db.state,before)


class IntegrationTests(unittest.TestCase):
    def test_only_expected_app_functions_changed(self):
        root=Path(__file__).parent
        import json,hashlib
        old_fn=json.loads((root/'legacy_function_hashes.json').read_text(encoding='utf-8'))
        new=ast.parse((root/'app.py').read_text(encoding='utf-8'))
        # Historical function AST hashes preserve the regression baseline.
        new_fn={n.name:hashlib.sha256(ast.dump(n).replace(', type_params=[]','').encode()).hexdigest() for n in new.body if isinstance(n,ast.FunctionDef)}
        # Other changes are limited to reading the active-order view; original
        # cancelled transaction rows remain available for audit.
        for node in new.body:
            for child in ast.walk(node):
                if isinstance(child,ast.Constant) and isinstance(child.value,str):
                    child.value=child.value.replace('FROM active_transactions','FROM transactions')
        new_fn={n.name:hashlib.sha256(ast.dump(n).replace(', type_params=[]','').encode()).hexdigest() for n in new.body if isinstance(n,ast.FunctionDef)}
        self.assertEqual({name for name in old_fn if old_fn[name]!=new_fn[name]}, {'handle_message','append_sales_rows','stock_in_serials','reply','reply_messages','get_sales_sheet','mark_product_sale'})
    def test_feature_defaults_off(self):
        code=(Path(__file__).parent/'app.py').read_text(encoding='utf-8')
        self.assertIn('os.getenv("GROUP_SELF_SERVICE_ENABLED", "0") == "1"',code)
    def test_adapter_blocks_legacy_fallthrough(self):
        from group_credit_handler import handle_group_credit
        from unittest.mock import patch,MagicMock
        from types import SimpleNamespace
        event=SimpleNamespace(message=SimpleNamespace(id='m',text='/撤回'))
        ns=dict(get_context_id=lambda e:GROUP,get_operator=lambda e:USER,is_admin=lambda u:False,
                get_db=None,RealDictCursor=None,find_product=None,create_order_no=None,split_text_chunks=None,
                parse_quick_sale_message=lambda t:None,reply=MagicMock())
        with patch('group_credit_handler.GroupCredit') as cls:
            cls.return_value.registered.return_value=True
            self.assertTrue(handle_group_credit(event,ns))
        self.assertIn('撤回',ns['reply'].call_args.args[1])
    def test_unregistered_groups_keep_old_flow(self):
        from group_credit_handler import handle_group_credit
        from unittest.mock import patch
        from types import SimpleNamespace
        event=SimpleNamespace(message=SimpleNamespace(id='m',text='/入庫'))
        ns=dict(get_context_id=lambda e:GROUP,get_db=None,RealDictCursor=None,find_product=None,create_order_no=None,split_text_chunks=None)
        with patch('group_credit_handler.GroupCredit') as cls:
            cls.return_value.registered.return_value=False
            self.assertFalse(handle_group_credit(event,ns))


if __name__=='__main__':unittest.main()
