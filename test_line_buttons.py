"""Serialization test using installed official LINE SDK. No network calls."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
import unittest
from linebot.v3.messaging import Configuration
from linebot.v3.webhooks import PostbackEvent
from group_credit_handler import reply_cancel_prompt

class LineSDKTests(unittest.TestCase):
    def check_app_reply(self,name,texts):
        import ast
        from pathlib import Path
        from bottom_quick import decorate_reply
        from linebot.v3.messaging import ReplyMessageRequest,TextMessage
        root=Path(__file__).parent;path=root/'app.py'
        if not path.exists():path=root/'runtime/app.py'
        fn=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name==name)
        api=MagicMock()
        ns=dict(ApiClient=MagicMock(),MessagingApi=api,configuration=Configuration(access_token='FAKE'),
                ReplyMessageRequest=ReplyMessageRequest,TextMessage=TextMessage,decorate_reply=decorate_reply)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<reply>','exec'),ns)
        with patch('bottom_quick.navigation_actions',return_value=[('MY1000','bu:quick:MY1000')]):
            ns[name](SimpleNamespace(reply_token='FAKE'),texts)
        messages=api.return_value.reply_message.call_args.args[0].messages
        self.assertEqual(len(messages),1 if name=='reply' else len(texts))
        self.assertIsNone(messages[-1].quick_reply)
        if len(messages)>1:self.assertIsNone(messages[0].quick_reply)
    def test_single_reply_has_no_bottom_shortcut(self):self.check_app_reply('reply','test')
    def test_card_batch_has_no_bottom_row(self):self.check_app_reply('reply_messages',['FAKE_CARD','summary'])
    def test_no_quick_replies_on_any_card_reply(self):
        from bottom_quick import decorate_reply
        from linebot.v3.messaging import TextMessage
        messages=[TextMessage(text='FAKE_MiXeD_CARD'),TextMessage(text='summary')]
        with patch('bottom_quick.navigation_actions',return_value=[('MY1000','bu:quick:MY1000'),('查庫存','bu:inventory')]):
            decorate_reply(object(),messages,{})
        self.assertIsNone(messages[0].quick_reply)
        self.assertEqual(messages[0].text,'FAKE_MiXeD_CARD')
        self.assertIsNone(messages[1].quick_reply)
    def test_postback_event_identity_fields(self):
        event=PostbackEvent.from_dict(dict(type='postback',timestamp=0,mode='active',replyToken='FAKE',
            webhookEventId='FAKE_EVENT_ID',deliveryContext=dict(isRedelivery=False),
            source=dict(type='group',groupId='C'+'a'*32,userId='U'+'b'*32),postback=dict(data='bu:begin:sale')))
        self.assertEqual(event.webhook_event_id,'FAKE_EVENT_ID')
        self.assertEqual(event.source.user_id,'U'+'b'*32)
    def test_legacy_send_buttons_serializes_as_text_without_quick_reply(self):
        from bot_ui import send_buttons,PRODUCTS
        with patch('linebot.v3.messaging.ApiClient'),patch('linebot.v3.messaging.MessagingApi') as api,patch('bottom_quick.navigation_actions',return_value=[('入庫','bu:begin:stock'),('查庫存','bu:inventory'),('選單','bu:menu')]):
            send_buttons(SimpleNamespace(reply_token='FAKE'),dict(configuration=Configuration(access_token='FAKE')),
                '選擇商品',[(label,'bu:product:'+'a'*32+':'+code) for code,label in PRODUCTS],True)
            m=api.return_value.reply_message.call_args.args[0].to_dict()['messages'][0]
        self.assertEqual(m['type'],'text')
        quick=m.get('quickReply',m.get('quick_reply'))
        self.assertIsNone(quick)
    def test_cancel_prompt_is_text_with_explicit_confirmation(self):
        reply=MagicMock();token='a'*32
        reply_cancel_prompt(SimpleNamespace(reply_token='FAKE'),
            dict(token=token,order_no='TX_FAKE',quantity=2,amount=Decimal('1810')),
            dict(reply=reply))
        text=reply.call_args.args[1]
        self.assertIn('/確認未使用並回收 '+token,text)
        self.assertIn('/取消撤回 '+token,text)
        self.assertIn('5分鐘',text)
    def test_receipt_serializes_as_four_card_texts_and_one_buttonless_flex(self):
        from receipt_cards import reply_receipt
        from test_receipt_cards import result
        r=result();r['serial_chunks']=['FAKE_0000_MiXeD']*4
        ns=dict(configuration=Configuration(access_token='FAKE'),reply_messages=MagicMock())
        with patch('linebot.v3.messaging.ApiClient'),patch('linebot.v3.messaging.MessagingApi') as api:
            reply_receipt(SimpleNamespace(reply_token='FAKE'),r,'fallback',ns)
        request=api.return_value.reply_message.call_args.args[0].to_dict()
        messages=request['messages']
        self.assertEqual(len(messages),5)
        self.assertTrue(all(m['type']=='text' and m['text']=='FAKE_0000_MiXeD' for m in messages[:4]))
        self.assertEqual(messages[-1]['type'],'flex')
        self.assertEqual(messages[-1]['contents']['size'],'kilo')
        self.assertNotIn('FAKE_0000_MiXeD',str(messages[-1]))
        self.assertNotIn('action',str(messages[-1]))
        ns['reply_messages'].assert_not_called()
    def test_local_receipt_render_failure_falls_back_before_sending(self):
        from receipt_cards import reply_receipt
        from test_receipt_cards import result
        r=result();fallback=MagicMock();ns=dict(configuration=Configuration(access_token='FAKE'),reply_messages=fallback)
        with patch('receipt_cards.receipt_bubble',side_effect=ValueError),patch('linebot.v3.messaging.MessagingApi') as api:
            reply_receipt(SimpleNamespace(reply_token='FAKE'),r,'fallback',ns)
        api.assert_not_called()
        fallback.assert_called_once_with(unittest.mock.ANY,r['serial_chunks']+['fallback'])
    def test_api_timeout_has_no_fallback_send(self):
        from receipt_cards import reply_receipt
        from test_receipt_cards import result
        ns=dict(configuration=Configuration(access_token='FAKE'),reply_messages=MagicMock())
        with patch('linebot.v3.messaging.ApiClient'),patch('linebot.v3.messaging.MessagingApi') as api:
            api.return_value.reply_message.side_effect=TimeoutError
            with self.assertRaises(TimeoutError):reply_receipt(SimpleNamespace(reply_token='FAKE'),result(),'fallback',ns)
        ns['reply_messages'].assert_not_called()

    def calculator_result(self):
        return dict(customer='測試',expression='-1000*0.905',amount=Decimal('-905'),previous_debt=Decimal('0'),
                    debt=Decimal('905'),credit_limit=Decimal('10000'),enabled=False,note='測試補帳')
    def test_calculator_serializes_buttonless_flex(self):
        from receipt_cards import reply_calculator
        ns=dict(configuration=Configuration(access_token='FAKE'),reply=MagicMock())
        with patch('linebot.v3.messaging.ApiClient'),patch('linebot.v3.messaging.MessagingApi') as api:
            reply_calculator(SimpleNamespace(reply_token='FAKE'),self.calculator_result(),ns)
        request=api.return_value.reply_message.call_args.args[0].to_dict()
        self.assertEqual(len(request['messages']),1)
        self.assertEqual(request['messages'][0]['type'],'flex')
        self.assertIn('905.00',str(request));self.assertNotIn('action',str(request))
        ns['reply'].assert_not_called()
    def test_calculator_local_render_error_falls_back_before_send(self):
        from receipt_cards import reply_calculator
        ns=dict(configuration=Configuration(access_token='FAKE'),reply=MagicMock())
        with patch('receipt_cards.calculator_bubble',side_effect=ValueError),patch('linebot.v3.messaging.MessagingApi') as api:
            reply_calculator(SimpleNamespace(reply_token='FAKE'),self.calculator_result(),ns)
        api.assert_not_called();ns['reply'].assert_called_once()
    def test_calculator_transport_error_never_sends_fallback(self):
        from receipt_cards import reply_calculator
        ns=dict(configuration=Configuration(access_token='FAKE'),reply=MagicMock())
        with patch('linebot.v3.messaging.ApiClient'),patch('linebot.v3.messaging.MessagingApi') as api:
            api.return_value.reply_message.side_effect=TimeoutError
            with self.assertRaises(TimeoutError):reply_calculator(SimpleNamespace(reply_token='FAKE'),self.calculator_result(),ns)
        api.return_value.reply_message.assert_called_once();ns['reply'].assert_not_called()

if __name__=='__main__':unittest.main()


class MentionSDKTests(unittest.TestCase):
    def test_real_sdk_mention_parsing(self):
        from linebot.v3.webhooks import TextMessageContent
        from group_credit_handler import mentioned_buyer
        msg=TextMessageContent.from_dict(dict(type='text',id='FAKE',text='/自助核准 @測試客人',mention=dict(mentionees=[dict(type='user',index=6,length=5,userId='U'+'e'*32,isSelf=False)])))
        self.assertEqual(mentioned_buyer(msg,'/自助核准'),'U'+'e'*32)
