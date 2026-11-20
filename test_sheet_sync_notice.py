"""No-network tests for private administrator synchronization alerts."""
from types import SimpleNamespace as N
import unittest
from unittest.mock import MagicMock, patch
import sheet_sync_notice as notice
from legacy_sale_delivery import deliver_committed_sale
from group_sheet_queue import sync_pending_sales

ORDER='TX2026112023533117124B'
ADMIN='U'+'a'*32
OTHER='U'+'b'*32


class NoticeTests(unittest.TestCase):
    def setUp(self):
        notice._sent.clear()
        self.api=MagicMock()
        self.ns=dict(ADMIN_LINE_USER_IDS={ADMIN},ApiClient=MagicMock(),configuration=object(),
            MessagingApi=MagicMock(return_value=self.api),PushMessageRequest=N,TextMessage=N)

    def test_private_admin_only_no_card_data(self):
        self.ns['ADMIN_LINE_USER_IDS']={ADMIN,'not-an-admin-id'}
        notice.notify_sheet_failure(self.ns,ORDER)
        self.api.push_message.assert_called_once()
        call=self.api.push_message.call_args
        self.assertEqual(call.args[0].to,ADMIN)
        self.assertIn('/同步出庫 '+ORDER,call.args[0].messages[0].text)
        self.assertEqual(call.kwargs,dict(_request_timeout=8))
        self.assertNotIn('serial',str(call))

    def test_second_admin_still_notified_if_first_push_fails(self):
        self.ns['ADMIN_LINE_USER_IDS']={OTHER,ADMIN}
        self.api.push_message.side_effect=[TimeoutError('SECRET_CARD'),None]
        with patch('builtins.print') as log:
            notice.notify_sheet_failure(self.ns,ORDER)
        self.assertEqual(self.api.push_message.call_count,2)
        self.assertNotIn('SECRET_CARD',str(log.call_args_list))

    def test_repeat_backlog_notice_is_limited_for_five_minutes(self):
        with patch('sheet_sync_notice.monotonic',side_effect=[100,101,401]):
            notice.notify_sheet_failure(self.ns,ORDER)
            notice.notify_sheet_failure(self.ns,ORDER)
            notice.notify_sheet_failure(self.ns,ORDER)
        self.assertEqual(self.api.push_message.call_count,2)

    def test_failed_push_is_not_automatically_retried(self):
        self.api.push_message.side_effect=TimeoutError('SECRET')
        notice.notify_sheet_failure(self.ns,ORDER)
        self.api.push_message.assert_called_once()

    def test_credit_group_uses_its_existing_sync_command(self):
        notice.notify_sheet_failure(self.ns,ORDER,credit=True)
        text=self.api.push_message.call_args.args[0].messages[0].text
        self.assertIn('/同步發卡紀錄',text)
        self.assertNotIn('/同步出庫',text)

    def test_invalid_order_never_enters_notification(self):
        notice.notify_sheet_failure(self.ns,'FAKE_SECRET_CARD')
        self.api.push_message.assert_not_called()

    def test_no_configured_administrator_does_not_send_to_other_users(self):
        self.ns['ADMIN_LINE_USER_IDS']=set()
        notice.notify_sheet_failure(self.ns,ORDER)
        self.api.push_message.assert_not_called()


class DeliveryNoticeTests(unittest.TestCase):
    def test_alert_follows_reply_and_both_mirror_attempts(self):
        sequence=[]
        ns=dict(reply_messages=lambda *a:sequence.append('cards'),
            append_sales_rows=lambda *a:sequence.append('sales') or False,
            mark_product_sale=lambda *a:sequence.append('product') or True)
        items=[dict(product=dict(code='大卡'),quantity=3,order_no=ORDER)]
        with patch('legacy_sale_delivery.notify_sheet_failure',side_effect=lambda *a:sequence.append('notice')):
            self.assertTrue(deliver_committed_sale(N(),ns,'J',items,['FAKE_CARDS'],ORDER))
        self.assertEqual(sequence,['cards','sales','product','notice'])

    def test_successful_sync_never_alerts(self):
        ns=dict(reply_messages=MagicMock(),append_sales_rows=lambda *a:True,mark_product_sale=lambda *a:True)
        items=[dict(product=dict(code='大卡'),quantity=3,order_no=ORDER)]
        with patch('legacy_sale_delivery.notify_sheet_failure') as alert:
            self.assertTrue(deliver_committed_sale(N(),ns,'J',items,['FAKE_CARDS'],ORDER))
        alert.assert_not_called()
        self.assertNotIn('接續同步',ns['reply_messages'].call_args.args[1][-1])

    def test_sheet_exception_alerts_without_repeating_cards(self):
        ns=dict(reply_messages=MagicMock(),append_sales_rows=MagicMock(side_effect=TimeoutError),
                mark_product_sale=lambda *a:True)
        items=[dict(product=dict(code='大卡'),quantity=3,order_no=ORDER)]
        with patch('legacy_sale_delivery.notify_sheet_failure') as alert:
            self.assertTrue(deliver_committed_sale(N(),ns,'J',items,['FAKE_CARDS'],ORDER))
        alert.assert_called_once_with(ns,ORDER)
        ns['reply_messages'].assert_called_once()


class QueueNoticeTests(unittest.TestCase):
    def setUp(self):
        self.conn=MagicMock()
        self.cur=self.conn.cursor.return_value.__enter__.return_value
        self.service=N(get_db=lambda:self.conn,dict_cursor=object)
        self.ns=dict(append_sales_rows=MagicMock(return_value=False),mark_product_sale=MagicMock(return_value=True))

    def test_failed_queue_notifies_after_db_locks_are_released(self):
        sequence=[]
        self.conn.close.side_effect=lambda:sequence.append('closed')
        self.cur.fetchone.side_effect=[dict(acquired=True),dict(event_id='FAKE',order_no=ORDER,child_orders=[ORDER])]
        self.cur.fetchall.side_effect=[[dict(id=1)],[dict(order_no=ORDER,quantity=1,unit_price=1,
            sale_amount=1,sold_to='J',code='大卡',product_id=1,sale_date='2026/11/20',cancelled=False)],
            [dict(serial='FAKE_CARD')]]
        with patch('group_sheet_queue.notify_sheet_failure',side_effect=lambda *a,**kw:sequence.append('notice')):
            result=sync_pending_sales(self.service,self.ns,'FAKE_GROUP')
        self.assertTrue(result['failed']);self.assertEqual(sequence,['closed','notice'])
        self.conn.commit.assert_not_called()

    def test_busy_queue_is_not_a_sync_failure(self):
        self.cur.fetchone.return_value=dict(acquired=False)
        with patch('group_sheet_queue.notify_sheet_failure') as alert:
            self.assertTrue(sync_pending_sales(self.service,self.ns,'FAKE_GROUP')['busy'])
        alert.assert_not_called()

    def test_empty_queue_never_alerts(self):
        self.cur.fetchone.side_effect=[dict(acquired=True),None]
        with patch('group_sheet_queue.notify_sheet_failure') as alert:
            self.assertFalse(sync_pending_sales(self.service,self.ns,'FAKE_GROUP')['failed'])
        alert.assert_not_called()
