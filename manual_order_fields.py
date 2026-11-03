"""Validation for manually entered sales. Never selects or changes stock serials."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def parse_manual_order_fields(cell):
    required = ['日期', '客戶', '商品（或服務）', '數量（或折扣）',
                '單價', '售價金額', '成本', '利潤']
    missing = [name for name in required if not str(cell(name)).strip()]
    if missing:
        raise ValueError('手填訂單缺少：' + '、'.join(missing))
    raw_date = str(cell('日期')).strip()
    try:
        parts = re.split(r'[/.-]', raw_date)
        if len(parts) != 3 or len(parts[0]) != 4:
            raise ValueError()
        sale_date = date(*map(int, parts))
    except (ValueError, TypeError):
        raise ValueError('日期請填 YYYY/MM/DD')
    numbers = {}
    for field in ['數量（或折扣）', '單價', '售價金額', '成本', '利潤']:
        raw = str(cell(field)).strip().replace(',', '').replace('NT$', '').replace('$', '')
        try:
            number = Decimal(raw)
            if not number.is_finite():
                raise InvalidOperation()
        except InvalidOperation:
            raise ValueError(field + '不是有效數字')
        numbers[field] = number
    quantity = numbers['數量（或折扣）']
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ValueError('手填銷售數量必須是正整數')
    product = str(cell('商品（或服務）')).strip()
    if len(product) > 80 or any(ord(c) < 32 for c in product):
        raise ValueError('商品名稱格式不正確')
    return {'sale_date': sale_date, 'product': product,
            'customer': str(cell('客戶')).strip(), 'quantity': int(quantity),
            'unit_price': numbers['單價'], 'sale_amount': numbers['售價金額'],
            'cost': numbers['成本'], 'profit': numbers['利潤']}
