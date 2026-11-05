"""Fail-closed payment orchestration draft; no HTTP, credentials, app imports or DB DDL.

Production enablement requires a durable journal, a verified provider adapter and
atomic/idempotent group bookkeeping. None is wired into the live bot in this draft.
A LINE message saying 扣點3290 is an admin authorization for exactly 3290 points.
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import re
import math
import time
import unicodedata


class Refused(ValueError):
    pass


class State(str, Enum):
    CHECKING = 'checking'
    REJECTED = 'rejected_before_payment'
    SENDING = 'sending'
    UNKNOWN = 'unknown'
    CONFIRMED = 'confirmed_pending_ledger'
    BOOKED = 'booked'


def parse_points(text):
    """No quantity, formulas, trailing remarks or partial matching."""
    if not isinstance(text, str) or len(text) > 40:
        raise Refused('格式：扣點3290；一次只處理一筆，不需數量。')
    match = re.fullmatch(r'/?扣點[ \t]*([0-9]{1,9})', unicodedata.normalize('NFKC', text).strip())
    if not match or int(match[1]) <= 0:
        raise Refused('格式：扣點3290；點數須為正整數，不需數量。')
    return int(match[1])


@dataclass(frozen=True)
class PaymentDetails:
    # Immutable server-verified transaction snapshot, not raw QR parameters.
    order_ref: str
    payee_ref: str = field(repr=False)
    face_amount: Decimal
    currency: str


@dataclass(frozen=True)
class Intent:
    group: str
    operator: str
    message: str
    image_message: str
    wallet_alias: str
    customer: str
    points: int
    # Final rate is required from reviewed group configuration; no hidden default.
    rate: Decimal
    expected_payment: PaymentDetails | None = None

    @property
    def amount(self):
        return (Decimal(self.points) * self.rate).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class QRQuote:
    # Only a provider-validated quote, never arbitrary fields taken from a QR URL.
    points: int
    expires_at: float
    fingerprint: str  # Nonreversible QR identity; never the raw authCode / QR payload.
    operation: object = field(repr=False)  # Ephemeral; never serialize or log this.
    payment: PaymentDetails | None = None


@dataclass(frozen=True)
class Confirmation:
    verified_success: bool
    transaction_ref: str
    payment: PaymentDetails | None = None


def check_intent(intent, quote, *, admin, enabled=False, max_points=None,
                 image_group=None, image_operator=None, image_time=None, command_time=None,
                 now=None, group_enabled=False, available_credit=None):
    # Authorization is checked before access to image content or provider calls.
    if admin is not True:
        raise Refused('只有管理員可以扣點。')
    if enabled is not True:
        raise Refused('自動扣點尚未啟用。')
    if not intent.group.startswith('group:') or group_enabled is not True:
        raise Refused('只可在已開放且綁定的群組使用。')
    if not all((intent.operator, intent.message, intent.image_message, intent.wallet_alias, intent.customer)):
        raise Refused('群組或交易識別不完整。')
    if type(intent.points) is not int or intent.points < 1:
        raise Refused('點數不正確。')
    if type(max_points) is not int or max_points < 1 or intent.points > max_points:
        raise Refused('尚未設定單筆上限，或本筆超過上限。')
    if not isinstance(intent.rate, Decimal) or not intent.rate.is_finite() or not 0 < intent.rate <= 1:
        raise Refused('群組扣點售價尚未確認。')
    payment=quote.payment
    if not isinstance(payment,PaymentDetails) or not isinstance(intent.expected_payment,PaymentDetails):
        raise Refused('尚未取得扣點頁的完整交易資訊。')
    if not payment.order_ref or not payment.payee_ref or payment.currency!='TWD':
        raise Refused('扣點頁的訂單、收款對象或幣別未能確認。')
    if (not isinstance(payment.face_amount,Decimal) or not payment.face_amount.is_finite()
            or not Decimal('0') < payment.face_amount <= Decimal('9999999999999999.99')
            or payment.face_amount != payment.face_amount.quantize(Decimal('.01'))):
        raise Refused('扣點頁的交易金額不正確。')
    if payment!=intent.expected_payment:
        raise Refused('扣點頁金額或交易對象已變更，請重新核對。')
    if image_group != intent.group or image_operator != intent.operator:
        raise Refused('請由同一位管理員在本群貼QR圖後輸入扣點指令。')
    if None in (now, image_time, command_time) or not image_time <= command_time <= now:
        raise Refused('圖片或指令時間不正確。')
    if now - image_time > 300 or now - command_time > 300:
        raise Refused('圖片或指令已過期，請重新貼圖。')
    if type(quote.points) is not int or quote.points != intent.points:
        raise Refused('QR交易點數與指令不一致，本筆不扣點。')
    if not isinstance(quote.expires_at, (int, float)) or not math.isfinite(quote.expires_at):
        raise Refused('QR有效時間不正確。')
    if not quote.fingerprint or quote.operation is None or quote.expires_at <= now:
        raise Refused('QR交易無效或已過期。')
    if available_credit is None or not isinstance(available_credit, Decimal) or not available_credit.is_finite() or intent.amount > available_credit:
        raise Refused('群組可用額度不足或未確認。')
    return intent.amount


def execute(intent, quote, journal, provider, ledger, **policy):
    """Provider-neutral draft. All supplied dependencies must meet the contract.

    journal.claim MUST atomically deduplicate message AND QR across processes,
    reserve the wallet and group credit, and durably write CHECKING before returning
    new=True. Claims bind the full intent including the verified page snapshot.
    journal.sending MUST persist SENDING before confirm. No TTL releases a payment
    attempt; unknown results retain the wallet/credit reservation for reconciliation.
    Existing commands return their saved state and NEVER call confirm again.
    journal.confirmed MUST persist the provider reference before ledger posting.
    ledger.book_once MUST atomically write the group event and amount once, with
    the original rate/ref; completion and reservation release are idempotent.
    Those persistence guarantees are prerequisites, not implemented by this file.
    """
    check_intent(intent, quote, **policy)
    claim = journal.claim(intent, quote.fingerprint)
    if not claim.new:
        return claim.state
    try:
        # Re-read through the supported provider adapter, never follow arbitrary QR URLs.
        refreshed=provider.refresh(quote.operation)
        if not isinstance(refreshed,QRQuote) or refreshed.fingerprint!=quote.fingerprint:
            raise Refused('QR交易識別已變更。')
        check_intent(intent,refreshed,**dict(policy,now=time.time()))
    except Exception:
        # confirm has NOT been called; this specific reservation can be released.
        journal.rejected(claim.reference)
        return State.REJECTED
    journal.sending(claim.reference)
    try:
        receipt = provider.confirm(refreshed.operation)
        if not isinstance(receipt, Confirmation) or receipt.verified_success is not True or not receipt.transaction_ref or receipt.payment!=intent.expected_payment:
            # An error response alone is not proof the points were never taken.
            raise Refused('付款結果未能驗證。')
    except Exception:
        journal.unknown(claim.reference)
        return State.UNKNOWN
    # If persisting success fails, the prior SENDING claim remains blocked.
    # No catch/retry may repeat the external payment.
    journal.confirmed(claim.reference, receipt.transaction_ref)
    return finish_bookkeeping(claim.reference, intent, receipt.transaction_ref, journal, ledger)


def finish_bookkeeping(reference, intent, transaction_ref, journal, ledger):
    """Retry ONLY verified receipt bookkeeping; no provider/network call here.

    assert_confirmed must check that full intent and provider receipt are exactly
    those durably saved for this claim. A root command cannot invent success.
    """
    journal.assert_confirmed(reference, intent, transaction_ref)
    try:
        ledger.book_once(reference, intent, transaction_ref)
        journal.booked(reference)
    except Exception:
        return State.CONFIRMED
    return State.BOOKED
