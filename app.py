import os
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Flask, request, abort

import gspread
from google.oauth2.service_account import Credentials

import psycopg2
from psycopg2.extras import RealDictCursor

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent


app = Flask(__name__)


# ==================================================
# LINE
# ==================================================

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(
    access_token=CHANNEL_ACCESS_TOKEN
)


# ==================================================
# Google Sheet
# ç®ååªä¿çãå¸³åè¡¨ã
# ==================================================

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

GOOGLE_CREDENTIALS_FILE = (
    "/etc/secrets/google-service-account.json"
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets"
]

credentials = Credentials.from_service_account_file(
    GOOGLE_CREDENTIALS_FILE,
    scopes=SCOPES,
)

gc = gspread.authorize(credentials)

spreadsheet = gc.open_by_key(
    GOOGLE_SHEET_ID
)

account_sheet = spreadsheet.worksheet(
    "å¸³åè¡¨"
)

SALES_SHEET_NAME = "é·å®ç´é"


def get_sales_sheet():
    """
    é·å®ç´éæ¡æ¶è¼å¥ã
    å³ä½¿ Google Sheet æ«ææåé¡ï¼ä¹ä¸è¦è®æ´å° LINE Bot ååå¤±æã
    """
    return spreadsheet.worksheet(
        SALES_SHEET_NAME
    )


# ==================================================
# Supabase PostgreSQL
# ==================================================

DATABASE_URL = os.getenv("DATABASE_URL")

# å¿«éåºåº«å¥å
# /å¤§10ã/å¤§*10ã/1490*60 ... æè½ææ­£å¼åå code
QUICK_PRODUCT_ALIASES = {
    "å¤§": "å¤§å¡",
    "å°": "å°å¡",
    "500": "è²500",
    "1000": "è²1000",
    "1490": "è²1490",
    "2990": "è²2990",
    "M3": "MY3000",
    "M5": "MY5000",
    "M1W": "MY1è¬",
}


# LINE ç®¡çå¡åå®
# Render ç°å¢è®æ¸ä»æ²¿ç¨ï¼ALLOWED_LINE_USER_IDS
# éè£¡æ¾çæ¯ãç®¡çå¡ãLINE User ID
# å¤åç®¡çå¡ç¨éèåé
ADMIN_LINE_USER_IDS = {
    item.strip()
    for item in os.getenv(
        "ALLOWED_LINE_USER_IDS",
        ""
    ).split(",")
    if item.strip()
}


def get_db():
    conn = psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=10,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SET TIME ZONE 'Asia/Taipei'"
        )

    return conn


# ==================================================
# å±ç¨å·¥å·
# ==================================================

def now_tw():
    return datetime.now(
        ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M:%S")


def get_operator(event):
    try:
        return event.source.user_id or "æªç¥"
    except Exception:
        return "æªç¥"


def get_context_id(event):
    """
    ä»¥ LINE ç¾¤çµçºåªåã
    ç¾¤çµï¼group:<group_id>
    å¤äººèå¤©å®¤ï¼room:<room_id>
    ä¸å°ä¸èå¤©ï¼user:<user_id>
    """
    try:
        group_id = getattr(event.source, "group_id", None)
        if group_id:
            return f"group:{group_id}"

        room_id = getattr(event.source, "room_id", None)
        if room_id:
            return f"room:{room_id}"

        user_id = getattr(event.source, "user_id", None)
        if user_id:
            return f"user:{user_id}"

    except Exception:
        pass

    return "unknown"


def is_admin(operator):
    """Render ALLOWED_LINE_USER_IDS å§çäºº = ç®¡çå¡ã"""
    return operator in ADMIN_LINE_USER_IDS


def is_authorized_user(operator):
    """ä¸è¬ä½¿ç¨èæ¬éå­æ¾å¨ Supabase authorized_usersã"""
    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM authorized_users
                WHERE line_user_id = %s
                  AND active = TRUE
                LIMIT 1
                """,
                (operator,),
            )

            return cur.fetchone() is not None

    finally:
        conn.close()


def can_use_bot(operator):
    """ç®¡çå¡æ active çä¸è¬ä½¿ç¨èé½å¯ä»¥ä½¿ç¨æ©å¨äººã"""
    if is_admin(operator):
        return True

    return is_authorized_user(operator)


def add_authorized_user(line_user_id):
    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO authorized_users (
                    line_user_id,
                    active
                )
                VALUES (%s, TRUE)
                ON CONFLICT (line_user_id)
                DO UPDATE SET
                    active = TRUE
                """,
                (line_user_id,),
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def remove_authorized_user(line_user_id):
    conn = get_db()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE authorized_users
                SET active = FALSE
                WHERE line_user_id = %s
                  AND active = TRUE
                """,
                (line_user_id,),
            )

            changed = cur.rowcount

        conn.commit()

        return changed > 0

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def get_authorized_users():
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT
                    line_user_id,
                    note,
                    created_at
                FROM authorized_users
                WHERE active = TRUE
                ORDER BY created_at
                """
            )

            return cur.fetchall()

    finally:
        conn.close()


def reply(event, text):
    # LINE å®åæå­è¨æ¯é¿åéé·
    if len(text) > 4900:
        text = text[:4850] + "\n\nâ ï¸ å§å®¹éé·ï¼å·²æªç­ã"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(text=text)
                ],
            )
        )


