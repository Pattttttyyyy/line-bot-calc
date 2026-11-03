import ast,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from bottom_quick import navigation_actions,merge_actions,decorate_reply
from bot_ui import handle_ui_postback,show_menu
from group_credit import CreditError

GROUP='group:C'+'a'*32
USER='U'+'b'*32

class ShortcutTests(unittest.TestCase):
    def test_management_menu_opens_stock_products_without_confirming(self):
        store=MagicMock();store.authorize.return_value=(True,None)
        session={'token':'a'*32,'state':'product','kind':'stock'}
        store.begin.return_value=session
        with patch('bot_ui.show_session') as show:
            show_menu(SimpleNamespace(webhook_event_id='menu-event'),{},store,GROUP,USER,True)
        store.begin.assert_called_once_with(GROUP,USER,'menu-event','stock',True)
        show.assert_called_once_with(unittest.mock.ANY,{},store,session)
        store.confirm.assert_not_called()
    def test_customer_menu_does_not_start_stock_session(self):
        store=MagicMock();store.authorize.return_value=(False,{})
        ns={'reply':MagicMock()}
        show_menu(object(),ns,store,GROUP,USER,False)
        store.begin.assert_not_called();ns['reply'].assert_called_once()
    def test_unauthorized_menu_does_not_start_session(self):
        store=MagicMock();store.authorize.side_effect=CreditError('denied')
        with self.assertRaises(CreditError):show_menu(object(),{},store,GROUP,USER,False)
        store.begin.assert_not_called()
    def ns(self):return dict(get_context_id=lambda e:GROUP,get_operator=lambda e:USER,is_admin=lambda u:False)
    def test_customer_four_products_and_navigation(self):
        with patch('bottom_quick.context_role',return_value='customer'):
            a=navigation_actions(object(),self.ns())
        self.assertEqual([label for label,_ in a[:4]],['MY1000','MY3000','MY5000','MY10000'])
        self.assertEqual(a[3][1],'bu:quick:MY1萬')
        self.assertEqual(len(a),7)
    def test_management_has_stock_and_inventory(self):
        with patch('bottom_quick.context_role',return_value='admin'):
            a=navigation_actions(object(),self.ns())
        self.assertEqual([label for label,_ in a],['入庫','查庫存','選單'])
    def test_no_private_chat_menu(self):
        ns=self.ns();ns['get_context_id']=lambda e:'user:'+USER
        with patch('bottom_quick.context_role') as lookup:self.assertIsNone(navigation_actions(object(),ns));lookup.assert_not_called()
    def test_menu_lookup_failure_does_not_prevent_card_reply(self):
        messages=[SimpleNamespace(text='FAKE_CARD'),SimpleNamespace(text='SUMMARY')]
        with patch('bottom_quick.navigation_actions',side_effect=RuntimeError('test')):
            self.assertIs(decorate_reply(object(),messages,{}),messages)
        self.assertFalse(hasattr(messages[-1],'quick_reply'))
    def test_admin_products_fit_13(self):
        actions=[(str(i),'bu:product:fake:'+str(i)) for i in range(10)]
        self.assertEqual(len(merge_actions(actions,[('入庫','bu:begin:stock'),('查庫存','bu:inventory'),('選單','bu:menu')])),13)
    def test_oversize_is_rejected(self):
        with self.assertRaises(ValueError):merge_actions([(str(i),str(i)) for i in range(14)],[])
    def test_duplicate_actions_removed(self):self.assertEqual(merge_actions([('A','x')],[('B','x')]),[('A','x')])
    def test_product_tap_starts_selection_without_selling(self):
        e=SimpleNamespace(postback=SimpleNamespace(data='bu:quick:MY1萬'),webhook_event_id='EID')
        ns=self.ns();ns['reply']=MagicMock()
        with patch('bot_ui.UIStore') as cls,patch('bot_ui.show_session'):
            store=cls.return_value;store.begin.return_value={'token':'a'*32}
            self.assertTrue(handle_ui_postback(e,ns))
            store.begin.assert_called_once_with(GROUP,USER,'EID','sale',False)
            store.change.assert_called_once_with(GROUP,USER,'a'*32,'product','MY1萬',False)
            store.confirm.assert_not_called()
    def test_unpriced_shortcut_rejected(self):
        ns=self.ns();ns['reply']=MagicMock()
        with patch('bot_ui.UIStore') as cls:
            self.assertTrue(handle_ui_postback(SimpleNamespace(postback=SimpleNamespace(data='bu:quick:貝1000')),ns))
            cls.return_value.begin.assert_not_called()
    def test_permission_denied_does_not_start_selection(self):
        ns=self.ns();ns['reply']=MagicMock()
        with patch('bot_ui.UIStore') as cls:
            cls.return_value.begin.side_effect=CreditError('沒有權限')
            handle_ui_postback(SimpleNamespace(postback=SimpleNamespace(data='bu:quick:MY1000'),webhook_event_id='EID'),ns)
            cls.return_value.change.assert_not_called()
    def test_app_decorates_only_existing_reply_payloads(self):
        tree=ast.parse((Path(__file__).parent/'app.py').read_text(encoding='utf-8'))
        for name in ('reply','reply_messages'):
            fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            self.assertEqual(sum(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='decorate_reply' for n in ast.walk(fn)),1)

if __name__=='__main__':unittest.main()
