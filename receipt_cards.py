"""Read-only presentation of a committed self-service sale; no accounting writes."""
from decimal import Decimal, ROUND_HALF_UP


def money_text(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError('Invalid receipt amount')
    return format(amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), ',.2f')


def receipt_bubble(result, sheet_ok=True, stock_ok=True):
    amount, debt, limit = (Decimal(str(result[key])) for key in ('amount', 'debt', 'credit_limit'))
    previous = debt - amount
    if not all(value.is_finite() and value >= 0 for value in (amount, debt, limit, previous)):
        raise ValueError('Invalid committed receipt')

    def text(value, **options):
        return dict(type='text', text=str(value), wrap=True, **options)

    def row(label, value, strong=False):
        return dict(type='box', layout='horizontal', spacing='sm', contents=[
            text(label, size='sm', color='#777777', flex=4),
            text(value, size='sm', color='#A5754A', align='end', flex=6,
                 weight='bold' if strong else 'regular')])

    items = result['items']
    product_lines = [f"{item['product']['code']} × {item['quantity']}張\n"
                     f"{money_text(item['unit_price'])} × {item['quantity']} = {money_text(item['sale_amount'])}"
                     for item in items[:6]]
    if len(items) > 6:
        product_lines.append(f'另 {len(items) - 6} 項，金額已包含於本次合計。')
    body = [text('拿卡記帳完成', size='lg', weight='bold', color='#23A455'),
            text('客戶：' + str(result['customer'])[:80], size='sm', color='#555555'),
            text('\n'.join(product_lines), size='xs', color='#777777'),
            dict(type='separator', margin='md', color='#EEEEEE'),
            row('原欠款', 'NT$ ' + money_text(previous)),
            row('本次拿卡', '+ NT$ ' + money_text(amount)),
            row('目前欠款', 'NT$ ' + money_text(debt), True),
            row('剩餘額度', 'NT$ ' + money_text(max(Decimal('0'), limit - debt))),
            dict(type='separator', margin='md', color='#EEEEEE'),
            text('訂單：' + str(result['order_no'])[:100], size='xxs', color='#999999'),
            text('已自動記帳，請勿另外重複加帳。\n欠款／額度為本筆完成時的群組共用帳況。',
                 size='xxs', color='#999999')]
    if debt >= limit:
        body.append(text('已達額度上限，請先聯繫管理員登記收款。', size='xs', color='#D95040'))
    if not sheet_ok or not stock_ok:
        body.append(text('試算表尚未同步完整；出庫與欠款已記錄，勿重複下單或加帳。',
                         size='xs', color='#D95040'))
    return dict(type='bubble', size='kilo', body=dict(type='box', layout='vertical',
                paddingAll='16px', spacing='sm', contents=body))


def reply_receipt(event, result, summary, ns, sheet_ok=True, stock_ok=True):
    # Build everything before sending. A local rendering error may safely fall
    # back to the original text; an API timeout may not (delivery is uncertain).
    try:
        from linebot.v3.messaging import (ApiClient, MessagingApi, ReplyMessageRequest,
                                         TextMessage, FlexMessage, FlexContainer)
        messages = [TextMessage(text=chunk) for chunk in result['serial_chunks']]
        if not 1 <= len(messages) <= 4:
            raise ValueError('Unexpected card message count')
        alt = (f"拿卡已記帳｜本次 NT$ {money_text(result['amount'])}｜"
               f"欠款 NT$ {money_text(result['debt'])}｜{result['order_no']}")[:400]
        messages.append(FlexMessage(alt_text=alt, contents=FlexContainer.from_dict(
            receipt_bubble(result, sheet_ok, stock_ok))))
        request = ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
    except Exception:
        # No card credentials or runtime settings in diagnostic output.
        return ns['reply_messages'](event, result['serial_chunks'] + [summary])
    with ApiClient(ns['configuration']) as client:
        MessagingApi(client).reply_message(request)
