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

class TestSalesMirrorTests(unittest.TestCase):
    def test_test_cancel_marks_only_dedicated_sales(self):
        from group_credit_handler import reply_cancel_result
        ns=namespace();ns['get_sales_sheet']=MagicMock(return_value='TEST_SHEET')
        ns['mark_sales_rows_undone_in_sheet']=MagicMock(return_value=True)
        result=dict(customer='測試',order_no='TX_TEST',quantity=1,amount=Decimal('905'),debt=Decimal('0'),items=[dict(order_no='CHILD')])
        reply_cancel_result(SimpleNamespace(),result,ns)
        ns['mark_sales_rows_undone_in_sheet'].assert_called_once_with(['CHILD'],'TEST_SHEET')
        ns['mark_sales_rows_undone'].assert_not_called()
    def test_sheet_created_once_and_preserves_headers(self):
        from group_credit_sheet import get_test_sales_sheet,TEST_SALES_HEADERS
        class Missing(Exception):pass
        book=MagicMock();sheet=MagicMock();sheet.row_values.return_value=[]
        book.worksheet.side_effect=Missing;book.add_worksheet.return_value=sheet
        self.assertIs(get_test_sales_sheet(book,Missing,threading.RLock()),sheet)
        sheet.append_row.assert_called_once_with(TEST_SALES_HEADERS,value_input_option='RAW')
        book.worksheet.side_effect=None;book.worksheet.return_value=sheet;sheet.row_values.return_value=TEST_SALES_HEADERS
        get_test_sales_sheet(book,Missing,threading.RLock())
        self.assertEqual(sheet.append_row.call_count,1)
    def test_incompatible_headers_are_not_overwritten(self):
        from group_credit_sheet import get_test_sales_sheet
        book=MagicMock();sheet=book.worksheet.return_value;sheet.row_values.return_value=['existing data']
        with self.assertRaises(ValueError):get_test_sales_sheet(book,KeyError,threading.RLock())
        sheet.append_row.assert_not_called();book.add_worksheet.assert_not_called()

    def test_app_routes_test_sales_separately_and_keeps_formal_set(self):
        from types import SimpleNamespace
        tree=ast.parse((Path(__file__).parent/'app.py').read_text())
        ns=dict(spreadsheet=MagicMock(),gspread=SimpleNamespace(WorksheetNotFound=KeyError),
                PRODUCT_SHEET_WRITE_LOCK=threading.RLock(),SALES_SHEET_NAME='銷售紀錄',XUAN_SALES_SHEET_NAME='軒銷售紀錄')
        for name in ('get_sales_sheet','get_sales_sheets'):
            fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            exec(compile(ast.Module(body=[fn],type_ignores=[]),'<sales routing>','exec'),ns)
        with patch('group_credit_sheet.get_test_sales_sheet',return_value='TEST_ONLY') as route:
            self.assertEqual(ns['get_sales_sheet']('測試'),'TEST_ONLY')
            route.assert_called_once()
            route.reset_mock();ns['get_sales_sheets']();route.assert_not_called()
        self.assertEqual([c.args[0] for c in ns['spreadsheet'].worksheet.call_args_list],['銷售紀錄','軒銷售紀錄'])

class MentionBuyerTests(unittest.TestCase):
    def message(self,command='/自助核准',label='@阿 豪😀',**changes):
        text='  '+command+' '+label+'  '
        target=dict(type='user',userId='U'+'e'*32,index=len(('  '+command+' ').encode('utf-16-le'))//2,length=len(label.encode('utf-16-le'))//2)
        target.update(changes)
        return SimpleNamespace(text=text,id='mention-event',mention=dict(mentionees=[target]))
    def test_structured_mention_name_with_spaces_and_emoji(self):
        from group_credit_handler import mentioned_buyer
        self.assertEqual(mentioned_buyer(self.message(),'/自助核准'),'U'+'e'*32)
    def test_root_approve_and_revoke_use_existing_group_management(self):
        for command,action in (('/自助核准','approve'),('/自助停權','revoke')):
            ns=namespace();event=SimpleNamespace(message=self.message(command))
            with patch('group_credit_handler.GroupCredit') as cls:
                cls.return_value.manage.return_value=dict(debt=Decimal('0'))
                self.assertTrue(handle_group_credit(event,ns))
                cls.return_value.manage.assert_called_once_with(GROUP,USER,'mention-event',action,'U'+'e'*32,'',True)
                cls.return_value.buy.assert_not_called()
    def test_nonroot_cannot_approve_even_with_mention(self):
        ns=namespace();ns['is_admin']=lambda u:False
        with patch('group_credit_handler.GroupCredit') as cls:
            handle_group_credit(SimpleNamespace(message=self.message()),ns)
            cls.return_value.manage.assert_not_called()
    def test_invalid_missing_id_bot_all_multiple_and_extra_text_fail(self):
        from group_credit_handler import mentioned_buyer
        cases=[self.message(type='all'),self.message(userId=None),self.message(isSelf=True),self.message(index=-1),self.message(length=9999)]
        multi=self.message();multi.mention['mentionees']*=2;cases.append(multi)
        extra=self.message();extra.text+=' extra';cases.append(extra)
        for msg in cases:
            with self.subTest(msg=msg),self.assertRaises(CreditError):mentioned_buyer(msg,'/自助核准')
    def test_manual_id_still_supported(self):
        ns=namespace();target='U'+'e'*32
        with patch('group_credit_handler.GroupCredit') as cls:
            cls.return_value.manage.return_value=dict(debt=Decimal('0'))
            handle_group_credit(SimpleNamespace(message=SimpleNamespace(text='/自助核准 '+target,id='manual')),ns)
            cls.return_value.manage.assert_called_once_with(GROUP,USER,'manual','approve',target,'',True)

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
