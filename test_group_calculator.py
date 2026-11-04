"""Calculator policy, Decimal parser and atomic-ledger tests. No live app import."""
import copy
import json
from decimal import Decimal as D
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch
from group_calculator import parse_calculation, looks_like_calculation, handle_calculator
from group_credit import CreditError
from receipt_cards import calculator_bubble
from test_group_credit import MemoryDB, service, GROUP, USER, OTHER


class CalculatorTests(unittest.TestCase):
    def test_screenshot_math_and_exact_decimals(self):
        self.assertEqual(parse_calculation('-10000*0.905')[1], D('-9050'))
        self.assertEqual(parse_calculation('+0.1+0.2')[1], D('.30'))
        self.assertEqual(parse_calculation('+(1000-200)/4')[1], D('200'))
    def test_fullwidth_and_note(self):
        self.assertEqual(parse_calculation('－１０００×０.９０５ #補帳'),('-1000*0.905',D('-905'),'補帳'))
    def test_final_rounding(self):
        self.assertEqual(parse_calculation('+1/3')[1],D('.33'))
        self.assertEqual(parse_calculation('-0.005')[1],D('-.01'))
    def test_code_and_unsupported_operations_are_rejected(self):
        for raw in ('+__import__("os")','+1**2','+10//3','+True','+1e3','+0x10','+1;2','+1,000','+1/0','+nan','+1%2','+1\n+2','+1_000'):
            with self.subTest(raw=raw), self.assertRaises(CreditError):parse_calculation(raw)
    def test_resource_bounds(self):
        for raw in ('+'+'9'*129,'+'+'-'*15+'1','+'+'1+'*40+'1','+1 #'+'備'*61,'+10000000000000000'):
            with self.subTest(raw=raw), self.assertRaises(CreditError):parse_calculation(raw)
    def test_normal_chat_not_calculator(self):
        for raw in ('hello','FAKE_CARD','123','/我的ID','/test100*1','+加油'):
            self.assertFalse(looks_like_calculation(raw))
        for raw in ('+0','-1000*0.905','＋０','/計算 -1000'):
            self.assertTrue(looks_like_calculation(raw))
    def test_zero_reads_without_audit_inventory_or_commit(self):
        db=MemoryDB(debt='905');before=copy.deepcopy(db.state)
        r=service(db).calculate(GROUP,USER,'query','+0')
        self.assertEqual(r['debt'],D('905'));self.assertEqual(db.state,before)
        self.assertEqual(db.inventory_reads,0);self.assertFalse(db.connections[-1].committed)
    def test_zero_requires_group_approval(self):
        db=MemoryDB()
        with self.assertRaises(CreditError):service(db).calculate(GROUP,OTHER,'query','+0')
        self.assertEqual(db.state['events'],{})
    def test_non_admin_cannot_adjust_even_approved_member(self):
        db=MemoryDB(debt='1000')
        for raw in ('+100','-100','/計算 -1','-0.005'):
            with self.assertRaises(CreditError):service(db).calculate(GROUP,USER,'calc',raw)
        self.assertEqual(db.connections,[])
    def test_admin_charge_payment_and_duplicate(self):
        db=MemoryDB();s=service(db)
        r=s.calculate(GROUP,USER,'charge','-1000*0.905 #補帳',True)
        self.assertEqual(r['previous_debt'],0);self.assertEqual(r['debt'],D('905'))
        self.assertTrue(s.calculate(GROUP,USER,'charge','-1000*0.905',True)['duplicate'])
        self.assertEqual(s.calculate(GROUP,USER,'payment','+500 #匯款',True)['debt'],D('405'))
        self.assertEqual(db.inventory_reads,0);self.assertEqual(db.state['transactions'],[])
        self.assertEqual(db.state['events'][(GROUP,'charge')]['amount'],D('905'))
        self.assertEqual(db.state['events'][(GROUP,'payment')]['amount'],D('-500'))
    def test_overpayment_rolls_back(self):
        db=MemoryDB(debt='905');before=copy.deepcopy(db.state)
        with self.assertRaises(CreditError):service(db).calculate(GROUP,USER,'over','+906',True)
        self.assertEqual(db.state,before)
    def test_audit_failure_rolls_back_debt(self):
        db=MemoryDB();db.fail_event=True;before=copy.deepcopy(db.state)
        with self.assertRaises(RuntimeError):service(db).calculate(GROUP,USER,'charge','-905',True)
        self.assertEqual(db.state,before)
    def test_test_receipt_values_and_no_actions(self):
        db=MemoryDB(debt='11.85');db.state['account']['customer']='測試'
        r=service(db).calculate(GROUP,USER,'charge','-10000*0.905 #補帳',True)
        data=json.dumps(calculator_bubble(r),ensure_ascii=False)
        for value in ('測試｜計算結果｜已記帳','11.85','-9,050.00','9,061.85','補帳'):
            self.assertIn(value,data)
        for key in ('"action"','"footer"','"quickReply"','"serial"'):self.assertNotIn(key,data)
    def test_zero_receipt_supports_unset_limit_and_disabled(self):
        r=service(MemoryDB(limit=None,enabled=False)).calculate(GROUP,USER,'query','+0')
        data=json.dumps(calculator_bubble(r),ensure_ascii=False)
        self.assertIn('帳況查詢',data);self.assertIn('尚未設定',data);self.assertIn('未變更',data)
    def test_production_adapter_routes_non_slash_and_explicit_command(self):
        from text_commands import handle_ui_text
        for raw in ('+0','-1000*0.905','/計算 -905'):
            with patch('text_commands.handle_calculator',return_value=True) as handle:
                event=NS(message=NS(text=raw))
                self.assertTrue(handle_ui_text(event,{}));handle.assert_called_once_with(event,{})
    def test_unbound_group_does_not_adjust_or_reply(self):
        ns=dict(get_context_id=lambda e:GROUP,get_db=MagicMock(),RealDictCursor=object,
                find_product=MagicMock(),create_order_no=MagicMock(),split_text_chunks=MagicMock(),reply=MagicMock())
        with patch('group_calculator.GroupCredit') as cls:
            cls.return_value.registered.return_value=False
            self.assertFalse(handle_calculator(NS(),ns))
            cls.return_value.calculate.assert_not_called();ns['reply'].assert_not_called()


if __name__=='__main__':unittest.main()
