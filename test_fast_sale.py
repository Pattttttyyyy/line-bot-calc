"""Safe fake-data tests of the single-connection sale route."""
import ast
from pathlib import Path
from types import SimpleNamespace as N
import unittest
from unittest.mock import MagicMock,patch
from group_credit import CreditError
from group_credit_handler import try_fast_group_sale

GROUP='group:C'+'a'*32
USER='U'+'b'*32

class FastSaleTests(unittest.TestCase):
    def setUp(self):
        self.conn=MagicMock()
        self.conn.cursor.return_value.__enter__.return_value.fetchone.return_value=(1,)
        self.ns=dict(get_context_id=lambda e:GROUP,get_operator=lambda e:USER,
            is_admin=MagicMock(return_value=False),get_db=MagicMock(return_value=self.conn),
            RealDictCursor=object,find_product=MagicMock(),create_order_no=MagicMock(),
            split_text_chunks=MagicMock(),reply=MagicMock(),
            parse_quick_sale_message=MagicMock(return_value=dict(ok=True,items=[dict(product_text='MY1000',quantity=1)])))
        self.event=N(message=N(text='/my1000 x 1',id='FAKE_EVENT'))
        self.service=patch('group_credit_handler.GroupCredit').start().return_value
        self.addCleanup(patch.stopall)
        self.service.buy.return_value=dict(duplicate=False,order_no='TX_FAKE')

    def test_one_connection_commits_and_closes_before_delivery(self):
        sequence=[]
        self.conn.commit.side_effect=lambda:sequence.append('commit')
        self.conn.close.side_effect=lambda:sequence.append('close')
        with patch('group_credit_handler.deliver_sale',side_effect=lambda *a:sequence.append('reply') or True):
            self.assertTrue(try_fast_group_sale(self.event,self.ns))
        self.assertEqual(sequence,['commit','close','reply'])
        self.ns['get_db'].assert_called_once()
        self.service.registered.assert_not_called()
        self.assertIs(self.service.buy.call_args.kwargs['transaction'],self.conn)
        self.assertFalse(self.service.buy.call_args.args[4])
        self.ns['parse_quick_sale_message'].assert_called_once_with('/MY1000*1')

    def test_unregistered_group_retains_legacy_route_without_sale(self):
        self.conn.cursor.return_value.__enter__.return_value.fetchone.return_value=None
        self.assertFalse(try_fast_group_sale(self.event,self.ns))
        self.service.buy.assert_not_called();self.conn.rollback.assert_called_once()
        self.conn.close.assert_called_once()

    def test_private_chat_never_opens_database(self):
        self.ns['get_context_id']=lambda e:'user:FAKE'
        self.assertFalse(try_fast_group_sale(self.event,self.ns))
        self.ns['get_db'].assert_not_called()

    def test_non_sale_and_unsupported_products_keep_existing_router(self):
        for parsed in (None,dict(ok=False),dict(ok=True,items=[dict(product_text='貝1000',quantity=1)])):
            self.ns['parse_quick_sale_message'].return_value=parsed
            self.assertFalse(try_fast_group_sale(self.event,self.ns))
        self.ns['get_db'].assert_not_called()

    def test_permission_or_limit_failure_rolls_back_before_reply(self):
        self.service.buy.side_effect=CreditError('FAKE_DENIED')
        with patch('group_credit_handler.deliver_sale') as send:
            self.assertTrue(try_fast_group_sale(self.event,self.ns))
        send.assert_not_called();self.conn.commit.assert_not_called()
        self.conn.rollback.assert_called_once();self.conn.close.assert_called_once()
        self.ns['reply'].assert_called_once_with(self.event,'FAKE_DENIED')

    def test_duplicate_never_delivers_cards_again(self):
        self.service.buy.return_value=dict(duplicate=True,order_no='TX_FAKE')
        with patch('group_credit_handler.deliver_sale') as send:
            self.assertTrue(try_fast_group_sale(self.event,self.ns))
        send.assert_not_called();self.assertIn('不會重複',self.ns['reply'].call_args.args[1])

    def test_commit_failure_never_sends_cards_or_falls_through(self):
        self.conn.commit.side_effect=RuntimeError('FAKE_DB')
        with patch('group_credit_handler.deliver_sale') as send,self.assertRaises(RuntimeError):
            try_fast_group_sale(self.event,self.ns)
        send.assert_not_called();self.conn.rollback.assert_called_once();self.conn.close.assert_called_once()

    def test_transport_timeout_after_commit_never_retries_buy(self):
        with patch('group_credit_handler.deliver_sale',side_effect=TimeoutError),self.assertRaises(TimeoutError):
            try_fast_group_sale(self.event,self.ns)
        self.service.buy.assert_called_once();self.conn.commit.assert_called_once()
        self.conn.rollback.assert_not_called()

