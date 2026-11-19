"""Credential-free regression tests for lost legacy inventory replies."""
import ast
from pathlib import Path
from types import SimpleNamespace as N
import unittest
from unittest.mock import MagicMock, patch
from legacy_sale_delivery import deliver_committed_sale, handle_sale_recovery, original_sale

ORDER = 'TX202611200006086CA48B'
CHILD = ORDER + '-01'
CONTEXT = 'group:FAKE'


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.event = N(message=N(text='/補發序號 ' + ORDER))
        self.items = [dict(product=dict(id=1, code='貝2990'), quantity=2,
                           order_no=CHILD, serials=['FAKE1', 'FAKE2'])]
        self.ns = dict(reply_messages=MagicMock(), append_sales_rows=MagicMock(return_value=True),
                       mark_product_sale=MagicMock(return_value=True), reply=MagicMock(),
                       is_admin=MagicMock(return_value=True), get_operator=lambda e:'FAKE_USER',
                       get_context_id=lambda e:CONTEXT, split_text_chunks=lambda s:[s])

    def send(self):
        return deliver_committed_sale(self.event, self.ns, '飛', self.items,
                                      ['FAKE1\nFAKE2'], ORDER)

    def test_reply_precedes_both_google_operations(self):
        sequence=[]
        for name in ('reply_messages','append_sales_rows','mark_product_sale'):
            self.ns[name].side_effect=lambda *a, name=name:sequence.append(name) or True
        self.assertTrue(self.send())
        self.assertEqual(sequence,['reply_messages','append_sales_rows','mark_product_sale'])

    def test_each_google_failure_cannot_suppress_or_repeat_cards(self):
        for name in ('append_sales_rows','mark_product_sale'):
            with self.subTest(name=name):
                self.setUp()
                self.ns[name].side_effect=TimeoutError('DO_NOT_LOG_CARD_OR_SECRET')
                with patch('builtins.print') as log:
                    self.assertTrue(self.send())
                self.ns['reply_messages'].assert_called_once()
                self.ns['append_sales_rows'].assert_called_once()
                self.ns['mark_product_sale'].assert_called_once()
                self.assertNotIn('DO_NOT_LOG',str(log.call_args_list))

    def test_ambiguous_line_failure_never_retries_reply_and_still_mirrors(self):
        self.ns['reply_messages'].side_effect=TimeoutError('DO_NOT_LOG')
        with patch('builtins.print') as log:
            self.assertFalse(self.send())
        self.ns['reply_messages'].assert_called_once()
        self.ns['reply'].assert_not_called()
        self.ns['append_sales_rows'].assert_called_once()
        self.ns['mark_product_sale'].assert_called_once()
        self.assertNotIn('DO_NOT_LOG',str(log.call_args_list))

    def test_non_admin_denied_before_any_original_card_lookup(self):
        self.ns['is_admin'].return_value=False
        with patch('legacy_sale_delivery.original_sale') as lookup:
            self.assertTrue(handle_sale_recovery(self.event,self.ns))
        lookup.assert_not_called();self.ns['reply_messages'].assert_not_called()

    def test_unrelated_command_falls_through(self):
        self.event.message.text='/飛 貝2990*20'
        self.assertFalse(handle_sale_recovery(self.event,self.ns))

    def test_resend_uses_same_context_and_only_original_serials(self):
        with patch('legacy_sale_delivery.original_sale',return_value=('飛',self.items)) as lookup:
            self.assertTrue(handle_sale_recovery(self.event,self.ns))
        lookup.assert_called_once_with(self.ns,CONTEXT,ORDER)
        messages=self.ns['reply_messages'].call_args.args[1]
        self.assertIn('FAKE1',messages[0]);self.assertIn('FAKE2',messages[0])
        self.assertIn('不另扣庫存',messages[-1])

    def test_sync_only_never_sends_cards(self):
        self.event.message.text='/同步出庫 '+ORDER
        with patch('legacy_sale_delivery.original_sale',return_value=('飛',self.items)):
            self.assertTrue(handle_sale_recovery(self.event,self.ns))
        self.ns['reply_messages'].assert_not_called()
        self.ns['append_sales_rows'].assert_called_once()
        self.assertNotIn('FAKE1',self.ns['reply'].call_args.args[1])

    def test_missing_or_recalled_order_never_sends(self):
        with patch('legacy_sale_delivery.original_sale',side_effect=ValueError('已撤回')):
            self.assertTrue(handle_sale_recovery(self.event,self.ns))
        self.ns['reply_messages'].assert_not_called()
        self.ns['append_sales_rows'].assert_not_called()


class OriginalOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn=MagicMock()
        self.cur=self.conn.cursor.return_value.__enter__.return_value
        self.ns=dict(get_db=MagicMock(return_value=self.conn),RealDictCursor=object)
        self.row=dict(order_no=CHILD,sale_batch_no=ORDER,quantity=2,sold_to='飛',
                      unit_price=None,sale_amount=None,product_id=1,code='貝2990',sale_date='2026/11/20')
        self.cur.fetchall.side_effect=[[self.row],[dict(serial='FAKE1'),dict(serial='FAKE2')]]
        self.cur.fetchone.return_value=(1,)

    def test_only_same_chat_sold_original_cards_without_writes(self):
        customer,items=original_sale(self.ns,CONTEXT,ORDER)
        self.assertEqual(customer,'飛');self.assertEqual(items[0]['sale_date'],'2026/11/20')
        self.assertEqual(items[0]['serials'],['FAKE1','FAKE2'])
        calls=self.cur.execute.call_args_list
        self.assertEqual(calls[0].args[1],(CONTEXT,ORDER,ORDER))
        self.assertEqual(calls[1].args[1],(CONTEXT,ORDER))
        self.assertEqual(calls[2].args[1],(CHILD,1,'飛'))
        self.assertTrue(all(c.args[0].lstrip().startswith('SELECT') for c in calls))
        self.conn.commit.assert_not_called();self.conn.rollback.assert_called_once();self.conn.close.assert_called_once()

    def test_cross_chat_or_nonexistent_order_denied(self):
        self.cur.fetchall.side_effect=[[]]
        with self.assertRaises(ValueError):original_sale(self.ns,CONTEXT,ORDER)
        self.assertEqual(self.cur.execute.call_count,1)

    def test_no_legacy_sale_marker_denied(self):
        self.cur.fetchone.return_value=None
        with self.assertRaises(ValueError):original_sale(self.ns,CONTEXT,ORDER)

    def test_recalled_or_incomplete_cards_denied(self):
        self.cur.fetchall.side_effect=[[self.row],[dict(serial='FAKE1')]]
        with self.assertRaises(ValueError):original_sale(self.ns,CONTEXT,ORDER)
        self.conn.commit.assert_not_called();self.conn.close.assert_called_once()

    def test_invalid_order_never_opens_db(self):
        for value in ('',"' OR 1=1",ORDER+'xxx','TX_FAKE'):
            with self.assertRaises(ValueError):original_sale(self.ns,CONTEXT,value)
        self.ns['get_db'].assert_not_called()


class RouterTests(unittest.TestCase):
    def test_both_sale_routes_deliver_without_preceding_google_work(self):
        tree=ast.parse((Path(__file__).parent/'app.py').read_text())
        handler=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='handle_message')
        for name in ('sell_multiple_items','sell_serials'):
            branch=next(n for n in ast.walk(handler) if isinstance(n,ast.If) and
                        any(isinstance(x,ast.Call) and isinstance(x.func,ast.Name) and x.func.id==name
                            for x in ast.walk(ast.Module(body=n.body,type_ignores=[]))))
            # The smallest branch containing allocation has one delivery and no Sheet calls.
            branches=[n for n in ast.walk(handler) if isinstance(n,ast.If) and
                      any(isinstance(x,ast.Call) and isinstance(x.func,ast.Name) and x.func.id==name
                          for x in ast.walk(ast.Module(body=n.body,type_ignores=[])))]
            branch=min(branches,key=lambda n:len(list(ast.walk(n))))
            names=[n.func.id for n in ast.walk(branch) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)]
            self.assertEqual(names.count('deliver_committed_sale'),1)
            self.assertNotIn('append_sales_rows',names);self.assertNotIn('mark_product_sale',names)
