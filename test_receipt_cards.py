"""Pure receipt presentation and delivery-state regressions. Fake data only."""
from decimal import Decimal as D
from types import SimpleNamespace as NS
import json
import unittest
from unittest.mock import MagicMock, patch
from receipt_cards import receipt_bubble, money_text
from group_credit_handler import deliver_sale, handle_group_credit


def result():
    return dict(customer='軒',order_no='TX_FAKE_RECEIPT',amount=D('9050'),debt=D('9061.85'),
                credit_limit=D('50000'),serial_chunks=['FAKE_MiXeD_0001 PIN_FAKE_0000'],
                items=[dict(product=dict(code='MY1萬'),quantity=1,unit_price=D('9050'),sale_amount=D('9050'))])


def ns():
    return dict(reply_messages=MagicMock(),append_sales_rows=MagicMock(return_value=True),
                mark_product_sale=MagicMock(return_value=True))


class ReceiptTests(unittest.TestCase):
    def test_test_receipt_mirrors_separate_sales_but_not_product_stock(self):
        r=result();r['customer']='測試'
        r['items'][0]['product']['code']='test100'
        data=json.dumps(receipt_bubble(r),ensure_ascii=False)
        self.assertIn('測試拿卡記帳完成',data)
        self.assertIn('測試銷售紀錄',data)
        namespace=ns();svc=MagicMock()
        with patch('receipt_cards.reply_receipt') as reply:
            deliver_sale(NS(),namespace,svc,'group:FAKE','FAKE',r)
            self.assertIn('【測試】',reply.call_args.args[2])
        namespace['append_sales_rows'].assert_called_once_with('測試',r['items'])
        namespace['mark_product_sale'].assert_not_called()
        svc.delivery.assert_called_once_with('group:FAKE','FAKE','delivered')
    def test_amounts_match_committed_sale_and_original_balance(self):
        data=json.dumps(receipt_bubble(result()),ensure_ascii=False)
        for value in ('NT$ 11.85','+ NT$ 9,050.00','NT$ 9,061.85','NT$ 40,938.15','9,050.00 × 1 = 9,050.00'):
            self.assertIn(value,data)
    def test_no_cards_ids_buttons_or_actions_in_receipt(self):
        data=json.dumps(receipt_bubble(result()),ensure_ascii=False)
        for value in ('FAKE_MiXeD','PIN_FAKE','serial','user_id','action','button','quickReply','quick_reply'):
            self.assertNotIn(value,data)
    def test_reaching_limit_is_clear(self):
        r=result();r['credit_limit']=r['debt']
        data=json.dumps(receipt_bubble(r),ensure_ascii=False)
        self.assertIn('已達額度上限',data);self.assertIn('NT$ 0.00',data)
    def test_sheet_failure_warns_already_recorded(self):
        for sheet_ok,stock_ok in ((False,True),(True,False),(False,False)):
            self.assertIn('勿重複下單或加帳',json.dumps(receipt_bubble(result(),sheet_ok,stock_ok),ensure_ascii=False))
    def test_large_multiline_order_has_bounded_receipt(self):
        r=result();r['items']*=1000
        data=json.dumps(receipt_bubble(r),ensure_ascii=False)
        self.assertLess(len(data.encode()),30000);self.assertIn('另 994 項',data)
    def test_decimal_rounding_and_invalid_amounts(self):
        self.assertEqual(money_text('0.005'),'0.01')
        for value in ('NaN','Infinity'):
            with self.assertRaises(ValueError):money_text(value)
        r=result();r['debt']=D('-95')
        data=json.dumps(receipt_bubble(r),ensure_ascii=False)
        self.assertIn('預付款餘額',data);self.assertIn('95.00',data)
        self.assertIn('原預付款',data);self.assertNotIn('-95.00',data)
        r['amount']=D('-1')
        with self.assertRaises(ValueError):receipt_bubble(r)
    def test_delivery_only_formats_and_mirrors_existing_order(self):
        data=result();namespace=ns();service=MagicMock()
        with patch('receipt_cards.reply_receipt') as reply:
            self.assertTrue(deliver_sale(NS(),namespace,service,'group:FAKE','EVENT_FAKE',data))
        reply.assert_called_once_with(unittest.mock.ANY,data,unittest.mock.ANY,namespace,True,True)
        service.delivery.assert_called_once_with('group:FAKE','EVENT_FAKE','delivered')
        service.buy.assert_not_called();service.manage.assert_not_called()
    def test_transport_timeout_never_retries_or_refunds(self):
        namespace=ns();service=MagicMock()
        with patch('receipt_cards.reply_receipt',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):deliver_sale(NS(),namespace,service,'group:FAKE','EVENT_FAKE',result())
        namespace['reply_messages'].assert_not_called()
        service.delivery.assert_called_once_with('group:FAKE','EVENT_FAKE','uncertain')
        service.manage.assert_not_called()
    def test_duplicate_event_does_not_send_receipt_again(self):
        namespace=ns()
        namespace.update(get_context_id=lambda e:'group:FAKE',get_operator=lambda e:'FAKE_USER',
                         is_admin=lambda u:False,get_db=MagicMock(),RealDictCursor=object,
                         find_product=MagicMock(),create_order_no=MagicMock(),split_text_chunks=MagicMock(),
                         parse_quick_sale_message=lambda text:dict(ok=True,items=[dict(product_text='MY1萬',quantity=1)]),
                         reply=MagicMock())
        event=NS(message=NS(text='/1萬*1',id='EVENT_FAKE'))
        with patch('group_credit_handler.GroupCredit') as cls,patch('receipt_cards.reply_receipt') as reply:
            cls.return_value.buy.return_value=dict(duplicate=True,order_no='TX_FAKE_RECEIPT')
            self.assertTrue(handle_group_credit(event,namespace))
            reply.assert_not_called()
        namespace['append_sales_rows'].assert_not_called()
        namespace['mark_product_sale'].assert_not_called()


if __name__=='__main__':unittest.main()
