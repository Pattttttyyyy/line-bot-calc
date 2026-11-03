import ast,re,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from bot_ui import parse_intake,handle_ui_text,handle_ui_postback
from group_credit import CreditError

def parser_namespace(path=None):
    path=path or Path(__file__).parent/'app.py'
    if not path.exists():path=Path(__file__).parent/'runtime/app.py'
    tree=ast.parse(path.read_text(encoding='utf-8'))
    ns={'re':re}
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='QUICK_PRODUCT_ALIASES' for t in node.targets):
            ns['QUICK_PRODUCT_ALIASES']=ast.literal_eval(node.value)
    for name in ('resolve_quick_product_alias','parse_supplier_stock_in','parse_stock_in_serial_values'):
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<parser>','exec'),ns)
    # Same note extraction as the existing command, no source-module startup.
    note_path=Path(__file__).parent/'stock_in_notes.py'
    exec(compile(note_path.read_text(encoding='utf-8'),'<notes>','exec'),ns)
    return ns

class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.ns=parser_namespace()
    def test_raw_my_case_and_whitespace(self):
        p=parse_intake('mc1234567890\n aBcDeF123456\n\nMC1234567891\nABCDEF123456\n','MY1000',self.ns)
        self.assertEqual(p['serials'],['mc1234567890 aBcDeF123456','MC1234567891 ABCDEF123456'])
    def test_vendor_my_segment(self):
        p=parse_intake('MyCard 1000｜360 張\n第 1／3 段\n\nMC1234567890\nABCDEF123456','MY1000',self.ns)
        self.assertEqual(p['segment'],(1,3));self.assertEqual(len(p['serials']),1)
    def test_raw_shell_leading_zero(self):
        self.assertEqual(parse_intake('\n0123456789012345\n\n1111111111111111\n','貝1000',self.ns)['serials'],['0123456789012345','1111111111111111'])
    def test_supplier_note(self):
        p=parse_intake('#飛飛\n貝殼幣1000｜1 張\n0123456789012345','貝1000',self.ns)
        self.assertEqual(p['note'],'飛飛')
    def test_existing_command_note(self):
        p=parse_intake('/入庫 貝1000 #EP\n0123456789012345','貝1000',self.ns)
        self.assertEqual(p['note'],'EP')
    def test_wrong_command_product(self):
        with self.assertRaises(CreditError):parse_intake('/入庫 貝500\n0123456789012345','貝1000',self.ns)
    def test_wrong_vendor_product(self):
        with self.assertRaises(CreditError):parse_intake('MyCard 3000｜1 張\nMC1234567890\nABCDEF123456','MY1000',self.ns)
    def test_count_mismatch(self):
        with self.assertRaises(CreditError):parse_intake('貝殼幣1000｜2 張\n0123456789012345','貝1000',self.ns)
    def test_duplicate_my_card_even_different_pin(self):
        with self.assertRaises(CreditError):parse_intake('MC1234567890 ABCDEF123456\nMC1234567890 abcdef123456','MY1000',self.ns)
    def test_incomplete_my_pair(self):
        with self.assertRaises(CreditError):parse_intake('MC1234567890','MY1000',self.ns)
    def test_raw_diamond(self):
        self.assertEqual(parse_intake('AbCdEf12345678901234\n','大卡',self.ns)['serials'],['AbCdEf12345678901234'])
    def test_no_input(self):
        with self.assertRaises(CreditError):parse_intake('\n','小卡',self.ns)
    def test_no_card_data_in_validation_error(self):
        with self.assertRaises(CreditError) as e:parse_intake('PRIVATE_INVALID_CARD','貝1000',self.ns)
        self.assertNotIn('PRIVATE_INVALID_CARD',str(e.exception))

class RoutingTests(unittest.TestCase):
    def ns(self):return dict(get_context_id=lambda e:'group:C'+'a'*32,get_operator=lambda e:'U'+'b'*32,is_admin=lambda u:False,reply=MagicMock())
    def test_ordinary_chat_ignored(self):
        with patch('bot_ui.UIStore') as cls:
            cls.return_value.admin_group.return_value=False
            self.assertFalse(handle_ui_text(SimpleNamespace(message=SimpleNamespace(text='一般聊天')),self.ns()))
            cls.return_value.pending.assert_not_called()
    def test_other_postback_ignored(self):
        self.assertFalse(handle_ui_postback(SimpleNamespace(postback=SimpleNamespace(data='gc_cancel_confirm:test')),{}))
    def test_non_admin_cannot_use_admin_group_commands(self):
        ns=self.ns()
        with patch('bot_ui.UIStore') as cls:
            cls.return_value.admin_group.return_value=True
            self.assertTrue(handle_ui_text(SimpleNamespace(message=SimpleNamespace(text='/入庫')),ns))
            cls.return_value.begin.assert_not_called()
        self.assertIn('僅供',ns['reply'].call_args.args[1])

if __name__=='__main__':unittest.main()
