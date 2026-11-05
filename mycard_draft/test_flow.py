"""Synthetic-only tests; no credential/API/production application access."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace as N
import unittest
from unittest.mock import MagicMock
from mycard_draft.flow import Intent, QRQuote, Confirmation, State, Refused, parse_points, check_intent, execute, finish_bookkeeping


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.intent = Intent('group:FAKE','admin','event','image','wallet-alias','軒',3290,D('.905'))
        self.qr = QRQuote(3290,2000,'irreversible-fake',object())
        self.policy = dict(admin=True,enabled=True,max_points=10000,image_group='group:FAKE',image_operator='admin',image_time=900,command_time=950,now=1000,group_enabled=True,available_credit=D('5000'))
        self.journal = MagicMock(spec=['claim','unknown','confirmed','assert_confirmed','booked'])
        self.journal.claim.return_value=N(new=True,reference='DRAFT_FAKE',state=State.SENDING)
        self.provider=MagicMock()
        self.provider.confirm.return_value=Confirmation(True,'PROVIDER_FAKE')
        self.ledger=MagicMock()

    def run_flow(self,**extra):
        return execute(self.intent,self.qr,self.journal,self.provider,self.ledger,**dict(self.policy,**extra))

    def test_command_no_quantity(self):
        self.assertEqual(parse_points('扣點3290'),3290)
        self.assertEqual(parse_points(' 扣點３２９０ '),3290)
        self.assertEqual(parse_points('/扣點 3290'),3290)

    def test_reject_partial_negative_quantity_or_expression(self):
        for text in ['扣點0','扣點-3290','扣點3290*1','扣點3290x1','扣點\n3290','扣點3290 1','扣點3290.0','扣點3290#備註','扣點3290\n扣點1000','扣點9999999999',None]:
            with self.subTest(text=text), self.assertRaises(Refused):parse_points(text)

    def test_denied_before_payment_or_journal(self):
        with self.assertRaises(Refused):self.run_flow(admin=False)
        self.provider.assert_not_called();self.journal.claim.assert_not_called()

    def test_default_disabled(self):
        policy=dict(self.policy);del policy['enabled']
        with self.assertRaises(Refused):execute(self.intent,self.qr,self.journal,self.provider,self.ledger,**policy)
        self.provider.confirm.assert_not_called()

    def test_explicit_limit_required(self):
        with self.assertRaises(Refused):self.run_flow(max_points=None)
        with self.assertRaises(Refused):self.run_flow(max_points=1000)
        self.journal.claim.assert_not_called()

    def test_group_and_uploader_bound(self):
        for extra in [dict(image_group='group:OTHER'),dict(image_operator='other-admin'),dict(group_enabled=False)]:
            with self.subTest(extra=extra),self.assertRaises(Refused):self.run_flow(**extra)
        self.provider.confirm.assert_not_called()

    def test_image_command_expiry_and_order(self):
        for extra in [dict(image_time=600),dict(command_time=850),dict(command_time=1001)]:
            with self.subTest(extra=extra),self.assertRaises(Refused):self.run_flow(**extra)
        self.provider.confirm.assert_not_called()

    def test_qr_expiry_and_point_match(self):
        for quote in [replace(self.qr,points=1000),replace(self.qr,expires_at=999),replace(self.qr,fingerprint=''),replace(self.qr,expires_at=float('nan'))]:
            with self.subTest(quote=quote),self.assertRaises(Refused):
                execute(self.intent,quote,self.journal,self.provider,self.ledger,**self.policy)
        self.provider.confirm.assert_not_called()

    def test_configured_price_and_credit_check(self):
        self.assertEqual(check_intent(self.intent,self.qr,**self.policy),D('2977.45'))
        with self.assertRaises(Refused):self.run_flow(available_credit=D('2977.44'))
        with self.assertRaises(Refused):check_intent(replace(self.intent,rate=D('NaN')),self.qr,**self.policy)

    def test_durable_claim_before_payment_then_bookkeeping(self):
        calls=[]
        self.journal.claim.side_effect=lambda *a:(calls.append('claim') or N(new=True,reference='FAKE',state=State.SENDING))
        self.provider.confirm.side_effect=lambda *a:(calls.append('confirm') or Confirmation(True,'PROVIDER_FAKE'))
        self.journal.confirmed.side_effect=lambda *a:calls.append('persist-success')
        self.ledger.book_once.side_effect=lambda *a:calls.append('book')
        self.assertEqual(self.run_flow(),State.BOOKED)
        self.assertEqual(calls,['claim','confirm','persist-success','book'])

    def test_duplicate_message_or_qr_never_pays_again(self):
        for state in State:
            self.journal.claim.return_value=N(new=False,state=state)
            self.assertEqual(self.run_flow(),state)
        self.provider.confirm.assert_not_called();self.ledger.book_once.assert_not_called()

    def test_timeout_is_unknown_not_retry_or_book(self):
        self.provider.confirm.side_effect=TimeoutError('synthetic')
        self.assertEqual(self.run_flow(),State.UNKNOWN)
        self.provider.confirm.assert_called_once();self.ledger.book_once.assert_not_called()
        self.journal.unknown.assert_called_once_with('DRAFT_FAKE')

    def test_unverified_response_is_unknown(self):
        for receipt in [Confirmation(False,'FAIL'),Confirmation(True,''),{},None]:
            self.provider.confirm.return_value=receipt
            self.assertEqual(self.run_flow(),State.UNKNOWN)
        self.ledger.book_once.assert_not_called()

    def test_claim_failure_never_pays(self):
        self.journal.claim.side_effect=RuntimeError('synthetic')
        with self.assertRaises(RuntimeError):self.run_flow()
        self.provider.confirm.assert_not_called()

    def test_success_save_failure_never_repeats_payment(self):
        self.journal.confirmed.side_effect=RuntimeError('synthetic')
        with self.assertRaises(RuntimeError):self.run_flow()
        self.provider.confirm.assert_called_once();self.ledger.book_once.assert_not_called()

    def test_bookkeeping_failure_can_retry_without_payment(self):
        self.ledger.book_once.side_effect=RuntimeError('synthetic')
        self.assertEqual(self.run_flow(),State.CONFIRMED)
        self.ledger.book_once.side_effect=None
        self.assertEqual(finish_bookkeeping('DRAFT_FAKE',self.intent,'PROVIDER_FAKE',self.journal,self.ledger),State.BOOKED)
        self.provider.confirm.assert_called_once()

    def test_cannot_invent_receipt_to_post_money(self):
        self.journal.assert_confirmed.side_effect=Refused('saved receipt mismatch')
        with self.assertRaises(Refused):finish_bookkeeping('FAKE',self.intent,'FORGED',self.journal,self.ledger)
        self.ledger.book_once.assert_not_called()


if __name__=='__main__':unittest.main()
