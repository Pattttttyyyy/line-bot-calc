import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import MagicMock,patch
from group_credit import CreditError
from group_credit_handler import handle_group_credit,handle_group_credit_postback
from group_credit_sheet import clear_recovered_product_sales

GROUP='group:C'+'a'*32
USER='U'+'b'*32
TOKEN='c'*32
def namespace():
    return dict(get_context_id=lambda e:GROUP,get_operator=lambda e:USER,is_admin=lambda u:True,
                get_db=MagicMock(),RealDictCursor=object,find_product=MagicMock(),
                create_order_no=MagicMock(),split_text_chunks=MagicMock(),reply=MagicMock(),
                mark_sales_rows_undone=MagicMock(return_value=True))

class AdapterTests(unittest.TestCase):
    def test_command_only_requests_confirmation(self):
        ns=namespace();e=SimpleNamespace(message=SimpleNamespace(text='/自助撤回 TX_TEST',id='message'))
        with patch('group_credit_handler.GroupCredit') as cls,patch('group_credit_handler.reply_cancel_prompt') as prompt:
            self.assertTrue(handle_group_credit(e,ns))
            cls.return_value.request_cancel.assert_called_once_with(GROUP,USER,'message','TX_TEST',True)
            cls.return_value.confirm_cancel.assert_not_called()
            prompt.assert_called_once()
    def test_no_order_no_request(self):
        ns=namespace();e=SimpleNamespace(message=SimpleNamespace(text='/自助撤回',id='m'))
        with patch('group_credit_handler.GroupCredit') as cls:
            self.assertTrue(handle_group_credit(e,ns))
            cls.return_value.request_cancel.assert_not_called()
    def test_other_postbacks_do_not_open_database(self):
        ns=namespace()
        self.assertFalse(handle_group_credit_postback(SimpleNamespace(postback=SimpleNamespace(data='other')),ns))
        ns['get_db'].assert_not_called()
    def test_permission_failure_does_not_write_sheets(self):
        ns=namespace()
        with patch('group_credit_handler.GroupCredit') as cls:
            cls.return_value.confirm_cancel.side_effect=CreditError('管理員限定')
            self.assertTrue(handle_group_credit_postback(SimpleNamespace(postback=SimpleNamespace(data='gc_cancel_confirm:'+TOKEN)),ns))
        ns['mark_sales_rows_undone'].assert_not_called()
    def test_duplicate_does_not_mirror_or_refund(self):
        ns=namespace()
        with patch('group_credit_handler.GroupCredit') as cls:
            cls.return_value.confirm_cancel.return_value=dict(duplicate=True,order_no='TX_TEST')
            self.assertTrue(handle_group_credit_postback(SimpleNamespace(postback=SimpleNamespace(data='gc_cancel_confirm:'+TOKEN)),ns))
        ns['mark_sales_rows_undone'].assert_not_called()
        self.assertIn('不會再次',ns['reply'].call_args.args[1])
    def test_decline_does_not_write_sheets(self):
        ns=namespace()
        with patch('group_credit_handler.GroupCredit') as cls:
            cls.return_value.confirm_cancel.return_value=dict(declined=True,order_no='TX_TEST')
            handle_group_credit_postback(SimpleNamespace(postback=SimpleNamespace(data='gc_cancel_decline:'+TOKEN)),ns)
            cls.return_value.confirm_cancel.assert_called_once_with(GROUP,USER,TOKEN,True,decline=True)
        ns['mark_sales_rows_undone'].assert_not_called()
    def test_sheet_failure_reports_committed_cancellation(self):
        ns=namespace();ns['mark_sales_rows_undone'].side_effect=RuntimeError('fake failure')
        with patch('group_credit_handler.GroupCredit') as cls,patch('group_credit_sheet.clear_recovered_product_sales',return_value=False):
            cls.return_value.confirm_cancel.return_value=dict(duplicate=False,order_no='TX_TEST',quantity=1,amount=Decimal('905'),debt=Decimal('0'),items=[dict(order_no='TX_CHILD')])
            handle_group_credit_postback(SimpleNamespace(postback=SimpleNamespace(data='gc_cancel_confirm:'+TOKEN)),ns)
            cls.return_value.confirm_cancel.assert_called_once()
        self.assertIn('資料庫已撤回',ns['reply'].call_args.args[1])
    def test_signed_webhook_handler_routes_button(self):
        code=(Path(__file__).parent/'app.py').read_text(encoding='utf-8')
        tree=ast.parse(code)
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='handle_group_credit_button')
        self.assertIn('handler.add(PostbackEvent)',ast.unparse(fn))
        self.assertIn('handle_group_credit_postback(event, globals())',ast.unparse(fn))

class MirrorTests(unittest.TestCase):
    def setup_ns(self,rows):
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value;cur.fetchall.return_value=rows
        return dict(get_db=lambda:conn,RealDictCursor=object,PRODUCT_SHEET_WRITE_LOCK=threading.RLock(),
                    clear_product_sale=MagicMock(return_value=True)),conn,cur
    def test_resold_card_never_cleared(self):
        ns,conn,cur=self.setup_ns([dict(id=1,serial='SoldAgain',status='sold')])
        self.assertTrue(clear_recovered_product_sales([dict(serial_ids=[1])],ns))
        ns['clear_product_sale'].assert_not_called()
        conn.rollback.assert_called_once()
    def test_only_available_cards_clear_and_keep_case(self):
        ns,conn,cur=self.setup_ns([dict(id=1,serial='MiXeD',status='available'),dict(id=2,serial='SoldAgain',status='sold')])
        self.assertTrue(clear_recovered_product_sales([dict(serial_ids=[1,2])],ns))
        self.assertEqual(ns['clear_product_sale'].call_args.args[0][0]['serials'],['MiXeD'])
        self.assertIn('FOR UPDATE',cur.execute.call_args.args[0])
    def test_sheet_error_releases_locks_and_warns(self):
        ns,conn,cur=self.setup_ns([dict(id=1,serial='Fake',status='available')])
        ns['clear_product_sale'].side_effect=RuntimeError('fake')
        self.assertFalse(clear_recovered_product_sales([dict(serial_ids=[1])],ns))
        conn.rollback.assert_called_once();conn.close.assert_called_once()

if __name__=='__main__':unittest.main()
