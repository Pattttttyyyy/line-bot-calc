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

if __name__=='__main__':unittest.main()
