"""Regression tests for the production text adapter, without importing app.py."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch
from text_commands import handle_ui_text, handle_ui_postback
from group_credit import CreditError

class TextCommandTests(unittest.TestCase):
    def ns(self, allowed=True, root=False):
        return dict(get_context_id=lambda e:'group:FAKE', get_operator=lambda e:'FAKE_USER',
                    is_admin=MagicMock(return_value=root), can_use_bot=MagicMock(return_value=allowed),
                    reply=MagicMock())
    def event(self, text):
        return NS(message=NS(text=text,id='FAKE'),source=NS(user_id='FAKE_USER',group_id='FAKE'),reply_token='FAKE')
    def test_existing_authorized_operator_reaches_legacy_commands(self):
        for command in ('/軒 10000 2','/出 MY1000*2','/入庫 MY1000\nFAKE_ONLY','/待補','/加權限 FAKE'):
            with self.subTest(command=command),patch('text_commands.UIStore') as cls:
                cls.return_value.admin_group.return_value=True
                ns=self.ns()
                self.assertFalse(handle_ui_text(self.event(command),ns))
                ns['can_use_bot'].assert_called_with('FAKE_USER')
                ns['reply'].assert_not_called()
    def test_removed_operator_is_denied_without_mutation(self):
        with patch('text_commands.UIStore') as cls:
            ns=self.ns(allowed=False)
            self.assertTrue(handle_ui_text(self.event('/軒 10000 2'),ns))
            cls.return_value.begin.assert_not_called()
            cls.return_value.bind_admin.assert_not_called()
            self.assertIn('沒有操作權限',ns['reply'].call_args.args[1])
    def test_root_only_group_binding_is_not_promoted(self):
        with patch('text_commands.UIStore') as cls:
            cls.return_value.bind_admin.side_effect=CreditError('管理員限定')
            ns=self.ns()
            handle_ui_text(self.event('/設定管理群組'),ns)
            cls.return_value.bind_admin.assert_called_once_with('group:FAKE','FAKE_USER',False)
    def test_customer_group_legacy_sales_still_go_through_credit_adapter(self):
        with patch('text_commands.UIStore') as cls:
            cls.return_value.admin_group.return_value=False
            ns=self.ns()
            self.assertFalse(handle_ui_text(self.event('/MY1000*2'),ns))
            ns['can_use_bot'].assert_not_called()
    def test_normal_chat_does_not_open_database_or_resume_old_intake(self):
        with patch('text_commands.UIStore') as cls:
            self.assertFalse(handle_ui_text(self.event('FAKE_CARD_DATA'),self.ns()))
            cls.assert_not_called()
    def test_menu_never_creates_or_confirms_session(self):
        with patch('text_commands.UIStore') as cls:
            ns=self.ns();handle_ui_text(self.event('/選單'),ns)
            cls.return_value.begin.assert_not_called()
            cls.return_value.confirm.assert_not_called()
            self.assertIn('文字指令',ns['reply'].call_args.args[1])
    def test_every_old_button_is_disabled_without_database(self):
        for data in ('bu:confirm:FAKE:1','bu:begin:stock','gc_cancel_confirm:FAKE','gc_cancel_decline:FAKE'):
            with patch('text_commands.UIStore') as cls:
                ns=self.ns()
                self.assertTrue(handle_ui_postback(NS(postback=NS(data=data)),ns))
                cls.assert_not_called()
                self.assertIn('未執行',ns['reply'].call_args.args[1])
    def test_text_confirmation_keeps_original_identity_and_root_permission(self):
        for command, data in (('/確認未使用並回收','gc_cancel_confirm:'),('/取消撤回','gc_cancel_decline:')):
            with patch('text_commands.UIStore') as cls,patch('group_credit_handler.handle_group_credit_postback',return_value=True) as confirm:
                cls.return_value.admin_group.return_value=False
                ns=self.ns();event=self.event(command+' '+'a'*32)
                self.assertTrue(handle_ui_text(event,ns))
                proxy,passed_ns=confirm.call_args.args
                self.assertIs(proxy.source,event.source)
                self.assertEqual(proxy.reply_token,event.reply_token)
                self.assertEqual(proxy.postback.data,data+'a'*32)
                self.assertIs(passed_ns,ns)
                self.assertFalse(passed_ns['is_admin']('FAKE_USER'))
    def test_invalid_confirmation_cannot_reach_cancellation(self):
        with patch('text_commands.UIStore'),patch('group_credit_handler.handle_group_credit_postback') as confirm:
            handle_ui_text(self.event('/確認未使用並回收 BAD'),self.ns())
            confirm.assert_not_called()
    def test_app_routes_to_text_only_module_and_keeps_root_gate(self):
        tree=ast.parse((Path(__file__).parent/'app.py').read_text())
        self.assertTrue(any(isinstance(n,ast.ImportFrom) and n.module=='text_commands' for n in tree.body))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='handle_message')
        # Execute the real handler with mocked adapters; no live app initialization.
        fn.decorator_list=[]
        import os
        reply=MagicMock();add=MagicMock()
        ns=dict(os=os,get_operator=lambda e:'FAKE_USER',get_context_id=lambda e:'group:FAKE',
                is_admin=lambda u:False,reply=reply,add_authorized_user=add,
                handle_ui_text=lambda e,n:False,handle_group_credit=lambda e,n:False,
                try_fast_group_sale=lambda e,n:False)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<handler>','exec'),ns)
        with patch.dict(os.environ,{'GROUP_SELF_SERVICE_ENABLED':'1'}):
            ns['handle_message'](self.event('/加權限 FAKE_USER2'))
        add.assert_not_called()
        self.assertIn('只有管理員',reply.call_args.args[1])

if __name__=='__main__':unittest.main()
