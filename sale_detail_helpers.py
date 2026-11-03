"""Sheet-only sale details; never changes inventory or transactions."""
from datetime import datetime
import re

def sheet_sale_time(order_no):
    match = re.match(r'^TX(\d{14})', str(order_no or ''))
    if not match:
        return ''
    try:
        value = datetime.strptime(match.group(1), '%Y%m%d%H%M%S')
    except ValueError:
        return ''
    return (value - datetime(1899, 12, 30)).total_seconds() / 86400

def visible_card_numbers(serials):
    return '\n'.join(str(s).split()[0] for s in serials if str(s).split())

def write_appended_sale_serials(ws, response, items):
    # Sales sheets no longer display serials; retain the hook for compatibility.
    return
