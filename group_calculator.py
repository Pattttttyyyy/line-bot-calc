"""Bound-group calculator. No eval, external calls, or startup side effects."""
import ast
import re
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
from group_credit import CreditError, GroupCredit, MAX_MONEY, balance_values

TRANSLATION = str.maketrans('＋－−×÷（）０１２３４５６７８９', '+--*/()0123456789')
LEDGER_COMMANDS = ('/記帳紀錄', '/更正記帳', '/撤銷記帳', '/撤回', '/撤回上一動', '/確認記帳', '/取消記帳')


def looks_like_calculation(text):
    text = text.strip().translate(TRANSLATION)
    command = text.split(maxsplit=1)[0] if text else ''
    return (command == '/計算' or command in LEDGER_COMMANDS
            or bool(re.match(r'^[+-]\s*[0-9.(]', text)))


def reply_ledger(event, result, ns):
    if result.get('needs_confirmation'):
        amount = Decimal(result['amount'])
        change = ('扣預付款／增加欠款 ' if amount < 0 else '結欠款／增加預付款 ') + f'{abs(amount):,.2f}元'
        title = ('⚠️ 120秒內出現相同金額，本次尚未記帳。' if result['request_kind']=='calculator_repeat_pending'
                 else '更正申請：沖回原帳，保留原紀錄。本次尚未更正。')
        detail = ('算式：'+result['expression'] if result.get('expression') else '原記帳：'+result['reference'])
        ns['reply'](event, title+'\n'+detail+'\n本次將'+change+'\n備註：'+result.get('note','')+
                    '\n5分鐘內確認請複製整行：\n/確認記帳 '+result['token']+
                    '\n取消請輸入：\n/取消記帳 '+result['token'])
    elif result.get('declined') or result.get('kind')=='calculator_declined':
        ns['reply'](event, '已取消確認；本次未調整欠款。')
    elif result.get('duplicate'):
        ns['reply'](event, '這則指令已處理，不會重複調帳。請傳 +0 查帳，或 /記帳紀錄 查看明細。')
    else:
        from receipt_cards import reply_calculator
        reply_calculator(event, result, ns)


