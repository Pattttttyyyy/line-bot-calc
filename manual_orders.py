"""Manual sales importer: no serial, stock, or product-table writes."""
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from manual_order_fields import parse_manual_order_fields

SOURCE_METADATA_KEY = 'linebot_manual_sale_source'


def create_manual_order_no():
    now = datetime.now(ZoneInfo('Asia/Taipei'))
    return 'TX' + now.strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:6].upper()


def normalized_row(row):
    row = list(row)
    while row and not str(row[-1]).strip():
        row.pop()
    return row


def allocate_order_no(conn, product_code, sale_date=None):
    sale_date = sale_date or datetime.now(ZoneInfo('Asia/Taipei')).date()
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO order_number_counters(product_code,last_value)
            VALUES (%s,1) ON CONFLICT(product_code) DO UPDATE
            SET last_value=order_number_counters.last_value+1 RETURNING last_value''',
            (product_code,))
        row = cur.fetchone()
        sequence = row['last_value'] if isinstance(row, dict) else row[0]
    return f'{product_code}-{sale_date:%m%d}-{sequence:03d}'


def source_keys(ws):
    result = {}
    response = ws.spreadsheet.client.request('post',
        'https://sheets.googleapis.com/v4/spreadsheets/' + ws.spreadsheet.id + '/developerMetadata:search',
        json={'dataFilters':[{'developerMetadataLookup':{'metadataKey':SOURCE_METADATA_KEY}}]})
    metadata = response.json()
    for match in metadata.get('matchedDeveloperMetadata', []):
        item = match['developerMetadata']
        location = item.get('location', {}).get('dimensionRange', {})
        if (item.get('metadataKey') == SOURCE_METADATA_KEY
                and location.get('sheetId') == ws.id
                and location.get('dimension') == 'ROWS'):
            result[location['startIndex'] + 1] = item['metadataValue']
    return result


def attach_source_key(ws, row_no):
    key = str(uuid.uuid4())
    ws.spreadsheet.batch_update({'requests':[{'createDeveloperMetadata':{
        'developerMetadata':{'metadataKey':SOURCE_METADATA_KEY, 'metadataValue':key,
            'visibility':'DOCUMENT', 'location':{'dimensionRange':{
                'sheetId':ws.id, 'dimension':'ROWS',
                'startIndex':row_no-1, 'endIndex':row_no}}}}}]})
    return key


def import_manual_orders(ws, get_db, context_id, operator, resolve_product):
    lock_conn = get_db()
    try:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", ('manual-sales-sheet:' + str(ws.id),))
        return _import_manual_orders(ws, get_db, context_id, operator, resolve_product)
    finally:
        # Closing the session releases its advisory lock, including on error.
        lock_conn.close()


def _import_manual_orders(ws, get_db, context_id, operator, resolve_product):
    from gspread.utils import rowcol_to_a1
    values = ws.get_all_values()
    if not values:
        return {'created':0, 'invalid':[]}
    headers = [str(v).strip() for v in values[0]]
    required = ['日期','客戶','數量（或折扣）','單價','售價金額','成本','利潤',
                '商品（或服務）','訂單編號','同步狀態','最後同步時間']
    if any(h not in headers for h in required):
        raise ValueError('銷售分頁缺少必要欄位')
    indexes = {h:headers.index(h) for h in required}
    keys = source_keys(ws)
    result = {'created':0, 'invalid':[]}
    for row_no, row in enumerate(values[1:], 2):
        cell = lambda h: str(row[indexes[h]]).strip() if indexes[h] < len(row) else ''
        if not any(str(v).strip() for v in row) or cell('同步狀態') == '已撤回':
            continue
        if cell('訂單編號') and row_no not in keys:
            continue
        try:
            fields = parse_manual_order_fields(cell)
        except ValueError as error:
            result['invalid'].append(f'第 {row_no} 列：{error}')
            continue
        # Canonical aliases share the same sequence as bot-generated sales.
        product_conn = get_db()
        try:
            product = resolve_product(product_conn, fields['product'])
        finally:
            product_conn.close()
        if product:
            fields['product'] = product['code']
        if normalized_row(ws.row_values(row_no)) != normalized_row(row):
            raise ValueError('同步期間表格已變更，請重新同步')
        source_key = keys.get(row_no) or attach_source_key(ws, row_no)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (source_key,))
                cur.execute('SELECT order_no,state,product_code,sale_date,quantity FROM manual_sales WHERE source_key=%s', (source_key,))
                existing = cur.fetchone()
                if existing:
                    if isinstance(existing, dict):
                        order_no, state = existing['order_no'], existing['state']
                        original = (existing['product_code'], existing['sale_date'], existing['quantity'])
                    else:
                        order_no, state, *original = existing
                    if tuple(original) != (fields['product'], fields['sale_date'], fields['quantity']):
                        raise ValueError('已登記的手填訂單不可更改商品、日期或數量')
                else:
                    if cell('訂單編號'):
                        raise ValueError('手填訂單尚未登記，請保留訂單編號空白')
                    order_no = create_manual_order_no()
                    cur.execute('''INSERT INTO manual_sales
                        (source_key,order_no,product_code,sale_date,sold_to,quantity,
                         unit_price,sale_amount,cost,profit,group_id,operator)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (source_key,order_no,fields['product'],fields['sale_date'],
                         fields['customer'],fields['quantity'],fields['unit_price'],
                         fields['sale_amount'],fields['cost'],fields['profit'],context_id,operator))
                    state = 'pending'
            # Persist the reservation first. A failed Sheet write is safely retryable.
            conn.commit()
            # Row metadata follows inserted/moved rows. Re-find it before writing.
            locations = [r for r, key in source_keys(ws).items() if key == source_key]
            if len(locations) != 1:
                raise ValueError('手填訂單所在列已變更，請重新同步')
            target_row = locations[0]
            current_row = ws.row_values(target_row)
            if normalized_row(current_row) != normalized_row(row):
                raise ValueError('同步期間表格已變更，請重新同步')
            if cell('訂單編號') and cell('訂單編號') != order_no:
                raise ValueError('手填訂單編號與原登記不一致，請勿手動更改')
            if not cell('訂單編號'):
                ws.update_acell(rowcol_to_a1(target_row,indexes['訂單編號']+1),order_no)
            if state == 'pending':
                with conn.cursor() as cur:
                    cur.execute("UPDATE manual_sales SET state='active' WHERE source_key=%s AND state='pending'", (source_key,))
                conn.commit()
                result['created'] += 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return result
