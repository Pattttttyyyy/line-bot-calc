"""Bound-group calculator. No eval, external calls, or startup side effects."""
import ast
import re
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext
from group_credit import CreditError, GroupCredit, MAX_MONEY

TRANSLATION = str.maketrans('＋－−×÷（）０１２３４５６７８９', '+--*/()0123456789')


def looks_like_calculation(text):
    text = text.strip().translate(TRANSLATION)
    return text.startswith('/計算') or bool(re.match(r'^[+-]\s*[0-9.(]', text))


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
        result = service.calculate(group, operator, str(getattr(event.message, 'id', '') or ''),
                                   event.message.text, ns['is_admin'](operator))
        if result.get('duplicate'):
            ns['reply'](event, '這則算式已記帳，不會重複加減。請傳 +0 查看目前帳況。')
        else:
            from receipt_cards import reply_calculator
            reply_calculator(event, result, ns)
        return True
    except CreditError as exc:
        ns['reply'](event, str(exc))
        return True