def parse_calculation(raw):
    if not isinstance(raw, str) or len(raw) > 260:
        raise CreditError('算式或備註太長。')
    text = raw.strip().translate(TRANSLATION)
    if text.startswith('/計算 '):
        text = text[len('/計算 '):].strip()
    expression, _, note = text.partition('#')
    expression, note = expression.strip(), note.strip()
    if (not expression.startswith(('+', '-')) or len(expression) > 128
            or len(note) > 60 or not re.fullmatch(r'[0-9.+*/()\- \t]+', expression)):
        raise CreditError('格式：+0 查帳；+1000 收款；-1000*0.905 增加欠款。備註可加 #原因。')
    try:
        tree = ast.parse(expression, mode='eval')
        if sum(1 for _ in ast.walk(tree)) > 64:
            raise ValueError('Too many nodes')

        def number(node, depth=0):
            if depth > 12:
                raise ValueError('Too deep')
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                source = ast.get_source_segment(expression, node)
                if not re.fullmatch(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', source):
                    raise ValueError('Invalid number')
                value = Decimal(source)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = number(node.operand, depth + 1)
                if isinstance(node.op, ast.USub):
                    value = -value
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                left, right = number(node.left, depth + 1), number(node.right, depth + 1)
                if isinstance(node.op, ast.Add): value = left + right
                elif isinstance(node.op, ast.Sub): value = left - right
                elif isinstance(node.op, ast.Mult): value = left * right
                else: value = left / right
            else:
                raise ValueError('Unsupported operation')
            if not value.is_finite() or abs(value) > MAX_MONEY:
                raise ValueError('Out of range')
            return value

        with localcontext() as ctx:
            # Source is limited to 128 characters. Keep enough precision for
            # all source digits and guard digits before the single cent rounding.
            ctx.prec = 160
            amount = number(tree.body).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if abs(amount) > MAX_MONEY:
            raise ValueError('Out of range')
        if amount == 0:
            amount = Decimal('0.00')
        return expression, amount, note
    except (SyntaxError, ValueError, DecimalException, RecursionError):
        raise CreditError('算式無效：僅支援加減乘除、小括號及有效金額，不能除以0。') from None


def handle_calculator(event, ns):
    group = ns['get_context_id'](event)
    if not str(group).startswith('group:'):
        return False
    service = GroupCredit(ns['get_db'], ns['RealDictCursor'], ns['find_product'],
                          ns['create_order_no'], ns['split_text_chunks'])
    if not service.registered(group):
        return False
    try:
        operator = ns['get_operator'](event)
        admin = ns['is_admin'](operator)
        text = event.message.text.strip()
        parts = text.split(maxsplit=2)
        command = parts[0] if parts else ''
        from ledger_controls import history, request_reverse, confirm
        if command == '/記帳紀錄':
            if len(parts)!=1: raise CreditError('格式：/記帳紀錄（顯示最近10筆）。')
            rows = history(service, group, operator, admin)
            from zoneinfo import ZoneInfo
            lines = ['本群最近10筆帳目｜正數增加欠款、負數減少欠款']
            labels = dict(calculator='手動記帳',charge='增加欠款',payment='登記收款',
                          adjust_credit='減少欠款',sale='自助拿卡',cancel='發卡撤回',calculator_reversal='記帳更正')
            for row in rows:
                reference = row['order_no'] or row['event_id']
                when = row['created_at'].astimezone(ZoneInfo('Asia/Taipei')).strftime('%m/%d %H:%M')
                owed, prepaid = balance_values(row['debt_after'])
                if row['kind']=='sale' and row['reason'] in ('sheet:pending:v1','sheet:done:v1'):
                    row['reason']='銷售試算表待同步' if row['reason']=='sheet:pending:v1' else '銷售試算表已同步'
                lines.append(f"\n{when} {labels[row['kind']]}{'（已更正）' if row['corrected'] else ''}\n編號：{reference}\n帳款變動：{row['amount']:+,.2f}元\n欠款：{owed:,.2f}元；預付款：{prepaid:,.2f}元\n{row['reason'][:120]}")
            ns['reply'](event, '\n'.join(lines) if rows else '本群目前沒有記帳或拿卡紀錄。')
            return True
        if command in ('/撤回','/撤回上一動'):
            reference = parts[1] if len(parts)>1 and parts[1]!='上一動' else None
            from ledger_controls import undo_previous
            result = undo_previous(service,group,operator,str(getattr(event.message,'id','') or ''),admin,reference)
            if result.get('undo_kind')=='sale':
                from group_credit_handler import reply_cancel_result
                return reply_cancel_result(event,result,ns)
            from group_credit import balance_text
            if result.get('duplicate'):
                ns['reply'](event,'此指令已處理，不會再次沖回。\n撤回編號：'+str(result.get('record_id','請查記帳紀錄')))
            else:
                ns['reply'](event,f"↩️ 已撤回記帳\n撤回編號：{result['record_id']}\n原記帳：{result['source_reference']}\n帳款調整：{result['amount']:+,.2f}元（正數退款／負數扣回款項）\n{balance_text(result['debt'])}")
            return True
        elif command in ('/更正記帳','/撤銷記帳'):
            if len(parts)!=3: raise CreditError('格式：/更正記帳 記帳編號 原因；會先顯示文字確認。')
            result = request_reverse(service,group,operator,str(getattr(event.message,'id','') or ''),parts[1],parts[2],admin)
        elif command in ('/確認記帳','/取消記帳'):
            if len(parts)!=2: raise CreditError('請複製申請回覆中的完整確認或取消指令。')
            result = confirm(service,group,operator,parts[1],admin,command=='/取消記帳')
        else:
            result = service.calculate(group, operator, str(getattr(event.message, 'id', '') or ''),text,admin)
        if result.get('undo_kind')=='sale':
            from group_credit_handler import reply_cancel_prompt
            reply_cancel_prompt(event,result,ns)
        else:
            reply_ledger(event,result,ns)
        return True
    except CreditError as exc:
        ns['reply'](event, str(exc))
        return True