def reply_messages(event, texts):
    """ä¸æ¬¡åè¦å¤å LINE æå­æ³¡æ³¡ã"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(text=str(item)[:4900])
                    for item in texts
                ],
            )
        )


def create_batch_no():
    now = datetime.now(
        ZoneInfo("Asia/Taipei")
    )

    suffix = uuid.uuid4().hex[:6].upper()

    return (
        "IN"
        + now.strftime("%Y%m%d%H%M%S")
        + suffix
    )


def create_order_no():
    now = datetime.now(
        ZoneInfo("Asia/Taipei")
    )

    suffix = uuid.uuid4().hex[:6].upper()

    return (
        "TX"
        + now.strftime("%Y%m%d%H%M%S")
        + suffix
    )


def format_db_time(value):
    if not value:
        return ""

    try:
        return value.astimezone(
            ZoneInfo("Asia/Taipei")
        ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


# ==================================================
# ååæ¥è©¢
# ååä»£ç¢¼å¿½ç¥è±æå¤§å°å¯«
# ==================================================

def find_product(conn, product_text):
    with conn.cursor(
        cursor_factory=RealDictCursor
    ) as cur:

        cur.execute(
            """
            SELECT id, code, name, active
            FROM products
            WHERE LOWER(code) = LOWER(%s)
              AND active = TRUE
            LIMIT 1
            """,
            (product_text,),
        )

        return cur.fetchone()


# ==================================================
# Google Sheet å¸³å
# ==================================================

def get_customer_balance(customer):
    records = account_sheet.get_all_records()

    balance = 0

    for row in records:
        if (
            str(row.get("å®¢æ¶", "")).strip()
            == customer
        ):
            try:
                balance += int(
                    str(row.get("éé¡", 0))
                    .replace(",", "")
                )
            except Exception:
                pass

    return balance



# ==================================================
# Google Sheetï¼é·å®ç´é
#
# æ¬ä½ï¼
# æ¥æãå®¢æ¶ãæ¸éï¼æææ£ï¼ãå®å¹ãå®å¹éé¡ãææ¬ãå©æ½¤ã
# ååï¼ææåï¼ãè¨å®ç·¨èãåæ­¥çæãæå¾åæ­¥æé
# ==================================================

def sales_date_tw():
    return datetime.now(
        ZoneInfo("Asia/Taipei")
    ).strftime("%Y/%m/%d")


def append_sales_rows(
    customer,
    items,
):
    """
    åºåº«å®æå¾ï¼ææ¯ååååå¯«ä¸åå°ãé·å®ç´éãã

    items æ¯ç­éè¦ï¼
    - product.code
    - quantity
    - order_no

    ç®åï¼
    - å®¢æ¶æå°±å¸¶å¥ï¼æ²æå¯ç©ºç½
    - å®å¹ / å®å¹éé¡ / ææ¬ / å©æ½¤åç©ºç½
    - åæ­¥çæåºå®ãå¾è£ã
    - æå¾åæ­¥æéåç©ºç½

    Google Sheet å¯«å¥å¤±æä¸åæ»¾å·²æåçæ­£å¼åºåº«ï¼
    åªåå³ Falseï¼é¿å Sheet æ«ææéé æåº«å­äº¤æå¤±æã
    """
    try:
        sales_sheet = get_sales_sheet()

        rows = []

        for item in items:
            rows.append([
                sales_date_tw(),
                customer or "",
                item["quantity"],
                "",
                "",
                "",
                "",
                item["product"]["code"],
                item["order_no"],
                "å¾è£",
                "",
            ])

        if rows:
            sales_sheet.append_rows(
                rows,
                value_input_option="USER_ENTERED",
            )

        return True

    except Exception as e:
        print(
            "SALES SHEET APPEND ERROR:",
            repr(e),
        )
        return False


def mark_sales_rows_undone(order_nos):
    """
    æ¤ååºåº«å¾ï¼ä¸åªé¤ Google Sheet æ­·å²ç´éï¼
    èæ¯æå°æè¨å®çãåæ­¥çæãæ¨è¨çºãå·²æ¤åãã
    """
    if not order_nos:
        return True

    try:
        sales_sheet = get_sales_sheet()
        values = sales_sheet.get_all_values()

        if not values:
            return True

        headers = [
            str(value).strip()
            for value in values[0]
        ]

        required = {
            "è¨å®ç·¨è",
            "åæ­¥çæ",
            "æå¾åæ­¥æé",
        }

        if not required.issubset(
            set(headers)
        ):
            print(
                "SALES SHEET UNDO ERROR: "
                "ç¼ºå°å¿è¦æ¬ä½"
            )
            return False

        order_col = headers.index(
            "è¨å®ç·¨è"
        )
        status_col = headers.index(
            "åæ­¥çæ"
        )
        synced_col = headers.index(
            "æå¾åæ­¥æé"
        )

        target_order_nos = {
            str(value).strip()
            for value in order_nos
        }

        updates = []
        sync_time = now_tw()

        for sheet_row, row in enumerate(
            values[1:],
            start=2,
        ):
            order_value = (
                str(row[order_col]).strip()
                if order_col < len(row)
                else ""
            )

            if order_value not in target_order_nos:
                continue

            # ä¸æ¬¡æ´æ°ãåæ­¥çæãèãæå¾åæ­¥æéãå©æ¬ã
            start_col = status_col + 1
            end_col = synced_col + 1

            # ç®åå©æ¬å¨ä½¿ç¨èæå®è¡¨æ ¼ä¸­æ¯ç¸é°ç JãKã
            # è¥æªä¾æ¬ä½ç§»åï¼ä»ä»¥å¯¦éæ¨é¡ä½ç½®è¨ç®ã
            if end_col == start_col + 1:
                from gspread.utils import rowcol_to_a1

                start_a1 = rowcol_to_a1(
                    sheet_row,
                    start_col,
                )
                end_a1 = rowcol_to_a1(
                    sheet_row,
                    end_col,
                )

                updates.append({
                    "range": (
                        f"{start_a1}:{end_a1}"
                    ),
                    "values": [[
                        "å·²æ¤å",
                        sync_time,
                    ]],
                })
            else:
                from gspread.utils import rowcol_to_a1

                status_a1 = rowcol_to_a1(
                    sheet_row,
                    start_col,
                )
                synced_a1 = rowcol_to_a1(
                    sheet_row,
                    end_col,
                )

                updates.extend([
                    {
                        "range": status_a1,
                        "values": [[
                            "å·²æ¤å"
                        ]],
                    },
                    {
                        "range": synced_a1,
                        "values": [[
                            sync_time
                        ]],
                    },
                ])

        if updates:
            sales_sheet.batch_update(
                updates,
                value_input_option="USER_ENTERED",
            )

        return True

    except Exception as e:
        print(
            "SALES SHEET UNDO ERROR:",
            repr(e),
        )
        return False


# ==================================================
# è£å®ï¼è£ä¸æ¢æè¨å®çå®¢æ¶
# ==================================================

def get_sale_batch_by_ref(conn, order_or_batch_no):
    """
    å¯æ¥åå®å order_noãå¤åé  sale_batch_noï¼
    æå¤åé å¶ä¸­ä¸å child order_noã
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(sale_batch_no, order_no) AS batch_no
            FROM transactions
            WHERE LOWER(order_no) = LOWER(%s)
               OR LOWER(COALESCE(sale_batch_no, order_no)) = LOWER(%s)
            ORDER BY id
            LIMIT 1
            """,
            (order_or_batch_no, order_or_batch_no),
        )
        row = cur.fetchone()

        if not row:
            return None, []

        batch_no = row["batch_no"]

        cur.execute(
            """
            SELECT
                t.id,
                t.order_no,
                t.sale_batch_no,
                t.sold_to,
                t.quantity,
                t.product_id,
                t.group_id,
                p.code,
                p.name
            FROM transactions t
            JOIN products p
              ON p.id = t.product_id
            WHERE LOWER(COALESCE(t.sale_batch_no, t.order_no)) = LOWER(%s)
            ORDER BY t.id
            """,
            (batch_no,),
        )

        return batch_no, cur.fetchall()


def find_latest_blank_sale_batch(conn, context_id):
    """
    æ¾ç®åç¾¤çµæè¿ä¸ç­ãæ´æ¹å®¢æ¶çç©ºç½ãçåºåº«ã
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            WITH batches AS (
                SELECT
                    COALESCE(sale_batch_no, order_no) AS batch_no,
                    MAX(created_at) AS latest_at,
                    BOOL_AND(
                        COALESCE(BTRIM(sold_to), '') = ''
                    ) AS all_blank
                FROM transactions
                WHERE group_id = %s
                GROUP BY COALESCE(sale_batch_no, order_no)
            )
            SELECT batch_no
            FROM batches
            WHERE all_blank = TRUE
            ORDER BY latest_at DESC
            LIMIT 1
            """,
            (context_id,),
        )
        row = cur.fetchone()

        return row["batch_no"] if row else None


def update_sales_sheet_customer(order_nos, customer):
    """
    è£å®æåå¾ï¼åæ­¥æ´æ° Google Sheetãé·å®ç´éãå®¢æ¶æ¬ã
    ä¸ç¢°å¶ä»äººå·¥æ¬ä½ã
    """
    if not order_nos:
        return True

    try:
        sales_sheet = get_sales_sheet()
        values = sales_sheet.get_all_values()

        if not values:
            return True

        headers = [str(v).strip() for v in values[0]]

        for required in ("å®¢æ¶", "è¨å®ç·¨è", "æå¾åæ­¥æé"):
            if required not in headers:
                print(
                    "SALES SHEET CUSTOMER UPDATE ERROR:",
                    f"ç¼ºå°æ¬ä½ {required}",
                )
                return False

        customer_col = headers.index("å®¢æ¶")
        order_col = headers.index("è¨å®ç·¨è")
        sync_time_col = headers.index("æå¾åæ­¥æé")

        targets = {str(v).strip() for v in order_nos}

        from gspread.utils import rowcol_to_a1

        updates = []
        sync_time = now_tw()

        for sheet_row, row in enumerate(values[1:], start=2):
            order_value = (
                str(row[order_col]).strip()
                if order_col < len(row)
                else ""
            )

            if order_value not in targets:
                continue

            updates.extend([
                {
                    "range": rowcol_to_a1(
                        sheet_row,
                        customer_col + 1,
                    ),
                    "values": [[customer]],
                },
                {
                    "range": rowcol_to_a1(
                        sheet_row,
                        sync_time_col + 1,
                    ),
                    "values": [[sync_time]],
                },
            ])

        if updates:
            sales_sheet.batch_update(
                updates,
                value_input_option="USER_ENTERED",
            )

        return True

    except Exception as e:
        print(
            "SALES SHEET CUSTOMER UPDATE ERROR:",
            repr(e),
        )
        return False


def fill_order_customer(
    context_id,
    customer,
    order_or_batch_no=None,
):
    """
    /è£å® åªè£ç©ºç½å®¢æ¶ã
    å¦æåè¨å®å·²æå®¢æ¶ï¼ä¸ç´æ¥è¦èã
    """
    conn = get_db()

    try:
        if order_or_batch_no:
            batch_no, orders = get_sale_batch_by_ref(
                conn,
                order_or_batch_no,
            )
        else:
            batch_no = find_latest_blank_sale_batch(
                conn,
                context_id,
            )

            if not batch_no:
                return {
                    "ok": False,
                    "reason": "no_blank_order",
                }

            batch_no, orders = get_sale_batch_by_ref(
                conn,
                batch_no,
            )

        if not orders:
            return {
                "ok": False,
                "reason": "order_not_found",
            }

        # ååè¨±å¨ç®åç¾¤çµ/èå¤©å®¤è£å®ã
        if any(
            order["group_id"] != context_id
            for order in orders
        ):
            return {
                "ok": False,
                "reason": "wrong_context",
            }

        existing_customers = sorted({
            str(order["sold_to"] or "").strip()
            for order in orders
            if str(order["sold_to"] or "").strip()
        })

        if existing_customers:
            return {
                "ok": False,
                "reason": "already_has_customer",
                "customers": existing_customers,
                "batch_no": batch_no,
            }

        order_nos = [order["order_no"] for order in orders]

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE transactions
                SET sold_to = %s
                WHERE order_no = ANY(%s)
                """,
                (customer, order_nos),
            )

            cur.execute(
                """
                UPDATE serials
                SET sold_to = %s
                WHERE order_no = ANY(%s)
                """,
                (customer, order_nos),
            )

        conn.commit()

        sheet_ok = update_sales_sheet_customer(
            order_nos,
            customer,
        )

        return {
            "ok": True,
            "batch_no": batch_no,
            "customer": customer,
            "orders": orders,
            "order_nos": order_nos,
            "sheet_ok": sheet_ok,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ==================================================
# Google Sheet â Supabaseï¼åæ­¥è£å®
# ==================================================

SALES_REQUIRED_HEADERS = [
    "æ¥æ",
    "å®¢æ¶",
    "æ¸éï¼æææ£ï¼",
    "å®å¹",
    "å®å¹éé¡",
    "ææ¬",
    "å©æ½¤",
    "ååï¼ææåï¼",
    "è¨å®ç·¨è",
    "åæ­¥çæ",
    "æå¾åæ­¥æé",
]


def parse_optional_decimal(value):
    """
    Sheet ç©ºç½ -> None
    æ¯æ´ï¼
    9,170
    NT$9,170
    $9,170
    0.925
    """
    raw = str(value or "").strip()

    if raw == "":
        return None

    cleaned = (
        raw
        .replace(",", "")
        .replace("NT$", "")
        .replace("NTï¼", "")
        .replace("$", "")
        .replace("ï¼", "")
        .strip()
    )

    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(
            f"ä¸æ¯æææ¸å­ï¼{raw}"
        )


def decimals_equal(left, right):
    if left is None and right is None:
        return True

    if left is None or right is None:
        return False

    return Decimal(str(left)) == Decimal(str(right))


def normalize_customer(value):
    """
    å®¢æ¶ç©ºç½ä¹è¦è½è¦èã
    transactions.sold_to ç®åæ²¿ç¨ç©ºå­ä¸²ä»£è¡¨ãæªå¡«ãã
    """
    return str(value or "").strip()


def is_sales_row_complete(
    customer,
    unit_price,
    sale_amount,
    cost,
    profit,
):
    """
    ç¬¬ä¸çå®ææ¢ä»¶ï¼
    å®¢æ¶ãå®å¹ãå®å¹éé¡ãææ¬ãå©æ½¤é½æå¼ -> å·²åæ­¥
    åªè¦æä¸é ç©ºç½ -> å¾è£

    å³ä½¿ä»æ¯ãå¾è£ãï¼æå¡«æè¢«æ¸ç©ºçæ¬ä½ä¸æ¨£æåæ­¥å Supabaseã
    """
    return (
        customer != ""
        and unit_price is not None
        and sale_amount is not None
        and cost is not None
        and profit is not None
    )


def get_transaction_for_sheet_sync(
    conn,
    order_no,
):
    with conn.cursor(
        cursor_factory=RealDictCursor
    ) as cur:
        cur.execute(
            """
            SELECT
                order_no,
                sold_to,
                quantity,
                unit_price,
                sale_amount,
                cost,
                profit
            FROM transactions
            WHERE LOWER(order_no) = LOWER(%s)
            LIMIT 1
            """,
            (order_no,),
        )

        return cur.fetchone()


def update_transaction_from_sheet(
    conn,
    order_no,
    customer,
    unit_price,
    sale_amount,
    cost,
    profit,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE transactions
            SET
                sold_to = %s,
                unit_price = %s,
                sale_amount = %s,
                cost = %s,
                profit = %s
            WHERE LOWER(order_no) = LOWER(%s)
            """,
            (
                customer,
                unit_price,
                sale_amount,
                cost,
                profit,
                order_no,
            ),
        )


def sync_sales_sheet_to_supabase():
    """
    è®å Google Sheetãé·å®ç´éãï¼å°äººå·¥ä¿®æ¹åæ­¥å Supabaseã

    è¦åï¼
    - å®¢æ¶ / å®å¹ / å®å¹éé¡ / ææ¬ / å©æ½¤ï¼æå¼æç©ºç½é½ç§ Sheet åæ­¥
    - ç©ºç½å¯ä»¥æ¸æ Supabase åæ¬å¼
    - åå / æ¸é / è¨å®ç·¨èï¼ç®åä¸å¾ Sheet åå¯«ï¼é¿åç ´å£åº«å­èåºèéè¯
    - å·²æ¤åçåè·³é
    - å®æ´ -> å·²åæ­¥
    - éæç©ºç½ -> å¾è£
    """
    sales_sheet = get_sales_sheet()
    values = sales_sheet.get_all_values()

    if not values:
        return {
            "ok": True,
            "updated": 0,
            "unchanged": 0,
            "not_found": 0,
            "skipped_undone": 0,
            "invalid": [],
        }

    headers = [
        str(value).strip()
        for value in values[0]
    ]

    missing_headers = [
        header
        for header in SALES_REQUIRED_HEADERS
        if header not in headers
    ]

    if missing_headers:
        return {
            "ok": False,
            "reason": "missing_headers",
            "missing_headers": missing_headers,
        }

    idx = {
        header: headers.index(header)
        for header in SALES_REQUIRED_HEADERS
    }

    conn = get_db()

    updated = 0
    unchanged = 0
    not_found = 0
    skipped_undone = 0
    invalid = []
    sheet_updates = []

    try:
        from gspread.utils import rowcol_to_a1

        for sheet_row_no, row in enumerate(
            values[1:],
            start=2,
        ):
            def cell(header):
                col = idx[header]
                if col >= len(row):
                    return ""
                return str(row[col]).strip()

            order_no = cell("è¨å®ç·¨è")
            status = cell("åæ­¥çæ")

            # ç©ºç½åç´æ¥è·³é
            if not order_no:
                if any(
                    str(value).strip()
                    for value in row
                ):
                    invalid.append(
                        f"ç¬¬ {sheet_row_no} åï¼è¨å®ç·¨èç©ºç½"
                    )
                continue

            if status == "å·²æ¤å":
                skipped_undone += 1
                continue

            try:
                customer = normalize_customer(
                    cell("å®¢æ¶")
                )
                unit_price = parse_optional_decimal(
                    cell("å®å¹")
                )
                sale_amount = parse_optional_decimal(
                    cell("å®å¹éé¡")
                )
                cost = parse_optional_decimal(
                    cell("ææ¬")
                )
                profit = parse_optional_decimal(
                    cell("å©æ½¤")
                )
            except ValueError as e:
                invalid.append(
                    f"ç¬¬ {sheet_row_no} åï¼{e}"
                )
                continue

            current = get_transaction_for_sheet_sync(
                conn,
                order_no,
            )

            if not current:
                not_found += 1
                invalid.append(
                    f"ç¬¬ {sheet_row_no} åï¼æ¾ä¸å°è¨å® {order_no}"
                )
                continue

            changed = (
                normalize_customer(
                    current["sold_to"]
                ) != customer
                or not decimals_equal(
                    current["unit_price"],
                    unit_price,
                )
                or not decimals_equal(
                    current["sale_amount"],
                    sale_amount,
                )
                or not decimals_equal(
                    current["cost"],
                    cost,
                )
                or not decimals_equal(
                    current["profit"],
                    profit,
                )
            )

            new_status = (
                "å·²åæ­¥"
                if is_sales_row_complete(
                    customer,
                    unit_price,
                    sale_amount,
                    cost,
                    profit,
                )
                else "å¾è£"
            )

            status_changed = (
                status != new_status
            )

            if changed:
                update_transaction_from_sheet(
                    conn,
                    order_no,
                    customer,
                    unit_price,
                    sale_amount,
                    cost,
                    profit,
                )
                updated += 1
            else:
                unchanged += 1

            # æè³æè®åæçæéè¦ä¿®æ­£æï¼æ´æ° Sheet çæèæéã
            if changed or status_changed:
                status_a1 = rowcol_to_a1(
                    sheet_row_no,
                    idx["åæ­¥çæ"] + 1,
                )
                time_a1 = rowcol_to_a1(
                    sheet_row_no,
                    idx["æå¾åæ­¥æé"] + 1,
                )

                sheet_updates.extend([
                    {
                        "range": status_a1,
                        "values": [[
                            new_status
                        ]],
                    },
                    {
                        "range": time_a1,
                        "values": [[
                            now_tw()
                        ]],
                    },
                ])

        conn.commit()

        if sheet_updates:
            sales_sheet.batch_update(
                sheet_updates,
                value_input_option="USER_ENTERED",
            )

        return {
            "ok": True,
            "updated": updated,
            "unchanged": unchanged,
            "not_found": not_found,
            "skipped_undone": skipped_undone,
            "invalid": invalid,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ==================================================
# Supabaseï¼ææåº«å­
# ==================================================

def get_all_inventory():
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    p.code,
                    p.name,
                    COUNT(s.id) AS stock
                FROM products p
                LEFT JOIN serials s
                  ON s.product_id = p.id
                 AND s.status = 'available'
                WHERE p.active = TRUE
                GROUP BY
                    p.id,
                    p.code,
                    p.name
                ORDER BY LOWER(p.code)
                """
            )

            return cur.fetchall()

    finally:
        conn.close()


# ==================================================
# Supabaseï¼å®ä¸åååº«å­
# ==================================================

def get_product_inventory(product_text):
    conn = get_db()

    try:
        product = find_product(
            conn,
            product_text
        )

        if not product:
            return None, None

        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*)
                FROM serials
                WHERE product_id = %s
                  AND status = 'available'
                """,
                (product["id"],),
            )

            count = cur.fetchone()[0]

        return product, count

    finally:
        conn.close()


def resolve_quick_product_alias(value):
    key = value.strip()

    # è±æå­æ¯å¥åå¿½ç¥å¤§å°å¯«
    for alias, product_code in QUICK_PRODUCT_ALIASES.items():
        if alias.upper() == key.upper():
            return product_code

    # ä¸å¨å¥åè¡¨æï¼ç´æ¥æè¼¸å¥ç¶æ­£å¼åå code
    return key


def parse_quick_sale_line(line):
    """
    æ¯æ´ï¼
    /å¤§10
    /å¤§*10
    /1490*60
    /åºå¤§10
    /ç¼å¤§*10
    /MY3000*5

    ç¡ * æï¼çºé¿åæ­£å¼åå code èæ¸éé»å¨ä¸èµ·ç¢çæ­§ç¾©ï¼
    åªæ¥åå·²è¨­å®çå¿«éå¥åã
    """
    value = line.strip()

    if not value.startswith("/"):
        return None

    body = value[1:].strip()

    if body.startswith("åº") or body.startswith("ç¼"):
        body = body[1:].strip()

    if not body:
        return {
            "ok": False,
            "reason": "empty",
        }

    # æ *ï¼å·¦éå¯ç¨å¥åï¼ä¹å¯ç´æ¥æ­£å¼åå code
    if "*" in body:
        left, right = body.rsplit("*", 1)

        product_key = left.strip()
        qty_text = right.strip()

        if not product_key or not qty_text.isdigit():
            return {
                "ok": False,
                "reason": "format",
            }

        return {
            "ok": True,
            "product_text": resolve_quick_product_alias(
                product_key
            ),
            "quantity": int(qty_text),
        }

    # ç¡ *ï¼åªå°å¿«éå¥ååãå¥å + æ¸éãè¾¨è­
    # é·å¥ååªåï¼é¿å 1 / 10 / 1000 é¡åèª¤å¤
    aliases = sorted(
        QUICK_PRODUCT_ALIASES.keys(),
        key=len,
        reverse=True,
    )

    for alias in aliases:
        if body[:len(alias)].upper() == alias.upper():
            qty_text = body[len(alias):].strip()

            if qty_text.isdigit():
                return {
                    "ok": True,
                    "product_text": QUICK_PRODUCT_ALIASES[alias],
                    "quantity": int(qty_text),
                }

    return {
        "ok": False,
        "reason": "format",
    }


def parse_quick_sale_message(raw_text):
    """
    åªè¦æ´åè¨æ¯çéç©ºç½è¡é½ä»¥ / éé ­ï¼
    å°±è¦çºå¿«éå¤åé åºåº«ã
    """
    lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip()
    ]

    if not lines:
        return None

    if not all(
        line.startswith("/")
        for line in lines
    ):
        return None

    parsed = []

    for line in lines:
        item = parse_quick_sale_line(line)

        if not item or not item.get("ok"):
            # å®è¡å¯è½æ¯å¶ä»æ­£å¼ / æä»¤ï¼ä¾å¦ï¼
            # /ç¼ å°ç¾ å¤§å¡ 10ã/æ¥åº«å­ å¤§å¡
            # äº¤åå¾é¢çæ­£å¼æä»¤èçã
            if len(lines) == 1:
                return None

            return {
                "ok": False,
                "reason": "format",
                "line": line,
            }

        if (
            item["quantity"] <= 0
            or item["quantity"] > 500
        ):
            return {
                "ok": False,
                "reason": "quantity",
                "line": line,
            }

        parsed.append(item)

    # åååéè¤åºç¾æï¼èªååä½µæ¸é
    merged = []
    positions = {}

    for item in parsed:
        key = item["product_text"].lower()

        if key in positions:
            merged[positions[key]]["quantity"] += item["quantity"]
        else:
            positions[key] = len(merged)
            merged.append({
                "product_text": item["product_text"],
                "quantity": item["quantity"],
            })

    total_quantity = sum(
        item["quantity"]
        for item in merged
    )

    if total_quantity > 500:
        return {
            "ok": False,
            "reason": "total_quantity",
        }

    return {
        "ok": True,
        "items": merged,
        "total_quantity": total_quantity,
    }


def format_labeled_pairs(raw_text):
    """
    /æ´ç

    ä¸çç·¨èä¸éï¼ç´æ¥ä¾ç§åºç¾é åºï¼
    ææ¯ä¸çµãåºè + å¯ç¢¼ãéæåä¸è¡ã
    """
    body = raw_text.strip()

    if body.startswith("/æ´ç"):
        body = body[len("/æ´ç"):].lstrip("\r\n ")

    # æåºæ¯ä¸ç­æ¨ç±¤è³æï¼ä¸éå¶ç·¨èä½æ¸èçµæ¸
    pattern = re.compile(
        r"(åºè|å¯ç¢¼)\s*[0-9ï¼-ï¼]*\s*[:ï¼]\s*([^\r\n]+)",
        re.IGNORECASE,
    )

    tokens = []
    for match in pattern.finditer(body):
        label = match.group(1)
        value = match.group(2).strip()

        if value:
            tokens.append((label, value))

    if not tokens:
        return {
            "ok": False,
            "reason": "no_pairs",
        }

    rows = []
    pending_serial = None

    for label, value in tokens:
        if label == "åºè":
            # å¦æåä¸ååºèéæ²ç­å°å¯ç¢¼ï¼å°±è¦çºä¸å®æ´
            if pending_serial is not None:
                return {
                    "ok": False,
                    "reason": "missing_pair",
                    "numbers": ["åä¸çµ"],
                }

            pending_serial = value
            continue

        # label == å¯ç¢¼
        if pending_serial is None:
            return {
                "ok": False,
                "reason": "missing_pair",
                "numbers": ["åä¸çµ"],
            }

        rows.append(
            f"{pending_serial}    {value}"
        )
        pending_serial = None

    if pending_serial is not None:
        return {
            "ok": False,
            "reason": "missing_pair",
            "numbers": ["æå¾ä¸çµ"],
        }

    if not rows:
        return {
            "ok": False,
            "reason": "no_pairs",
        }

    return {
        "ok": True,
        "text": "\n".join(rows),
        "count": len(rows),
    }



def add_comma_between_columns(raw_text):
    """
    /é
    å°ï¼
    AAA    BBB
    CCC<TAB>DDD

    è®æï¼
    AAA,BBB
    CCC,DDD
    """
    lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip()
    ]

    if lines and lines[0].lstrip().startswith("/é"):
        lines = lines[1:]

    rows = []

    for line in lines:
        # ä»¥ä»»æé£çºç©ºç½ï¼ç©ºæ ¼æ Tabï¼åæå©æ¬
        parts = re.split(r"\s+", line.strip(), maxsplit=1)

        if len(parts) != 2:
            return {
                "ok": False,
                "reason": "bad_line",
                "line": line,
            }

        left, right = parts[0].strip(), parts[1].strip()

        if not left or not right:
            return {
                "ok": False,
                "reason": "bad_line",
                "line": line,
            }

        rows.append(f"{left},{right}")

    if not rows:
        return {
            "ok": False,
            "reason": "no_pairs",
        }

    return {
        "ok": True,
        "text": "\n".join(rows),
        "count": len(rows),
    }



def split_text_chunks(text, max_len=4300):
    """
    LINE å®åæå­è¨æ¯ä¸éç´ 5000 å­åã
    ä¿å®åå¨ 4300ï¼ä¸¦ç¡éä¾æè¡åéã
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = []

    current_len = 0

    for line in text.splitlines():
        add_len = len(line) + 1

        if (
            current
            and current_len + add_len > max_len
        ):
            chunks.append(
                "\n".join(current)
            )
            current = []
            current_len = 0

        current.append(line)
        current_len += add_len

    if current:
        chunks.append(
            "\n".join(current)
        )

    return chunks


