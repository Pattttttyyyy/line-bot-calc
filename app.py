import os
import uuid
from datetime import datetime
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
# 目前只保留「帳務表」
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
    "帳務表"
)


# ==================================================
# Supabase PostgreSQL
# ==================================================

DATABASE_URL = os.getenv("DATABASE_URL")

# 快速出庫別名
# /大10、/大*10、/1490*60 ... 會轉成正式商品 code
QUICK_PRODUCT_ALIASES = {
    "大": "大卡",
    "小": "小卡",
    "500": "貝500",
    "1000": "貝1000",
    "1490": "貝1490",
    "2990": "貝2990",
    "M3": "MY3000",
    "M5": "MY5000",
    "M1W": "MY1萬",
}


# LINE 管理員名單
# Render 環境變數仍沿用：ALLOWED_LINE_USER_IDS
# 這裡放的是「管理員」LINE User ID
# 多個管理員用逗號分隔
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
# 共用工具
# ==================================================

def now_tw():
    return datetime.now(
        ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M:%S")


def get_operator(event):
    try:
        return event.source.user_id or "未知"
    except Exception:
        return "未知"


def get_context_id(event):
    """
    以 LINE 群組為優先。
    群組：group:<group_id>
    多人聊天室：room:<room_id>
    一對一聊天：user:<user_id>
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
    """Render ALLOWED_LINE_USER_IDS 內的人 = 管理員。"""
    return operator in ADMIN_LINE_USER_IDS


def is_authorized_user(operator):
    """一般使用者權限存放在 Supabase authorized_users。"""
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
    """管理員或 active 的一般使用者都可以使用機器人。"""
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
    # LINE 單則文字訊息避免過長
    if len(text) > 4900:
        text = text[:4850] + "\n\n⚠️ 內容過長，已截短。"

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
    """一次回覆多個 LINE 文字泡泡。"""
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
# 商品查詢
# 商品代碼忽略英文大小寫
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
# Google Sheet 帳務
# ==================================================

def get_customer_balance(customer):
    records = account_sheet.get_all_records()

    balance = 0

    for row in records:
        if (
            str(row.get("客戶", "")).strip()
            == customer
        ):
            try:
                balance += int(
                    str(row.get("金額", 0))
                    .replace(",", "")
                )
            except Exception:
                pass

    return balance


# ==================================================
# Supabase：所有庫存
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
# Supabase：單一商品庫存
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

    # 英文字母別名忽略大小寫
    for alias, product_code in QUICK_PRODUCT_ALIASES.items():
        if alias.upper() == key.upper():
            return product_code

    # 不在別名表時，直接把輸入當正式商品 code
    return key


def parse_quick_sale_line(line):
    """
    支援：
    /大10
    /大*10
    /1490*60
    /出大10
    /發大*10
    /MY3000*5

    無 * 時，為避免正式商品 code 與數量黏在一起產生歧義，
    只接受已設定的快速別名。
    """
    value = line.strip()

    if not value.startswith("/"):
        return None

    body = value[1:].strip()

    if body.startswith("出") or body.startswith("發"):
        body = body[1:].strip()

    if not body:
        return {
            "ok": False,
            "reason": "empty",
        }

    # 有 *：左邊可用別名，也可直接正式商品 code
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

    # 無 *：只對快速別名做「別名 + 數量」辨識
    # 長別名優先，避免 1 / 10 / 1000 類型誤判
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
    只要整則訊息的非空白行都以 / 開頭，
    就視為快速多品項出庫。
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

    # 同商品重複出現時，自動合併數量
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


def split_text_chunks(text, max_len=4300):
    """
    LINE 單則文字訊息上限約 5000 字元。
    保守切在 4300，並盡量依換行切開。
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
    多品項一次出庫：
    - 全部庫存都足夠才會一起成功
    - 任一商品不足就整筆 rollback
    - 共用同一個「批次訂單編號」
    - 每個商品仍有自己的 child order_no
    - stock_actions 只記 1 筆，因此「撤回」會整批撤回
    """
    conn = get_db()

    try:
        prepared = []

        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            # 先查商品、鎖定各品項序號。
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
                    # 再查實際可用總數，讓錯誤訊息更清楚
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

        # 先組回覆內容，若 LINE 需要超過 5 則，
        # 就不提交，避免出庫成功卻拿不完整序號。
        serial_sections = []

        for item in result_items:
            section = [
                f"【{item['product']['code']} × {item['quantity']}】"
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

        # LINE Reply API 單次最多 5 則：
        # 最多 4 則序號 + 1 則摘要。
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
# Supabase：發序號
#
# FOR UPDATE SKIP LOCKED：
# 多人同時操作時避免拿到相同序號
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

            # 鎖住本次準備發出的序號
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

            # 更新序號狀態
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

            # 新增一筆交易紀錄
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

            # 記錄這個群組的一次「出庫動作」
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

            # 查剩餘庫存
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
# Supabase：入庫
# 一行一個序號；序號內容原樣保存
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

            # 只有真的有新增序號，才記成一次可撤回動作
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
# Supabase：撤回出庫
# 「撤回」＝撤回自己最後一筆出庫
# 「撤回 TX...」＝撤回自己指定的訂單
# ==================================================

def undo_last_action(
    context_id,
    operator,
):
    """
    撤回「這個群組 / 聊天室」最近一筆尚未撤回的庫存動作。
    不分操作者，群組內有權限的人都可撤回群組上一筆。
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
            # 撤回入庫
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

                if not rows:
                    conn.rollback()

                    return {
                        "ok": False,
                        "reason": "stock_in_rows_missing",
                    }

                # 如果這批序號後來已經被出庫，就不能整批刪除，
                # 避免破壞後續訂單。
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

                cur.execute(
                    """
                    DELETE FROM serials
                    WHERE batch_no = %s
                    """,
                    (action["ref_no"],),
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
            # 撤回出庫（單品 / 多品項都整批撤回）
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
# Supabase：今日銷售
# 以目前 LINE 群組 / 聊天室為主
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
# Supabase：查訂單
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
# 網站健康檢查
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
# LINE 指令
# ==================================================

@handler.add(
    MessageEvent,
    message=TextMessageContent,
)
def handle_message(event):

    raw_text = event.message.text
    text = raw_text.strip()

    operator = get_operator(event)
    context_id = get_context_id(event)

    try:

        # ------------------------------------------
        # 查自己的 LINE User ID
        # 這個指令不需要權限，方便設定白名單
        # ------------------------------------------

        if text == "我的ID":
            reply(
                event,
                f"你的 LINE User ID：\n{operator}"
            )
            return

        # ------------------------------------------
        # 管理員專用：加權限
        # 加權限 Uxxxxxxxx
        # ------------------------------------------

        if text.startswith("加權限 "):

            if not is_admin(operator):
                reply(
                    event,
                    "⛔ 只有管理員可以新增權限"
                )
                return

            target_id = (
                text[len("加權限 "):]
                .strip()
            )

            if not target_id:
                reply(
                    event,
                    "格式：加權限 LINE_USER_ID"
                )
                return

            add_authorized_user(
                target_id
            )

            reply(
                event,
                f"✅ 已新增權限\n"
                f"{target_id}"
            )
            return


        # ------------------------------------------
        # 管理員專用：刪權限
        # 刪權限 Uxxxxxxxx
        # ------------------------------------------

        if text.startswith("刪權限 "):

            if not is_admin(operator):
                reply(
                    event,
                    "⛔ 只有管理員可以刪除權限"
                )
                return

            target_id = (
                text[len("刪權限 "):]
                .strip()
            )

            if not target_id:
                reply(
                    event,
                    "格式：刪權限 LINE_USER_ID"
                )
                return

            removed = remove_authorized_user(
                target_id
            )

            if removed:
                reply(
                    event,
                    f"✅ 已刪除權限\n"
                    f"{target_id}"
                )
            else:
                reply(
                    event,
                    f"⚠️ 找不到啟用中的權限\n"
                    f"{target_id}"
                )
            return


        # ------------------------------------------
        # 管理員專用：權限名單
        # ------------------------------------------

        if text == "權限名單":

            if not is_admin(operator):
                reply(
                    event,
                    "⛔ 只有管理員可以查看權限名單"
                )
                return

            users = get_authorized_users()

            lines = [
                "👑 管理員",
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
                    "（尚未設定）"
                )

            lines.append("")
            lines.append(
                "👤 一般使用者"
            )

            if users:
                for user in users:
                    lines.append(
                        user["line_user_id"]
                    )
            else:
                lines.append(
                    "（目前沒有）"
                )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # 一般操作權限檢查
        # 管理員或 authorized_users 才能使用
        # ------------------------------------------

        if not can_use_bot(operator):
            reply(
                event,
                "⛔ 你沒有操作權限\n"
                "如需開通，請輸入「我的ID」"
            )
            return

        # ------------------------------------------
        # 測試
        # ------------------------------------------

        if text == "測試":
            reply(
                event,
                "收到！機器人正常運作"
            )
            return


        # ------------------------------------------
        # 記帳
        # 小美 +1900
        # 小美 -1900
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
                        "⚠️ 金額格式不正確"
                    )
                    return

                account_type = (
                    "加帳"
                    if amount > 0
                    else "收款"
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
                    f"✅ 已記帳\n"
                    f"客戶：{customer}\n"
                    f"本次：{amount:+,}\n"
                    f"目前餘額：{balance:,}"
                )
                return


        # ------------------------------------------
        # 查帳
        # ------------------------------------------

        if text.startswith("查帳 "):

            customer = text[3:].strip()

            if not customer:
                reply(
                    event,
                    "請輸入客戶名稱"
                )
                return

            balance = get_customer_balance(
                customer
            )

            reply(
                event,
                f"👤 {customer}\n"
                f"目前帳款：${balance:,}"
            )
            return


        # ------------------------------------------
        # 全部庫存
        # ------------------------------------------

        if text == "庫存":

            rows = get_all_inventory()

            if not rows:
                reply(
                    event,
                    "📦 目前沒有商品資料"
                )
                return

            lines = [
                "📦 目前庫存"
            ]

            for row in rows:
                lines.append(
                    f"{row['code']}："
                    f"{row['stock']} 張"
                )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # 查單一商品庫存
        #
        # 查庫存 TEST100
        # 查庫存 test100
        # 兩者視為相同商品
        # ------------------------------------------

        if text.startswith("查庫存 "):

            product_text = (
                text[len("查庫存 "):]
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
                    f"⚠️ 找不到商品："
                    f"{product_text}"
                )
                return

            reply(
                event,
                f"📦 {product['code']}\n"
                f"{product['name']}\n"
                f"目前庫存：{count} 張"
            )
            return


        # ------------------------------------------
        # 入庫
        #
        # 入庫 test100
        # Abc001xY
        # TEST-002
        # 120 556 AA
        # ------------------------------------------

        if text.startswith("入庫 "):

            raw_lines = raw_text.splitlines()

            first_line = (
                raw_lines[0]
                .strip()
            )

            product_text = (
                first_line[len("入庫 "):]
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
                    "格式：\n"
                    "入庫 商品\n"
                    "序號1\n"
                    "序號2\n\n"
                    "例如：\n"
                    "入庫 test100\n"
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
                        f"⚠️ 找不到商品："
                        f"{product_text}"
                    )
                    return

            reply(
                event,
                f"✅ 入庫完成\n"
                f"{result['product']['code']}\n"
                f"新增：{len(result['added'])} 張\n"
                f"重複：{len(result['duplicates'])} 張\n"
                f"目前庫存：{result['stock']} 張"
            )
            return


        # ------------------------------------------
        # 超快速出庫（可單品，也可一次多品項）
        #
        # /大10
        # /大*10
        # /小20
        # /1490*60
        #
        # 多行一起貼 = 同一筆出庫
        # ------------------------------------------

        quick_sale = parse_quick_sale_message(
            raw_text
        )

        if quick_sale is not None:

            if not quick_sale["ok"]:

                if (
                    quick_sale["reason"]
                    == "quantity"
                ):
                    reply(
                        event,
                        "⚠️ 單一商品一次最多 500 張"
                    )
                    return

                if (
                    quick_sale["reason"]
                    == "total_quantity"
                ):
                    reply(
                        event,
                        "⚠️ 一次快速出庫總數最多 500 張"
                    )
                    return

                reply(
                    event,
                    "⚠️ 快速出庫格式看不懂\n\n"
                    "例如：\n"
                    "/大10\n"
                    "/小*20\n"
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
                        f"⚠️ 找不到商品："
                        f"{result['product_text']}"
                    )
                    return

                if (
                    result["reason"]
                    == "not_enough"
                ):
                    reply(
                        event,
                        f"⚠️ "
                        f"{result['product']['code']} "
                        f"庫存不足\n"
                        f"需要：{result['requested']} 張\n"
                        f"目前可用：{result['available']} 張\n\n"
                        f"整筆沒有出庫。"
                    )
                    return

                if (
                    result["reason"]
                    == "reply_too_long"
                ):
                    reply(
                        event,
                        "⚠️ 這次序號內容太長，"
                        "LINE 一次無法完整回傳。\n"
                        "整筆沒有出庫，請拆成兩次。"
                    )
                    return

            summary_lines = [
                "✅ 出庫完成",
                "",
            ]

            for item in result["items"]:
                summary_lines.append(
                    f"{item['product']['code']} × "
                    f"{item['quantity']}"
                )

            summary_lines.extend([
                "",
                f"（訂單編號{result['sale_batch_no']}）",
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
        # 出庫 / 發序號
        #
        # 快速格式：
        # /出大卡*10
        # /發大卡*10
        #
        # 也保留原本格式：
        # 發 小美 大卡 10
        # 發 大卡*10
        # ------------------------------------------

        quick_prefix = None

        if text.startswith("/出"):
            quick_prefix = "/出"
        elif text.startswith("/發"):
            quick_prefix = "/發"

        if (
            quick_prefix
            or text.startswith("發 ")
        ):

            customer = ""
            product_text = ""
            quantity = None

            # ------------------------------
            # 新快速格式：
            # /出大卡*10
            # /發大卡*10
            # ------------------------------
            if quick_prefix:

                body = text[len(quick_prefix):].strip()

                if "*" not in body:
                    reply(
                        event,
                        "格式：\n"
                        "/出產品*數量\n"
                        "/發產品*數量\n\n"
                        "例如：\n"
                        "/出大卡*10\n"
                        "/發大卡*10"
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
                        "⚠️ 數量必須是數字\n"
                        "例如：/出大卡*10"
                    )
                    return

                if not product_text:
                    reply(
                        event,
                        "⚠️ 請輸入商品\n"
                        "例如：/出大卡*10"
                    )
                    return

            # ------------------------------
            # 原本格式：
            # 發 大卡*10
            # 發 小美 大卡 10
            # ------------------------------
            else:

                body = text[len("發 "):].strip()

                # 發 大卡*10
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
                            "⚠️ 數量必須是數字\n"
                            "例如：發 大卡*10"
                        )
                        return

                    if not product_text:
                        reply(
                            event,
                            "⚠️ 請輸入商品\n"
                            "例如：發 大卡*10"
                        )
                        return

                # 發 小美 大卡 10
                else:

                    parts = body.split()

                    if len(parts) != 3:
                        reply(
                            event,
                            "格式：\n"
                            "發 客戶 商品 數量\n"
                            "例如：發 小美 大卡 10\n\n"
                            "快速格式：\n"
                            "/出大卡*10\n"
                            "/發大卡*10"
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
                            "⚠️ 數量必須是數字"
                        )
                        return

            if (
                quantity <= 0
                or quantity > 20
            ):
                reply(
                    event,
                    "⚠️ 一次發送數量需為 1～20"
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
                        f"⚠️ 找不到商品："
                        f"{product_text}"
                    )
                    return

                if (
                    result["reason"]
                    == "not_enough"
                ):
                    reply(
                        event,
                        f"⚠️ "
                        f"{result['product']['code']} "
                        f"庫存不足\n"
                        f"目前可用："
                        f"{result['available']} 張"
                    )
                    return

            serial_text = "\n".join(
                result["serials"]
            )

            if customer:
                summary_line = (
                    f"{result['product']['code']} × {quantity}  "
                    f"➡️《{customer}》"
                )
            else:
                summary_line = (
                    f"{result['product']['code']} × {quantity}"
                )

            reply_messages(
                event,
                [
                    serial_text,
                    (
                        f"✅ 已出庫\n"
                        f"{summary_line}\n"
                        f"（訂單編號{result['order_no']}）"
                    ),
                ],
            )
            return


        # ------------------------------------------
        # 撤回
        #
        # 撤回這個群組 / 聊天室的上一筆庫存動作
        # 不管上一筆是入庫或出庫
        # ------------------------------------------

        if text == "撤回":

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
                        "⚠️ 這個群組目前沒有可撤回的上一筆動作"
                    )
                    return

                if (
                    result["reason"]
                    == "stock_in_already_used"
                ):
                    reply(
                        event,
                        "⚠️ 無法撤回這次入庫\n"
                        "這一批裡已有序號被出庫，"
                        "為避免破壞訂單，系統沒有刪除。"
                    )
                    return

                reply(
                    event,
                    "⚠️ 這筆動作目前無法撤回"
                )
                return

            if (
                result["action_type"]
                == "stock_in"
            ):
                reply(
                    event,
                    f"↩️ 已撤回上一筆入庫\n"
                    f"{result['product']['code']} × "
                    f"{result['quantity']} 張\n"
                    f"（批次編號{result['ref_no']}）"
                )
                return

            if (
                result["action_type"]
                == "sale"
            ):
                lines = [
                    "↩️ 已撤回上一筆出庫",
                ]

                for item in result["items"]:
                    lines.append(
                        f"{item['product']['code']} × "
                        f"{item['quantity']}"
                    )

                sold_to_values = [
                    item["sold_to"]
                    for item in result["items"]
                    if item["sold_to"]
                ]

                if sold_to_values:
                    lines.append(
                        f"原客戶：《{sold_to_values[0]}》"
                    )

                lines.append(
                    f"（訂單編號{result['ref_no']}）"
                )

                reply(
                    event,
                    "\n".join(lines)
                )
                return


        # ------------------------------------------
        # 今日銷售
        # 以目前 LINE 群組 / 聊天室為主
        # ------------------------------------------

        if text in (
            "今日銷售",
            "今天銷售",
            "/今日銷售",
        ):

            sales = get_today_sales(
                context_id
            )

            if not sales["rows"]:
                reply(
                    event,
                    "📊 今日銷售\n"
                    "目前尚無出庫紀錄"
                )
                return

            lines = [
                "📊 今日銷售",
                f"總出庫：{sales['total_quantity']} 張",
                f"訂單數：{sales['total_orders']} 筆",
                "",
            ]

            for row in sales["rows"]:
                lines.append(
                    f"{row['code']}："
                    f"{row['quantity']} 張"
                    f"（{row['orders']} 筆）"
                )

            reply(
                event,
                "\n".join(lines)
            )
            return


        # ------------------------------------------
        # 查訂單
        # ------------------------------------------

        if text.startswith("查單 "):

            order_no = text[3:].strip()

            order = get_order(
                order_no
            )

            if not order:
                reply(
                    event,
                    f"⚠️ 找不到訂單："
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
                f"🧾 訂單資料\n"
                f"訂單：{order['order_no']}\n"
                f"客戶：{order['sold_to']}\n"
                f"商品：{order['code']}\n"
                f"數量：{order['quantity']}\n"
                f"時間：{created_at}\n\n"
                f"{serial_text}"
            )
            return


        # ------------------------------------------
        # 指令說明
        # ------------------------------------------

        if text == "指令":

            command_text = (
                "📋 可用指令\n\n"
                "查自己的ID：我的ID\n\n"
                "記帳：小美 +1900\n"
                "收款：小美 -1900\n"
                "查帳：查帳 小美\n\n"
                "全部庫存：庫存\n"
                "單品庫存："
                "查庫存 MyCard1000\n\n"
                "入庫：\n"
                "入庫 MyCard1000\n"
                "序號1\n"
                "序號2\n\n"
                "出庫："
                "發 小美 MyCard1000 5\n"
                "快速出庫：/大10 或 /大*10\n""多品項：每行一個，例如 /大10、/小20\n\n"
                "撤回群組上一筆：撤回\n\n"
                "今日銷售：今日銷售\n"
                "查訂單："
                "查單 TXxxxxxxxx"
            )

            if is_admin(operator):
                command_text += (
                    "\n\n👑 管理員指令\n"
                    "加權限 Uxxxxxxxx\n"
                    "刪權限 Uxxxxxxxx\n"
                    "權限名單"
                )

            reply(
                event,
                command_text
            )
            return


        reply(
            event,
            "看不懂這個指令 😆\n"
            "輸入「指令」查看使用方式"
        )

    except Exception as e:

        print(
            "BOT ERROR:",
            repr(e),
        )

        reply(
            event,
            "⚠️ 系統處理失敗，"
            "請稍後再試。"
        )


# ==================================================
# 啟動
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
