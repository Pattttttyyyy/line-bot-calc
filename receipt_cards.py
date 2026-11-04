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
    testing = result['customer'] == '測試'
    body = [text('測試拿卡記帳完成' if testing else '拿卡記帳完成', size='lg', weight='bold', color='#23A455'),
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
    if testing:
        body.append(text('測試卡原價 NT$ 1,000，售價 NT$ 905。\n僅記錄此群測試帳，不寫入正式銷售試算表。', size='xxs', color='#999999'))
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
        prefix = '【測試】' if result['customer']=='測試' else ''
        alt = (f"{prefix}拿卡已記帳｜本次 NT$ {money_text(result['amount'])}｜"
               f"欠款 NT$ {money_text(result['debt'])}｜{result['order_no']}")[:400]
        messages.append(FlexMessage(alt_text=alt, contents=FlexContainer.from_dict(
            receipt_bubble(result, sheet_ok, stock_ok))))
        request = ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
    except Exception:
        # No card credentials or runtime settings in diagnostic output.
        return ns['reply_messages'](event, result['serial_chunks'] + [summary])
    with ApiClient(ns['configuration']) as client:
        MessagingApi(client).reply_message(request)


def calculator_bubble(result):
    amount = Decimal(str(result['amount']))
    prefix = '測試｜' if result['customer'] == '測試' else ''
    title = prefix + ('帳況查詢' if amount == 0 else '計算結果｜已記帳')
    signed = ('+' if amount >= 0 else '-') + money_text(abs(amount))

    def text(value, **options):
        return dict(type='text', text=str(value), wrap=True, **options)

    def row(label, value, strong=False):
        return dict(type='box', layout='horizontal', spacing='sm', contents=[
            text(label, size='sm', color='#777777', flex=4),
            text(value, size='sm', color='#A5754A', align='end', flex=6,
                 weight='bold' if strong else 'regular')])

    limit = result['credit_limit']
    remaining = ('尚未設定' if limit is None else 'NT$ ' + money_text(
        max(Decimal('0'), Decimal(str(limit)) - Decimal(str(result['debt'])))))
    contents = [text(title, size='lg', weight='bold', color='#23A455'),
                text('客戶：' + result['customer'], size='sm', color='#777777'),
                text(result['expression'] + ' = ' + signed, size='sm', align='end', color='#A5754A'),
                dict(type='separator', margin='md', color='#EEEEEE'),
                row('上次欠款', 'NT$ ' + money_text(result['previous_debt'])),
                row('本次金額', signed + ' 元'),
                row('目前欠款', 'NT$ ' + money_text(result['debt']), True),
                row('剩餘額度', remaining),
                dict(type='separator', margin='md', color='#EEEEEE'),
                text('備註：' + (result['note'] or ('僅查詢，帳款未變更' if amount == 0 else '管理員手動記帳')),
                     size='xs', color='#777777'),
                text('正數收款減少欠款；負數增加欠款。\n拿卡已自動記帳，請勿再輸入同筆金額。', size='xxs', color='#999999')]
    if not result['enabled']:
        contents.append(text('自助拿卡目前關閉。', size='xs', color='#D95040'))
    return dict(type='bubble', size='kilo', body=dict(type='box', layout='vertical',
                paddingAll='16px', spacing='sm', contents=contents))


def reply_calculator(event, result, ns):
    summary = (f"{'【測試】' if result['customer']=='測試' else ''}計算機\n"
               f"{result['expression']} = {money_text(result['amount'])}\n"
               f"上次欠款：{money_text(result['previous_debt'])}元\n"
               f"目前欠款：{money_text(result['debt'])}元\n"
               + ('僅查詢，帳款未變更。' if result['amount']==0 else '已記帳，請勿重複輸入。'))
    try:
        from linebot.v3.messaging import (ApiClient, MessagingApi, ReplyMessageRequest,
                                         FlexMessage, FlexContainer)
        message = FlexMessage(alt_text=summary[:400], contents=FlexContainer.from_dict(calculator_bubble(result)))
        request = ReplyMessageRequest(reply_token=event.reply_token, messages=[message])
    except Exception:
        return ns['reply'](event, summary)
    # A timeout may follow a committed adjustment. Never retry or reverse it.
    with ApiClient(ns['configuration']) as client:
        MessagingApi(client).reply_message(request)