def sell_multiple_items(
    operator,
    context_id,
    customer,
    requested_items,
):
    """
    å¤åé ä¸æ¬¡åºåº«ï¼
    - å¨é¨åº«å­é½è¶³å¤ ææä¸èµ·æå
    - ä»»ä¸ååä¸è¶³å°±æ´ç­ rollback
    - å±ç¨åä¸åãæ¹æ¬¡è¨å®ç·¨èã
    - æ¯åååä»æèªå·±ç child order_no
    - stock_actions åªè¨ 1 ç­ï¼å æ­¤ãæ¤åãææ´æ¹æ¤å
    """
    conn = get_db()

    try:
        prepared = []

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # åæ¥ååãéå®ååé åºèã
            for requested in requested_items:

                product = find_product(
                    conn,
                    requested["product_text"],
                )

                if not product:
                    conn.rollback()

                    return {
                        "ok": False,
                        "reason": "product_not_found",
                        "product_text": requested["product_text"],
                    }

                quantity = requested["quantity"]

                cur.execute(
                    """
                    SELECT id, serial
                    FROM serials
                    WHERE product_id = %s
                      AND status = 'available'
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (
                        product["id"],
                        quantity,
                    ),
                )

                rows = cur.fetchall()

                if len(rows) < quantity:
                    # åæ¥å¯¦éå¯ç¨ç¸½æ¸ï¼è®é¯èª¤è¨æ¯æ´æ¸æ¥
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM serials
                        WHERE product_id = %s
                          AND status = 'available'
                        """,
                        (product["id"],),
                    )

                    available = cur.fetchone()["count"]

                    conn.rollback()

                    return {
                        "ok": False,
                        "reason": "not_enough",
                        "product": product,
                        "available": available,
                        "requested": quantity,
                    }

                prepared.append({
                    "product": product,
                    "quantity": quantity,
                    "rows": rows,
                })

            sale_batch_no = create_order_no()

            result_items = []

            for index, item in enumerate(
                prepared,
                start=1,
            ):
                child_order_no = (
                    f"{sale_batch_no}-{index:02d}"
                )

                serial_ids = [
                    row["id"]
                    for row in item["rows"]
                ]

                serial_values = [
                    row["serial"]
                    for row in item["rows"]
                ]

                cur.execute(
                    """
                    UPDATE serials
                    SET
                        status = 'sold',
                        sold_to = %s,
                        order_no = %s,
                        sold_at = NOW(),
                        operator = %s
                    WHERE id = ANY(%s)
                    """,
                    (
                        customer,
                        child_order_no,
                        operator,
                        serial_ids,
                    ),
                )

                cur.execute(
                    """
                    INSERT INTO transactions (
                        order_no,
                        product_id,
                        sold_to,
                        quantity,
                        operator,
                        group_id,
                        sale_batch_no
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        child_order_no,
                        item["product"]["id"],
                        customer,
                        item["quantity"],
                        operator,
                        context_id,
                        sale_batch_no,
                    ),
                )

                result_items.append({
                    "product": item["product"],
                    "quantity": item["quantity"],
                    "order_no": child_order_no,
                    "serials": serial_values,
                })

            cur.execute(
                """
                INSERT INTO stock_actions (
                    group_id,
                    action_type,
                    ref_no,
                    operator
                )
                VALUES (%s, 'sale', %s, %s)
                """,
                (
                    context_id,
                    sale_batch_no,
                    operator,
                ),
            )

        # åçµåè¦å§å®¹ï¼è¥ LINE éè¦è¶é 5 åï¼
        # å°±ä¸æäº¤ï¼é¿ååºåº«æåå»æ¿ä¸å®æ´åºèã
        serial_sections = []

        for item in result_items:
            section = [
                f"ã{item['product']['code']} Ã {item['quantity']}ã"
            ]
            section.extend(
                item["serials"]
            )

            serial_sections.append(
                "\n".join(section)
            )

        serial_text = "\n\n".join(
            serial_sections
        )

        serial_chunks = split_text_chunks(
            serial_text
        )

        # LINE Reply API å®æ¬¡æå¤ 5 åï¼
        # æå¤ 4 ååºè + 1 åæè¦ã
        if len(serial_chunks) > 4:
            conn.rollback()

            return {
                "ok": False,
                "reason": "reply_too_long",
            }

        conn.commit()

        return {
            "ok": True,
            "sale_batch_no": sale_batch_no,
            "items": result_items,
            "serial_chunks": serial_chunks,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ==================================================
# Supabaseï¼ç¼åºè
#
# FOR UPDATE SKIP LOCKEDï¼
# å¤äººåææä½æé¿åæ¿å°ç¸ååºè
# ==================================================

def sell_serials(
    operator,
    context_id,
    customer,
    product_text,
    quantity,
):
    conn = get_db()

    try:

        product = find_product(
            conn,
            product_text
        )

        if not product:
            conn.rollback()

            return {
                "ok": False,
                "reason": "product_not_found",
            }

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # éä½æ¬æ¬¡æºåç¼åºçåºè
            cur.execute(
                """
                SELECT id, serial
                FROM serials
                WHERE product_id = %s
                  AND status = 'available'
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (
                    product["id"],
                    quantity,
                ),
            )

            rows = cur.fetchall()

            if len(rows) < quantity:
                conn.rollback()

                return {
                    "ok": False,
                    "reason": "not_enough",
                    "available": len(rows),
                    "product": product,
                }

            order_no = create_order_no()

            serial_ids = [
                row["id"]
                for row in rows
            ]

            serial_values = [
                row["serial"]
                for row in rows
            ]

            # æ´æ°åºèçæ
            cur.execute(
                """
                UPDATE serials
                SET
                    status = 'sold',
                    sold_to = %s,
                    order_no = %s,
                    sold_at = NOW(),
                    operator = %s
                WHERE id = ANY(%s)
                """,
                (
                    customer,
                    order_no,
                    operator,
                    serial_ids,
                ),
            )

            # æ°å¢ä¸ç­äº¤æç´é
            cur.execute(
                """
                INSERT INTO transactions (
                    order_no,
                    product_id,
                    sold_to,
                    quantity,
                    operator,
                    group_id,
                    sale_batch_no
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    order_no,
                    product["id"],
                    customer,
                    quantity,
                    operator,
                    context_id,
                    order_no,
                ),
            )

            # è¨ééåç¾¤çµçä¸æ¬¡ãåºåº«åä½ã
            cur.execute(
                """
                INSERT INTO stock_actions (
                    group_id,
                    action_type,
                    ref_no,
                    operator
                )
                VALUES (%s, 'sale', %s, %s)
                """,
                (
                    context_id,
                    order_no,
                    operator,
                ),
            )

            # æ¥å©é¤åº«å­
            cur.execute(
                """
                SELECT COUNT(*)
                FROM serials
                WHERE product_id = %s
                  AND status = 'available'
                """,
                (product["id"],),
            )

            remaining = cur.fetchone()["count"]

        conn.commit()

        return {
            "ok": True,
            "product": product,
            "order_no": order_no,
            "serials": serial_values,
            "remaining": remaining,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ==================================================
# Supabaseï¼å¥åº«
# ä¸è¡ä¸ååºèï¼åºèå§å®¹åæ¨£ä¿å­
# ==================================================

def stock_in_serials(
    operator,
    context_id,
    product_text,
    serial_values,
):
    conn = get_db()

    try:
        product = find_product(
            conn,
            product_text
        )

        if not product:
            conn.rollback()

            return {
                "ok": False,
                "reason": "product_not_found",
            }

        batch_no = create_batch_no()

        added = []
        duplicates = []

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            for serial_value in serial_values:

                cur.execute(
                    """
                    INSERT INTO serials (
                        product_id,
                        serial,
                        status,
                        operator,
                        batch_no
                    )
                    VALUES (%s, %s, 'available', %s, %s)
                    ON CONFLICT (serial)
                    DO NOTHING
                    RETURNING serial
                    """,
                    (
                        product["id"],
                        serial_value,
                        operator,
                        batch_no,
                    ),
                )

                row = cur.fetchone()

                if row:
                    added.append(
                        row["serial"]
                    )
                else:
                    duplicates.append(
                        serial_value
                    )

            # åªæççææ°å¢åºèï¼æè¨æä¸æ¬¡å¯æ¤ååä½
            if added:
                cur.execute(
                    """
                    INSERT INTO stock_actions (
                        group_id,
                        action_type,
                        ref_no,
                        operator
                    )
                    VALUES (%s, 'stock_in', %s, %s)
                    """,
                    (
                        context_id,
                        batch_no,
                        operator,
                    ),
                )

            cur.execute(
                """
                SELECT COUNT(*)
                FROM serials
                WHERE product_id = %s
                  AND status = 'available'
                """,
                (product["id"],),
            )

            stock = cur.fetchone()["count"]

        conn.commit()

        return {
            "ok": True,
            "product": product,
            "batch_no": batch_no,
            "added": added,
            "duplicates": duplicates,
            "stock": stock,
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ==================================================
# Supabaseï¼æ¤ååºåº«
# ãæ¤åãï¼æ¤åèªå·±æå¾ä¸ç­åºåº«
# ãæ¤å TX...ãï¼æ¤åèªå·±æå®çè¨å®
# ==================================================

def undo_last_action(
    context_id,
    operator,
):
    """
    æ¤åãéåç¾¤çµ / èå¤©å®¤ãæè¿ä¸ç­å°æªæ¤åçåº«å­åä½ã
    ä¸åæä½èï¼ç¾¤çµå§ææ¬éçäººé½å¯æ¤åç¾¤çµä¸ä¸ç­ã
    """
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    id,
                    group_id,
                    action_type,
                    ref_no,
                    operator,
                    created_at
                FROM stock_actions
                WHERE group_id = %s
                  AND undone = FALSE
                ORDER BY
                    created_at DESC,
                    id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (context_id,),
            )

            action = cur.fetchone()

            if not action:
                conn.rollback()

                return {
                    "ok": False,
                    "reason": "nothing_to_undo",
                }

            # ------------------------------
            # æ¤åå¥åº«
            # ------------------------------
            if action["action_type"] == "stock_in":

                cur.execute(
                    """
                    SELECT
                        s.id,
                        s.serial,
                        s.status,
                        s.order_no,
                        s.product_id,
                        p.code,
                        p.name
                    FROM serials s
                    JOIN products p
                      ON p.id = s.product_id
                    WHERE s.batch_no = %s
                    ORDER BY s.id
                    FOR UPDATE OF s
                    """,
                    (action["ref_no"],),
                )

                rows = cur.fetchall()

                # èçæ¾æå¥åº«åä½å·²å¯«å¥ stock_actionsï¼
                # ä½ serials.batch_no æ²ææ­£ç¢ºçä¸çææ³ã
                # PostgreSQL åä¸ transaction ç now() æéç¸åï¼
                # å æ­¤å¯ç¨ operator + created_at ç²¾æºæ¾åè©²æ¹èå¥åº«ã
                if not rows:
                    cur.execute(
                        """
                        SELECT
                            s.id,
                            s.serial,
                            s.status,
                            s.order_no,
                            s.product_id,
                            p.code,
                            p.name
                        FROM serials s
                        JOIN products p
                          ON p.id = s.product_id
                        WHERE s.batch_no IS NULL
                          AND s.operator = %s
                          AND s.created_at = %s
                        ORDER BY s.id
                        FOR UPDATE OF s
                        """,
                        (
                            action["operator"],
                            action["created_at"],
                        ),
                    )

                    rows = cur.fetchall()

                if not rows:
                    conn.rollback()

                    return {
                        "ok": False,
                        "reason": "stock_in_rows_missing",
                        "batch_no": action["ref_no"],
                    }

                # å¦æéæ¹åºèå¾ä¾å·²ç¶è¢«åºåº«ï¼å°±ä¸è½æ´æ¹åªé¤ï¼
                # é¿åç ´å£å¾çºè¨å®ã
                used_rows = [
                    row
                    for row in rows
                    if (
                        row["status"] != "available"
                        or row["order_no"] is not None
                    )
                ]

                if used_rows:
                    conn.rollback()

                    return {
                        "ok": False,
                        "reason": "stock_in_already_used",
                        "used_count": len(used_rows),
                        "batch_no": action["ref_no"],
                    }

                product = {
                    "id": rows[0]["product_id"],
                    "code": rows[0]["code"],
                    "name": rows[0]["name"],
                }

                serial_values = [
                    row["serial"]
                    for row in rows
                ]

                serial_ids = [
                    row["id"]
                    for row in rows
                ]

                cur.execute(
                    """
                    DELETE FROM serials
                    WHERE id = ANY(%s)
                    """,
                    (serial_ids,),
                )

                cur.execute(
                    """
                    UPDATE stock_actions
                    SET
                        undone = TRUE,
                        undone_at = NOW(),
                        undone_by = %s
                    WHERE id = %s
                    """,
                    (
                        operator,
                        action["id"],
                    ),
                )

                conn.commit()

                return {
                    "ok": True,
                    "action_type": "stock_in",
                    "ref_no": action["ref_no"],
                    "product": product,
                    "serials": serial_values,
                    "quantity": len(serial_values),
                }

            # ------------------------------
            # æ¤ååºåº«ï¼å®å / å¤åé é½æ´æ¹æ¤åï¼
            # ------------------------------
            if action["action_type"] == "sale":

                cur.execute(
                    """
                    SELECT
                        t.id,
                        t.order_no,
                        t.sale_batch_no,
                        t.sold_to,
                        t.quantity,
                        t.product_id,
                        p.code,
                        p.name
                    FROM transactions t
                    JOIN products p
                      ON p.id = t.product_id
                    WHERE COALESCE(
                        t.sale_batch_no,
                        t.order_no
                    ) = %s
                    ORDER BY t.id
                    FOR UPDATE OF t
                    """,
                    (action["ref_no"],),
                )

                orders = cur.fetchall()

                if not orders:
                    conn.rollback()

                    return {
                        "ok": False,
                        "reason": "sale_missing",
                    }

                order_nos = [
                    order["order_no"]
                    for order in orders
                ]

                items = []

                for order in orders:

                    cur.execute(
                        """
                        SELECT id, serial
                        FROM serials
                        WHERE order_no = %s
                        ORDER BY id
                        FOR UPDATE
                        """,
                        (order["order_no"],),
                    )

                    serial_rows = cur.fetchall()

                    items.append({
                        "product": {
                            "id": order["product_id"],
                            "code": order["code"],
                            "name": order["name"],
                        },
                        "quantity": order["quantity"],
                        "sold_to": order["sold_to"],
                        "order_no": order["order_no"],
                        "serials": [
                            row["serial"]
                            for row in serial_rows
                        ],
                    })

                cur.execute(
                    """
                    UPDATE serials
                    SET
                        status = 'available',
                        sold_to = NULL,
                        order_no = NULL,
                        sold_at = NULL,
                        operator = NULL
                    WHERE order_no = ANY(%s)
                    """,
                    (order_nos,),
                )

                cur.execute(
                    """
                    DELETE FROM transactions
                    WHERE order_no = ANY(%s)
                    """,
                    (order_nos,),
                )

                cur.execute(
                    """
                    UPDATE stock_actions
                    SET
                        undone = TRUE,
                        undone_at = NOW(),
                        undone_by = %s
                    WHERE id = %s
                    """,
                    (
                        operator,
                        action["id"],
                    ),
                )

                conn.commit()

                return {
                    "ok": True,
                    "action_type": "sale",
                    "ref_no": action["ref_no"],
                    "items": items,
                    "quantity": sum(
                        item["quantity"]
                        for item in items
                    ),
                }

            conn.rollback()

            return {
                "ok": False,
                "reason": "unknown_action_type",
            }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ==================================================
# æ¥æåéï¼é·è²¨ / é²è²¨æ¥è©¢
# ==================================================

def parse_query_date(value):
    """
    æ¯æ´ï¼
    2026/10/1
    2026-10-01
    10/1
    10-01

    åªææ/æ¥æï¼ä½¿ç¨å°ç£ç®åå¹´ä»½ã
    """
    raw = str(value or "").strip()

    formats = [
        "%Y/%m/%d",
        "%Y-%m-%d",
        "%m/%d",
        "%m-%d",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)

            if fmt in ("%m/%d", "%m-%d"):
                now = datetime.now(
                    ZoneInfo("Asia/Taipei")
                )
                parsed = parsed.replace(
                    year=now.year
                )

            return parsed.date()

        except ValueError:
            pass

    raise ValueError("æ¥ææ ¼å¼é¯èª¤")


def get_sales_by_date_range(
    context_id,
    start_date,
    end_date,
):
    """
    æ¥ç®åç¾¤çµ/èå¤©å®¤æå®æ¥æåéçé·è²¨ã
    èµ·è¨æ¥é½åå«ã
    """
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    p.code,
                    SUM(t.quantity)::bigint AS quantity,
                    COUNT(
                        DISTINCT COALESCE(
                            t.sale_batch_no,
                            t.order_no
                        )
                    )::bigint AS orders
                FROM transactions t
                JOIN products p
                  ON p.id = t.product_id
                WHERE t.group_id = %s
                  AND t.created_at >= %s::date
                  AND t.created_at < (
                        %s::date + interval '1 day'
                      )
                GROUP BY p.id, p.code
                ORDER BY
                    SUM(t.quantity) DESC,
                    LOWER(p.code)
                """,
                (
                    context_id,
                    start_date,
                    end_date,
                ),
            )

            rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                    COALESCE(
                        SUM(quantity),
                        0
                    )::bigint AS total_quantity,
                    COUNT(
                        DISTINCT COALESCE(
                            sale_batch_no,
                            order_no
                        )
                    )::bigint AS total_orders
                FROM transactions
                WHERE group_id = %s
                  AND created_at >= %s::date
                  AND created_at < (
                        %s::date + interval '1 day'
                      )
                """,
                (
                    context_id,
                    start_date,
                    end_date,
                ),
            )

            total = cur.fetchone()

            return {
                "rows": rows,
                "total_quantity": total["total_quantity"],
                "total_orders": total["total_orders"],
            }

    finally:
        conn.close()


def get_stock_in_by_date_range(
    context_id,
    start_date,
    end_date,
):
    """
    æ¥ç®åç¾¤çµ/èå¤©å®¤æå®æ¥æåéçé²è²¨ã
    ä»¥ stock_actions çå¥åº«åä½ + serials.batch_no è¨ç®ï¼
    å·²æ¤åçå¥åº«ä¸è¨ã
    """
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    p.code,
                    COUNT(s.id)::bigint AS quantity,
                    COUNT(
                        DISTINCT a.ref_no
                    )::bigint AS batches
                FROM stock_actions a
                JOIN serials s
                  ON s.batch_no = a.ref_no
                JOIN products p
                  ON p.id = s.product_id
                WHERE a.group_id = %s
                  AND a.action_type = 'stock_in'
                  AND a.undone = FALSE
                  AND a.created_at >= %s::date
                  AND a.created_at < (
                        %s::date + interval '1 day'
                      )
                GROUP BY p.id, p.code
                ORDER BY
                    COUNT(s.id) DESC,
                    LOWER(p.code)
                """,
                (
                    context_id,
                    start_date,
                    end_date,
                ),
            )

            rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                    COUNT(s.id)::bigint AS total_quantity,
                    COUNT(
                        DISTINCT a.ref_no
                    )::bigint AS total_batches
                FROM stock_actions a
                JOIN serials s
                  ON s.batch_no = a.ref_no
                WHERE a.group_id = %s
                  AND a.action_type = 'stock_in'
                  AND a.undone = FALSE
                  AND a.created_at >= %s::date
                  AND a.created_at < (
                        %s::date + interval '1 day'
                      )
                """,
                (
                    context_id,
                    start_date,
                    end_date,
                ),
            )

            total = cur.fetchone()

            return {
                "rows": rows,
                "total_quantity": total["total_quantity"],
                "total_batches": total["total_batches"],
            }

    finally:
        conn.close()


def format_query_date(value):
    return value.strftime("%Y/%m/%d")



# ==================================================
# Supabaseï¼ä»æ¥é·å®
# ä»¥ç®å LINE ç¾¤çµ / èå¤©å®¤çºä¸»
# ==================================================

def get_today_sales(context_id):
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    p.code,
                    SUM(t.quantity)::bigint AS quantity,
                    COUNT(DISTINCT COALESCE(t.sale_batch_no, t.order_no))::bigint AS orders
                FROM transactions t
                JOIN products p
                  ON p.id = t.product_id
                WHERE t.group_id = %s
                  AND t.created_at >= date_trunc(
                        'day',
                        NOW() AT TIME ZONE 'Asia/Taipei'
                      ) AT TIME ZONE 'Asia/Taipei'
                  AND t.created_at < (
                        date_trunc(
                            'day',
                            NOW() AT TIME ZONE 'Asia/Taipei'
                        ) + interval '1 day'
                      ) AT TIME ZONE 'Asia/Taipei'
                GROUP BY
                    p.id,
                    p.code
                ORDER BY
                    SUM(t.quantity) DESC,
                    LOWER(p.code)
                """,
                (context_id,),
            )

            rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                    COALESCE(SUM(quantity), 0)::bigint AS total_quantity,
                    COUNT(DISTINCT COALESCE(sale_batch_no, order_no))::bigint AS total_orders
                FROM transactions
                WHERE group_id = %s
                  AND created_at >= date_trunc(
                        'day',
                        NOW() AT TIME ZONE 'Asia/Taipei'
                      ) AT TIME ZONE 'Asia/Taipei'
                  AND created_at < (
                        date_trunc(
                            'day',
                            NOW() AT TIME ZONE 'Asia/Taipei'
                        ) + interval '1 day'
                      ) AT TIME ZONE 'Asia/Taipei'
                """,
                (context_id,),
            )

            total = cur.fetchone()

            return {
                "rows": rows,
                "total_quantity": total["total_quantity"],
                "total_orders": total["total_orders"],
            }

    finally:
        conn.close()


# ==================================================
# Supabaseï¼æ¥è¨å®
# ==================================================

def get_order(order_no):
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    t.order_no,
                    t.sold_to,
                    t.quantity,
                    t.operator,
                    t.created_at,
                    p.code,
                    p.name
                FROM transactions t
                JOIN products p
                  ON p.id = t.product_id
                WHERE LOWER(t.order_no)
                      = LOWER(%s)
                LIMIT 1
                """,
                (order_no,),
            )

            order = cur.fetchone()

            if not order:
                return None

            cur.execute(
                """
                SELECT serial
                FROM serials
                WHERE order_no = %s
                ORDER BY id
                """,
                (order["order_no"],),
            )

            serial_rows = cur.fetchall()

            order["serials"] = [
                row["serial"]
                for row in serial_rows
            ]

            return order

    finally:
        conn.close()


# ==================================================
# ç¶²ç«å¥åº·æª¢æ¥
# ==================================================

@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running!", 200


# ==================================================
# LINE Webhook
# ==================================================

@app.route("/callback", methods=["POST"])
def callback():

    signature = request.headers.get(
        "X-Line-Signature",
        "",
    )

    body = request.get_data(
        as_text=True
    )

    try:
        handler.handle(
            body,
            signature,
        )

    except InvalidSignatureError:
        abort(400)

    return "OK", 200


# ==================================================
# LINE æä»¤
# ==================================================

@handler.add(
    MessageEvent,
    message=TextMessageContent,
)
def handle_message(event):

    original_raw_text = event.message.text
    original_text = original_raw_text.strip()

    # æææ©å¨äººæä»¤é½å¿é ä»¥ / éé ­ã
    # ä¸è¬èå¤©æ²æ /ï¼æ©å¨äººå®å¨ä¸åæã
    if not original_text.startswith("/"):
        return

    # èææä»¤èçéè¼¯ç¶­æä¸è®ï¼
    # åªç§»é¤æ´åè¨æ¯ãç¬¬ä¸åæä»¤éé ­ãç /ã
    # å¥åº«å¾é¢çåºèå§å®¹ä¸æè¢«ä¿®æ¹ã
    leading_ws_len = len(original_raw_text) - len(original_raw_text.lstrip())
    raw_text = (
        original_raw_text[:leading_ws_len]
        + original_raw_text[leading_ws_len + 1:]
    )
    text = raw_text.strip()

    operator = get_operator(event)
    context_id = get_context_id(event)

    try:

        # ------------------------------------------
        # æ¥èªå·±ç LINE User ID
        # éåæä»¤ä¸éè¦æ¬éï¼æ¹ä¾¿è¨­å®ç½åå®
        # ------------------------------------------

        if text == "æçID":
            reply(
                event,
                f"ä½ ç LINE User IDï¼\n{operator}"
            )
            return

        # ------------------------------------------
        # ç®¡çå¡å°ç¨ï¼å æ¬é
        # å æ¬é Uxxxxxxxx
        # ------------------------------------------

        if text.startswith("å æ¬é "):

            if not is_admin(operator):
                reply(
                    event,
                    "â åªæç®¡çå¡å¯ä»¥æ°å¢æ¬é"
                )
                return

            target_id = (
                text[len("å æ¬é "):]
                .strip()
            )

            if not target_id:
                reply(
                    event,
                    "æ ¼å¼ï¼/å æ¬é LINE_USER_ID"
                )
                return

            add_authorized_user(
                target_id
            )

            reply(
                event,
                f"â å·²æ°å¢æ¬é\n"
                f"{target_id}"
            )
            return


        # ------------------------------------------
        # ç®¡çå¡å°ç¨ï¼åªæ¬é
        # åªæ¬é Uxxxxxxxx
        # ------------------------------------------

        if text.startswith("åªæ¬é "):

            if not is_admin(operator):
                reply(
                    event,
                    "â åªæç®¡çå¡å¯ä»¥åªé¤æ¬é"
                )
                return

            target_id = (
                text[len("åªæ¬é "):]
                .strip()
            )

            if not target_id:
                reply(
                    event,
                    "æ ¼å¼ï¼/åªæ¬é LINE_USER_ID"
                )
                return

            removed = remove_authorized_user(
                target_id
            )

            if removed:
                reply(
                    event,
                    f"â å·²åªé¤æ¬é\n"
                    f"{target_id}"
                )
            else:
                reply(
                    event,
                    f"â ï¸ æ¾ä¸å°åç¨ä¸­çæ¬é\n"
                    f"{target_id}"
                )
            return


        # ------------------------------------------
        # ç®¡çå¡å°ç¨ï¼æ¬éåå®
        # ------------------------------------------

        if text == "æ¬éåå®":

            if not is_admin(operator):
                reply(
                    event,
                    "â åªæç®¡çå¡å¯ä»¥æ¥çæ¬éåå®"
                )
                return

            users = get_authorized_users()

            lines = [
                "ð ç®¡çå¡",
            ]

            if ADMIN_LINE_USER_IDS:
                for admin_id in sorted(
                    ADMIN_LINE_USER_IDS
                ):
                    lines.append(
                        admin_id
                    )
            else:
                lines.append(
                    "ï¼å°æªè¨­å®ï¼"
                )

            lines.append("")
            lines.append(
                "ð¤ ä¸è¬ä½¿ç¨è"
            )

            if users:
                for user in users:
                    lines.append(
                        user["line_user_id"]
                    )
            else:
                lines.append(
                    "ï¼ç®åæ²æï¼"
                )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # ä¸è¬æä½æ¬éæª¢æ¥
        # ç®¡çå¡æ authorized_users æè½ä½¿ç¨
        # ------------------------------------------

        if not can_use_bot(operator):
            reply(
                event,
                "â ä½ æ²ææä½æ¬é\n"
                "å¦éééï¼è«è¼¸å¥ã/æçIDã"
            )
            return

        # ------------------------------------------
        # æ¸¬è©¦
        # ------------------------------------------

        if text == "æ¸¬è©¦":
            reply(
                event,
                "æ¶å°ï¼æ©å¨äººæ­£å¸¸éä½"
            )
            return


        # ------------------------------------------
        # è¨å¸³
        # /å°ç¾ +1900
        # /å°ç¾ -1900
        # ------------------------------------------

        parts = text.split()

        if len(parts) == 2:

            customer = parts[0]
            amount_text = parts[1]

            if (
                amount_text.startswith("+")
                or amount_text.startswith("-")
            ):

                try:
                    amount = int(
                        amount_text.replace(
                            ",",
                            "",
                        )
                    )

                except ValueError:
                    reply(
                        event,
                        "â ï¸ éé¡æ ¼å¼ä¸æ­£ç¢º"
                    )
                    return

                account_type = (
                    "å å¸³"
                    if amount > 0
                    else "æ¶æ¬¾"
                )

                account_sheet.append_row([
                    now_tw(),
                    customer,
                    amount,
                    account_type,
                    "",
                    operator,
                ])

                balance = get_customer_balance(
                    customer
                )

                reply(
                    event,
                    f"â å·²è¨å¸³\n"
                    f"å®¢æ¶ï¼{customer}\n"
                    f"æ¬æ¬¡ï¼{amount:+,}\n"
                    f"ç®åé¤é¡ï¼{balance:,}"
                )
                return


        # ------------------------------------------
        # æ¥å¸³
        # ------------------------------------------

        if text.startswith("æ¥å¸³ "):

            customer = text[3:].strip()

            if not customer:
                reply(
                    event,
                    "è«è¼¸å¥å®¢æ¶åç¨±"
                )
                return

            balance = get_customer_balance(
                customer
            )

            reply(
                event,
                f"ð¤ {customer}\n"
                f"ç®åå¸³æ¬¾ï¼${balance:,}"
            )
            return


        # ------------------------------------------
        # å¨é¨åº«å­
        # ------------------------------------------

        if text == "åº«å­":

            rows = get_all_inventory()

            if not rows:
                reply(
                    event,
                    "ð¦ ç®åæ²æååè³æ"
                )
                return

            lines = [
                "ð¦ ç®ååº«å­"
            ]

            for row in rows:
                lines.append(
                    f"{row['code']}ï¼"
                    f"{row['stock']} å¼µ"
                )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # æ¥å®ä¸åååº«å­
        #
        # æ¥åº«å­ TEST100
        # æ¥åº«å­ test100
        # å©èè¦çºç¸ååå
        # ------------------------------------------

        if text.startswith("æ¥åº«å­ "):

            product_text = (
                text[len("æ¥åº«å­ "):]
                .strip()
            )

            product, count = (
                get_product_inventory(
                    product_text
                )
            )

            if not product:
                reply(
                    event,
                    f"â ï¸ æ¾ä¸å°ååï¼"
                    f"{product_text}"
                )
                return

            reply(
                event,
                f"ð¦ {product['code']}\n"
                f"{product['name']}\n"
                f"ç®ååº«å­ï¼{count} å¼µ"
            )
            return


        # ------------------------------------------
        # å¥åº«
        #
        # /å¥åº« test100
        # Abc001xY
        # TEST-002
        # 120 556 AA
        # ------------------------------------------

        if text.startswith("å¥åº« "):

            raw_lines = raw_text.splitlines()

            first_line = (
                raw_lines[0]
                .strip()
            )

            product_text = (
                first_line[len("å¥åº« "):]
                .strip()
            )

            serial_values = [
                line
                for line in raw_lines[1:]
                if line.strip() != ""
            ]

            if (
                not product_text
                or not serial_values
            ):
                reply(
                    event,
                    "æ ¼å¼ï¼\n"
                    "/å¥åº« åå\n"
                    "åºè1\n"
                    "åºè2\n\n"
                    "ä¾å¦ï¼\n"
                    "/å¥åº« test100\n"
                    "ABC001\n"
                    "120 556 AA"
                )
                return

            result = stock_in_serials(
                operator,
                context_id,
                product_text,
                serial_values,
            )

            if not result["ok"]:

                if (
                    result["reason"]
                    == "product_not_found"
                ):
                    reply(
                        event,
                        f"â ï¸ æ¾ä¸å°ååï¼"
                        f"{product_text}"
                    )
                    return

            reply(
                event,
                f"â å¥åº«å®æ\n"
                f"{result['product']['code']}\n"
                f"æ°å¢ï¼{len(result['added'])} å¼µ\n"
                f"éè¤ï¼{len(result['duplicates'])} å¼µ\n"
                f"ç®ååº«å­ï¼{result['stock']} å¼µ"
            )
            return


        # ------------------------------------------
        # /æ´çï¼æãåºè1 / å¯ç¢¼1ãæ´çæåä¸è¡
        # /éï¼æå©æ¬ç©ºç½æ¹æéè
        # ------------------------------------------

        if text == "æ´ç" or text.startswith("æ´ç\n"):

            result = format_labeled_pairs(
                original_raw_text
            )

            if not result["ok"]:

                if result["reason"] == "missing_pair":
                    nums = "ã".join(
                        str(n)
                        for n in result["numbers"]
                    )

                    if nums in ("åä¸çµ", "æå¾ä¸çµ"):
                        msg = f"â ï¸ {nums}çåºèæå¯ç¢¼ä¸å®æ´"
                    else:
                        msg = f"â ï¸ ç¬¬ {nums} çµçåºèæå¯ç¢¼ä¸å®æ´"

                    reply(
                        event,
                        msg
                    )
                    return

                reply(
                    event,
                    "â ï¸ æ²ææ¾å°å¯æ´ççåºè/å¯ç¢¼\n\n"
                    "æ ¼å¼ä¾å¦ï¼\n"
                    "/æ´ç\n"
                    "åºè1: MFXMTA003786\n"
                    "å¯ç¢¼1: LFC6M8G3DF8G"
                )
                return

            reply_messages(
                event,
                split_text_chunks(result["text"])
            )
            return

        if text == "é" or text.startswith("é\n"):

            result = add_comma_between_columns(
                original_raw_text
            )

            if not result["ok"]:

                bad_line = result.get("line", "")

                reply(
                    event,
                    "â ï¸ /é éè¦æ¯è¡æå©æ¬è³æ\n\n"
                    "ä¾å¦ï¼\n"
                    "/é\n"
                    "MFXMTA003797    GP3TX8F8QTUV"
                    + (
                        f"\n\nçä¸æéè¡ï¼{bad_line}"
                        if bad_line
                        else ""
                    )
                )
                return

            reply_messages(
                event,
                split_text_chunks(result["text"])
            )
            return


        # ------------------------------------------
        # è¶å¿«éåºåº«ï¼å¯å®åï¼ä¹å¯ä¸æ¬¡å¤åé ï¼
        #
        # /å¤§10
        # /å¤§*10
        # /å°20
        # /1490*60
        #
        # å¤è¡ä¸èµ·è²¼ = åä¸ç­åºåº«
        # ------------------------------------------

        quick_sale = parse_quick_sale_message(
            original_raw_text
        )

        if quick_sale is not None:

            if not quick_sale["ok"]:

                if (
                    quick_sale["reason"]
                    == "quantity"
                ):
                    reply(
                        event,
                        "â ï¸ å®ä¸ååä¸æ¬¡æå¤ 500 å¼µ"
                    )
                    return

                if (
                    quick_sale["reason"]
                    == "total_quantity"
                ):
                    reply(
                        event,
                        "â ï¸ ä¸æ¬¡å¿«éåºåº«ç¸½æ¸æå¤ 500 å¼µ"
                    )
                    return

                reply(
                    event,
                    "â ï¸ å¿«éåºåº«æ ¼å¼çä¸æ\n\n"
                    "ä¾å¦ï¼\n"
                    "/å¤§10\n"
                    "/å°*20\n"
                    "/1490*60"
                )
                return

            result = sell_multiple_items(
                operator,
                context_id,
                "",
                quick_sale["items"],
            )

            if not result["ok"]:

                if (
                    result["reason"]
                    == "product_not_found"
                ):
                    reply(
                        event,
                        f"â ï¸ æ¾ä¸å°ååï¼"
                        f"{result['product_text']}"
                    )
                    return

                if (
                    result["reason"]
                    == "not_enough"
                ):
                    reply(
                        event,
                        f"â ï¸ "
                        f"{result['product']['code']} "
                        f"åº«å­ä¸è¶³\n"
                        f"éè¦ï¼{result['requested']} å¼µ\n"
                        f"ç®åå¯ç¨ï¼{result['available']} å¼µ\n\n"
                        f"æ´ç­æ²æåºåº«ã"
                    )
                    return

                if (
                    result["reason"]
                    == "reply_too_long"
                ):
                    reply(
                        event,
                        "â ï¸ éæ¬¡åºèå§å®¹å¤ªé·ï¼"
                        "LINE ä¸æ¬¡ç¡æ³å®æ´åå³ã\n"
                        "æ´ç­æ²æåºåº«ï¼è«ææå©æ¬¡ã"
                    )
                    return

            sheet_sync_ok = append_sales_rows(
                "",
                result["items"],
            )

            summary_lines = [
                "â åºåº«å®æ",
                "",
            ]

            for item in result["items"]:
                summary_lines.append(
                    f"{item['product']['code']} Ã "
                    f"{item['quantity']}"
                )

            summary_lines.extend([
                "",
                f"ï¼è¨å®ç·¨è{result['sale_batch_no']}ï¼",
            ])

            if not sheet_sync_ok:
                summary_lines.extend([
                    "",
                    "â ï¸ é·å®ç´éæ«ææªå¯«å¥ Google Sheet",
                ])

            messages = list(
                result["serial_chunks"]
            )

            messages.append(
                "\n".join(summary_lines)
            )

            reply_messages(
                event,
                messages,
            )
            return


        # ------------------------------------------
        # åºåº« / ç¼åºè
        #
        # å¿«éæ ¼å¼ï¼
        # /åºå¤§å¡*10
        # /ç¼å¤§å¡*10
        #
        # ä¹ä¿çåæ¬æ ¼å¼ï¼
        # /ç¼ å°ç¾ å¤§å¡ 10
        # ç¼ å¤§å¡*10
        # ------------------------------------------

        quick_prefix = None

        if text.startswith("/åº"):
            quick_prefix = "/åº"
        elif text.startswith("/ç¼"):
            quick_prefix = "/ç¼"

        if (
            quick_prefix
            or text.startswith("ç¼ ")
        ):

            customer = ""
            product_text = ""
            quantity = None

            # ------------------------------
            # æ°å¿«éæ ¼å¼ï¼
            # /åºå¤§å¡*10
            # /ç¼å¤§å¡*10
            # ------------------------------
            if quick_prefix:

                body = text[len(quick_prefix):].strip()

                if "*" not in body:
                    reply(
                        event,
                        "æ ¼å¼ï¼\n"
                        "/åºç¢å*æ¸é\n"
                        "/ç¼ç¢å*æ¸é\n\n"
                        "ä¾å¦ï¼\n"
                        "/åºå¤§å¡*10\n"
                        "/ç¼å¤§å¡*10"
                    )
                    return

                left, right = body.rsplit("*", 1)

                product_text = left.strip()

                try:
                    quantity = int(
                        right.strip()
                    )
                except ValueError:
                    reply(
                        event,
                        "â ï¸ æ¸éå¿é æ¯æ¸å­\n"
                        "ä¾å¦ï¼/åºå¤§å¡*10"
                    )
                    return

                if not product_text:
                    reply(
                        event,
                        "â ï¸ è«è¼¸å¥åå\n"
                        "ä¾å¦ï¼/åºå¤§å¡*10"
                    )
                    return

            # ------------------------------
            # åæ¬æ ¼å¼ï¼
            # ç¼ å¤§å¡*10
            # /ç¼ å°ç¾ å¤§å¡ 10
            # ------------------------------
            else:

                body = text[len("ç¼ "):].strip()

                # ç¼ å¤§å¡*10
                if "*" in body:

                    left, right = body.rsplit("*", 1)

                    product_text = left.strip()

                    try:
                        quantity = int(
                            right.strip()
                        )
                    except ValueError:
                        reply(
                            event,
                            "â ï¸ æ¸éå¿é æ¯æ¸å­\n"
                            "ä¾å¦ï¼/ç¼ å¤§å¡*10"
                        )
                        return

                    if not product_text:
                        reply(
                            event,
                            "â ï¸ è«è¼¸å¥åå\n"
                            "ä¾å¦ï¼/ç¼ å¤§å¡*10"
                        )
                        return

                # /ç¼ å°ç¾ å¤§å¡ 10
                else:

                    parts = body.split()

                    if len(parts) != 3:
                        reply(
                            event,
                            "æ ¼å¼ï¼\n"
                            "/ç¼ å®¢æ¶ åå æ¸é\n"
                            "ä¾å¦ï¼/ç¼ å°ç¾ å¤§å¡ 10\n\n"
                            "å¿«éæ ¼å¼ï¼\n"
                            "/åºå¤§å¡*10\n"
                            "/ç¼å¤§å¡*10"
                        )
                        return

                    customer = parts[0]
                    product_text = parts[1]

                    try:
                        quantity = int(
                            parts[2]
                        )
                    except ValueError:
                        reply(
                            event,
                            "â ï¸ æ¸éå¿é æ¯æ¸å­"
                        )
                        return

            if (
                quantity <= 0
                or quantity > 20
            ):
                reply(
                    event,
                    "â ï¸ ä¸æ¬¡ç¼éæ¸ééçº 1ï½20"
                )
                return

            result = sell_serials(
                operator,
                context_id,
                customer,
                product_text,
                quantity,
            )

            if not result["ok"]:

                if (
                    result["reason"]
                    == "product_not_found"
                ):
                    reply(
                        event,
                        f"â ï¸ æ¾ä¸å°ååï¼"
                        f"{product_text}"
                    )
                    return

                if (
                    result["reason"]
                    == "not_enough"
                ):
                    reply(
                        event,
                        f"â ï¸ "
                        f"{result['product']['code']} "
                        f"åº«å­ä¸è¶³\n"
                        f"ç®åå¯ç¨ï¼"
                        f"{result['available']} å¼µ"
                    )
                    return

            sheet_sync_ok = append_sales_rows(
                customer,
                [
                    {
                        "product": result["product"],
                        "quantity": quantity,
                        "order_no": result["order_no"],
                    }
                ],
            )

            serial_text = "\n".join(
                result["serials"]
            )

            if customer:
                summary_line = (
                    f"{result['product']['code']} Ã {quantity}  "
                    f"â¡ï¸ã{customer}ã"
                )
            else:
                summary_line = (
                    f"{result['product']['code']} Ã {quantity}"
                )

            reply_messages(
                event,
                [
                    serial_text,
                    (
                        f"â å·²åºåº«\n"
                        f"{summary_line}\n"
                        f"ï¼è¨å®ç·¨è{result['order_no']}ï¼"
                        + (
                            "\nâ ï¸ é·å®ç´éæ«ææªå¯«å¥ Google Sheet"
                            if not sheet_sync_ok
                            else ""
                        )
                    ),
                ],
            )
            return


        # ------------------------------------------
        # æ¤å
        #
        # æ¤åéåç¾¤çµ / èå¤©å®¤çä¸ä¸ç­åº«å­åä½
        # ä¸ç®¡ä¸ä¸ç­æ¯å¥åº«æåºåº«
        # ------------------------------------------

        if text == "æ¤å":

            result = undo_last_action(
                context_id,
                operator,
            )

            if not result["ok"]:

                if (
                    result["reason"]
                    == "nothing_to_undo"
                ):
                    reply(
                        event,
                        "â ï¸ éåç¾¤çµç®åæ²æå¯æ¤åçä¸ä¸ç­åä½"
                    )
                    return

                if (
                    result["reason"]
                    == "stock_in_rows_missing"
                ):
                    reply(
                        event,
                        "â ï¸ æ¾ä¸å°éæ¬¡å¥åº«çåºèè³æï¼"
                        "æä»¥ç³»çµ±æ²æåªé¤ä»»ä½åº«å­ã\n"
                        "è«æéåç«é¢æªåçµ¦ç®¡çå¡ã"
                    )
                    return

                if (
                    result["reason"]
                    == "stock_in_already_used"
                ):
                    reply(
                        event,
                        "â ï¸ ç¡æ³æ¤åéæ¬¡å¥åº«\n"
                        "éä¸æ¹è£¡å·²æåºèè¢«åºåº«ï¼"
                        "çºé¿åç ´å£è¨å®ï¼ç³»çµ±æ²æåªé¤ã"
                    )
                    return

                reply(
                    event,
                    "â ï¸ éç­åä½ç®åç¡æ³æ¤å"
                )
                return

            if (
                result["action_type"]
                == "stock_in"
            ):
                reply(
                    event,
                    f"â©ï¸ å·²æ¤åä¸ä¸ç­å¥åº«\n"
                    f"{result['product']['code']} Ã "
                    f"{result['quantity']} å¼µ\n"
                    f"ï¼æ¹æ¬¡ç·¨è{result['ref_no']}ï¼"
                )
                return

            if (
                result["action_type"]
                == "sale"
            ):
                sheet_undo_ok = mark_sales_rows_undone(
                    [
                        item["order_no"]
                        for item in result["items"]
                    ]
                )

                lines = [
                    "â©ï¸ å·²æ¤åä¸ä¸ç­åºåº«",
                ]

                for item in result["items"]:
                    lines.append(
                        f"{item['product']['code']} Ã "
                        f"{item['quantity']}"
                    )

                sold_to_values = [
                    item["sold_to"]
                    for item in result["items"]
                    if item["sold_to"]
                ]

                if sold_to_values:
                    lines.append(
                        f"åå®¢æ¶ï¼ã{sold_to_values[0]}ã"
                    )

                lines.append(
                    f"ï¼è¨å®ç·¨è{result['ref_no']}ï¼"
                )

                if not sheet_undo_ok:
                    lines.append(
                        "â ï¸ Google Sheet å°æªæ¨è¨æ¤å"
                    )

                reply(
                    event,
                    "\n".join(lines)
                )
                return


        # ------------------------------------------
        # è£ä¸ä¸å® å®¢æ¶
        # ------------------------------------------

        if text.startswith("è£ä¸ä¸å® "):

            customer = (
                text[len("è£ä¸ä¸å® "):]
                .strip()
            )

            if not customer:
                reply(
                    event,
                    "æ ¼å¼ï¼/è£ä¸ä¸å® å®¢æ¶å"
                )
                return

            result = fill_order_customer(
                context_id,
                customer,
            )

            if not result["ok"]:

                if result["reason"] == "no_blank_order":
                    reply(
                        event,
                        "â ï¸ ç®åéåç¾¤çµæ²æå¯è£å®¢æ¶çè¨å®"
                    )
                    return

                reply(
                    event,
                    "â ï¸ æ¾ä¸å°å¯è£çè¨å®"
                )
                return

            lines = [
                "â è£å®å®æ",
                f"å®¢æ¶ï¼{customer}",
                "",
            ]

            for order in result["orders"]:
                lines.append(
                    f"{order['code']} Ã {order['quantity']}"
                )

            lines.extend([
                "",
                f"ï¼è¨å®ç·¨è{result['batch_no']}ï¼",
            ])

            if not result["sheet_ok"]:
                lines.append(
                    "â ï¸ Google Sheet å®¢æ¶æ¬æ«ææªæ´æ°"
                )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # è£å® è¨å®ç·¨è å®¢æ¶
        # ------------------------------------------

        if text.startswith("è£å® "):

            parts = text.split(maxsplit=2)

            if len(parts) != 3:
                reply(
                    event,
                    "æ ¼å¼ï¼/è£å® è¨å®ç·¨è å®¢æ¶å"
                )
                return

            order_no = parts[1].strip()
            customer = parts[2].strip()

            result = fill_order_customer(
                context_id,
                customer,
                order_or_batch_no=order_no,
            )

            if not result["ok"]:

                if result["reason"] == "order_not_found":
                    reply(
                        event,
                        f"â ï¸ æ¾ä¸å°è¨å®ï¼{order_no}"
                    )
                    return

                if result["reason"] == "wrong_context":
                    reply(
                        event,
                        "â ï¸ éç­è¨å®ä¸å±¬æ¼ç®åéåç¾¤çµ"
                    )
                    return

                if result["reason"] == "already_has_customer":
                    current = "ã".join(
                        result.get("customers", [])
                    )
                    reply(
                        event,
                        "â ï¸ éç­è¨å®å·²ç¶æå®¢æ¶"
                        + (
                            f"ï¼{current}"
                            if current
                            else ""
                        )
                        + "\nè£å®ä¸æç´æ¥è¦èã"
                    )
                    return

                reply(
                    event,
                    "â ï¸ ç¡æ³è£éç­è¨å®"
                )
                return

            lines = [
                "â è£å®å®æ",
                f"å®¢æ¶ï¼{customer}",
                "",
            ]

            for order in result["orders"]:
                lines.append(
                    f"{order['code']} Ã {order['quantity']}"
                )

            lines.extend([
                "",
                f"ï¼è¨å®ç·¨è{result['batch_no']}ï¼",
            ])

            if not result["sheet_ok"]:
                lines.append(
                    "â ï¸ Google Sheet å®¢æ¶æ¬æ«ææªæ´æ°"
                )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # åæ­¥è£å®
        # Google Sheetãé·å®ç´éã -> Supabase
        # ------------------------------------------

        if text == "åæ­¥è£å®":

            result = sync_sales_sheet_to_supabase()

            if not result["ok"]:

                if (
                    result["reason"]
                    == "missing_headers"
                ):
                    reply(
                        event,
                        "â ï¸ é·å®ç´éç¼ºå°æ¬ä½ï¼\n"
                        + "ã".join(
                            result["missing_headers"]
                        )
                    )
                    return

                reply(
                    event,
                    "â ï¸ åæ­¥è£å®å¤±æ"
                )
                return

            lines = [
                "â åæ­¥è£å®å®æ",
                f"æè®æ´ï¼{result['updated']} ç­",
                f"ç¡è®æ´ï¼{result['unchanged']} ç­",
            ]

            if result["skipped_undone"]:
                lines.append(
                    f"å·²æ¤åè·³éï¼"
                    f"{result['skipped_undone']} ç­"
                )

            if result["not_found"]:
                lines.append(
                    f"æ¾ä¸å°è¨å®ï¼"
                    f"{result['not_found']} ç­"
                )

            if result["invalid"]:
                lines.append("")
                lines.append("â ï¸ éè¦æª¢æ¥ï¼")
                lines.extend(
                    result["invalid"][:10]
                )

                if len(result["invalid"]) > 10:
                    lines.append(
                        f"...å¦å¤éæ "
                        f"{len(result['invalid']) - 10} ç­"
                    )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # æ¥æ¥æåéé·è²¨
        # ------------------------------------------

        if text.startswith("æ¥é·è²¨ "):

            parts = text.split()

            if len(parts) != 3:
                reply(
                    event,
                    "æ ¼å¼ï¼/æ¥é·è²¨ éå§æ¥æ çµææ¥æ\n"
                    "ä¾å¦ï¼/æ¥é·è²¨ 2026/10/1 2026/10/7"
                )
                return

            try:
                start_date = parse_query_date(parts[1])
                end_date = parse_query_date(parts[2])
            except ValueError:
                reply(
                    event,
                    "â ï¸ æ¥ææ ¼å¼é¯èª¤\n"
                    "ä¾å¦ï¼/æ¥é·è²¨ 2026/10/1 2026/10/7"
                )
                return

            if start_date > end_date:
                reply(
                    event,
                    "â ï¸ éå§æ¥æä¸è½ææ¼çµææ¥æ"
                )
                return

            result = get_sales_by_date_range(
                context_id,
                start_date,
                end_date,
            )

            lines = [
                "ð¤ é·è²¨æ¥è©¢",
                (
                    f"{format_query_date(start_date)}"
                    f" ï½ "
                    f"{format_query_date(end_date)}"
                ),
                "",
            ]

            if not result["rows"]:
                lines.append(
                    "éååéæ²æé·è²¨ç´é"
                )
            else:
                for row in result["rows"]:
                    lines.append(
                        f"{row['code']}ï¼"
                        f"{row['quantity']} å¼µ"
                    )

                lines.extend([
                    "",
                    f"ç¸½é·è²¨ï¼{result['total_quantity']} å¼µ",
                    f"è¨å®ï¼{result['total_orders']} ç­",
                ])

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # æ¥æ¥æåéé²è²¨
        # ------------------------------------------

        if text.startswith("æ¥é²è²¨ "):

            parts = text.split()

            if len(parts) != 3:
                reply(
                    event,
                    "æ ¼å¼ï¼/æ¥é²è²¨ éå§æ¥æ çµææ¥æ\n"
                    "ä¾å¦ï¼/æ¥é²è²¨ 2026/10/1 2026/10/7"
                )
                return

            try:
                start_date = parse_query_date(parts[1])
                end_date = parse_query_date(parts[2])
            except ValueError:
                reply(
                    event,
                    "â ï¸ æ¥ææ ¼å¼é¯èª¤\n"
                    "ä¾å¦ï¼/æ¥é²è²¨ 2026/10/1 2026/10/7"
                )
                return

            if start_date > end_date:
                reply(
                    event,
                    "â ï¸ éå§æ¥æä¸è½ææ¼çµææ¥æ"
                )
                return

            result = get_stock_in_by_date_range(
                context_id,
                start_date,
                end_date,
            )

            lines = [
                "ð¥ é²è²¨æ¥è©¢",
                (
                    f"{format_query_date(start_date)}"
                    f" ï½ "
                    f"{format_query_date(end_date)}"
                ),
                "",
            ]

            if not result["rows"]:
                lines.append(
                    "éååéæ²æé²è²¨ç´é"
                )
            else:
                for row in result["rows"]:
                    lines.append(
                        f"{row['code']}ï¼"
                        f"{row['quantity']} å¼µ"
                    )

                lines.extend([
                    "",
                    f"ç¸½é²è²¨ï¼{result['total_quantity']} å¼µ",
                    f"å¥åº«æ¹æ¬¡ï¼{result['total_batches']} ç­",
                ])

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # ä»æ¥é·å®
        # ä»¥ç®å LINE ç¾¤çµ / èå¤©å®¤çºä¸»
        # ------------------------------------------

        if text in (
            "ä»æ¥é·å®",
            "ä»å¤©é·å®",
            "/ä»æ¥é·å®",
        ):

            sales = get_today_sales(
                context_id
            )

            if not sales["rows"]:
                reply(
                    event,
                    "ð ä»æ¥é·å®\n"
                    "ç®åå°ç¡åºåº«ç´é"
                )
                return

            lines = [
                "ð ä»æ¥é·å®",
                f"ç¸½åºåº«ï¼{sales['total_quantity']} å¼µ",
                f"è¨å®æ¸ï¼{sales['total_orders']} ç­",
                "",
            ]

            for row in sales["rows"]:
                lines.append(
                    f"{row['code']}ï¼"
                    f"{row['quantity']} å¼µ"
                    f"ï¼{row['orders']} ç­ï¼"
                )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # æ¥è¨å®
        # ------------------------------------------

        if text.startswith("æ¥å® "):

            order_no = text[3:].strip()

            order = get_order(
                order_no
            )

            if not order:
                reply(
                    event,
                    f"â ï¸ æ¾ä¸å°è¨å®ï¼"
                    f"{order_no}"
                )
                return

            serial_text = "\n".join(
                order["serials"]
            )

            created_at = format_db_time(
                order["created_at"]
            )

            reply(
                event,
                f"ð§¾ è¨å®è³æ\n"
                f"è¨å®ï¼{order['order_no']}\n"
                f"å®¢æ¶ï¼{order['sold_to']}\n"
                f"ååï¼{order['code']}\n"
                f"æ¸éï¼{order['quantity']}\n"
                f"æéï¼{created_at}\n\n"
                f"{serial_text}"
            )
            return


        # ------------------------------------------
        # æä»¤èªªæ
        # ------------------------------------------

        if text == "æä»¤":

            command_text = (
                "ð å¯ç¨æä»¤\n\n"
                "æ¥èªå·±çIDï¼/æçID\n"
                "æ¸¬è©¦ï¼/æ¸¬è©¦\n\n"
                "è¨å¸³ï¼/å°ç¾ +1900\n"
                "æ¶æ¬¾ï¼/å°ç¾ -1900\n"
                "æ¥å¸³ï¼/æ¥å¸³ å°ç¾\n\n"
                "å¨é¨åº«å­ï¼/åº«å­\n"
                "å®ååº«å­ï¼/æ¥åº«å­ MyCard1000\n\n"
                "å¥åº«ï¼\n"
                "/å¥åº« MyCard1000\n"
                "åºè1\n"
                "åºè2\n\n"
                "å®æ´åºåº«ï¼/ç¼ å°ç¾ MyCard1000 5\n"
                "å¿«éåºåº«ï¼/å¤§10 æ /å¤§*10\n"
                "å¤åé ï¼æ¯è¡ä¸åï¼ä¾å¦ /å¤§10ã/å°20\n\n"
                "æ¤åç¾¤çµä¸ä¸ç­ï¼/æ¤å\n"
                "ä»æ¥é·å®ï¼/ä»æ¥é·å®\n"
                "æ¥è¨å®ï¼/æ¥å® TXxxxxxxxx\n"
                "è£ä¸ä¸å®ï¼/è£ä¸ä¸å® å®¢æ¶å\n"
                "æå®è£å®ï¼/è£å® TXxxxxxxxx å®¢æ¶å\n"
                "åæ­¥è£å®ï¼/åæ­¥è£å®\n"
                "æ¥æé·è²¨ï¼/æ¥é·è²¨ 10/1 10/7\n"
                "æ¥æé²è²¨ï¼/æ¥é²è²¨ 10/1 10/7\n"
                "æ´çåºèå¯ç¢¼ï¼/æ´ç\n"
                "ç©ºç½æ¹éèï¼/é"
            )

            if is_admin(operator):
                command_text += (
                    "\n\nð ç®¡çå¡æä»¤\n"
                    "/å æ¬é Uxxxxxxxx\n"
                    "/åªæ¬é Uxxxxxxxx\n"
                    "/æ¬éåå®"
                )

            reply(
                event,
                command_text
            )
            return


        reply(
            event,
            "çä¸æéåæä»¤ ð\n"
            "è¼¸å¥ã/æä»¤ãæ¥çä½¿ç¨æ¹å¼"
        )

    except Exception as e:

        print(
            "BOT ERROR:",
            repr(e),
        )

        reply(
            event,
            "â ï¸ ç³»çµ±èçå¤±æï¼"
            "è«ç¨å¾åè©¦ã"
        )


# ==================================================
# åå
# ==================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            10000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
