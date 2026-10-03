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

# LINE 操作權限白名單
# Render 環境變數：ALLOWED_LINE_USER_IDS
# 多個 user_id 用逗號分隔
ALLOWED_LINE_USER_IDS = {
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


def is_authorized(operator):
    return operator in ALLOWED_LINE_USER_IDS


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


# ==================================================
# Supabase：發序號
#
# FOR UPDATE SKIP LOCKED：
# 多人同時操作時避免拿到相同序號
# ==================================================

def sell_serials(
    operator,
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
                    operator
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    order_no,
                    product["id"],
                    customer,
                    quantity,
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
                        operator
                    )
                    VALUES (%s, %s, 'available', %s)
                    ON CONFLICT (serial)
                    DO NOTHING
                    RETURNING serial
                    """,
                    (
                        product["id"],
                        serial_value,
                        operator,
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

def undo_sale(
    operator,
    order_no=None,
):
    conn = get_db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cur:

            if order_no:
                cur.execute(
                    """
                    SELECT
                        t.id,
                        t.order_no,
                        t.sold_to,
                        t.quantity,
                        t.product_id,
                        p.code,
                        p.name
                    FROM transactions t
                    JOIN products p
                      ON p.id = t.product_id
                    WHERE LOWER(t.order_no) = LOWER(%s)
                      AND t.operator = %s
                    LIMIT 1
                    FOR UPDATE OF t
                    """,
                    (
                        order_no,
                        operator,
                    ),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        t.id,
                        t.order_no,
                        t.sold_to,
                        t.quantity,
                        t.product_id,
                        p.code,
                        p.name
                    FROM transactions t
                    JOIN products p
                      ON p.id = t.product_id
                    WHERE t.operator = %s
                    ORDER BY
                        t.created_at DESC,
                        t.id DESC
                    LIMIT 1
                    FOR UPDATE OF t
                    """,
                    (operator,),
                )

            order = cur.fetchone()

            if not order:
                conn.rollback()

                return {
                    "ok": False,
                    "reason": "order_not_found",
                }

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

            serial_values = [
                row["serial"]
                for row in serial_rows
            ]

            cur.execute(
                """
                UPDATE serials
                SET
                    status = 'available',
                    sold_to = NULL,
                    order_no = NULL,
                    sold_at = NULL,
                    operator = NULL
                WHERE order_no = %s
                """,
                (order["order_no"],),
            )

            cur.execute(
                """
                DELETE FROM transactions
                WHERE id = %s
                """,
                (order["id"],),
            )

        conn.commit()

        return {
            "ok": True,
            "order": order,
            "serials": serial_values,
        }

    except Exception:
        conn.rollback()
        raise

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
        # 權限檢查
        # 未列入白名單者不可操作機器人
        # ------------------------------------------

        if not is_authorized(operator):
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
        # 發序號
        #
        # 發 小美 MyCard1000 5
        # ------------------------------------------

        if text.startswith("發 "):

            parts = text.split()

            if len(parts) != 4:
                reply(
                    event,
                    "格式：\n"
                    "發 客戶 商品 數量\n\n"
                    "例如：\n"
                    "發 小美 MyCard1000 5"
                )
                return

            customer = parts[1]
            product_text = parts[2]

            try:
                quantity = int(parts[3])

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
                    "⚠️ 一次發送數量"
                    "需為 1～20"
                )
                return

            result = sell_serials(
                operator,
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

            reply_messages(
                event,
                [
                    serial_text,
                    (
                        f"✅ 已出庫\n"
                        f"{result['product']['code']} × {quantity}  ➡️《{customer}》\n"
                        f"（訂單編號{result['order_no']}）"
                    ),
                ],
            )
            return


        # ------------------------------------------
        # 撤回出庫
        #
        # 撤回
        # 撤回 TXxxxxxxxx
        # ------------------------------------------

        if (
            text == "撤回"
            or text.startswith("撤回 ")
        ):

            order_no = None

            if text.startswith("撤回 "):
                order_no = (
                    text[len("撤回 "):]
                    .strip()
                )

                if not order_no:
                    order_no = None

            result = undo_sale(
                operator,
                order_no,
            )

            if not result["ok"]:
                reply(
                    event,
                    "⚠️ 找不到可撤回的出庫訂單"
                )
                return

            restored = len(
                result["serials"]
            )

            reply(
                event,
                f"↩️ 已撤回出庫\n"
                f"{result['order']['code']} × {restored}\n"
                f"原客戶：《{result['order']['sold_to']}》\n"
                f"（訂單編號{result['order']['order_no']}）\n"
                f"序號已恢復庫存"
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

            reply(
                event,
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
                "發 小美 MyCard1000 5\n\n"
                "撤回最後一筆：撤回\n"
                "撤回指定訂單："
                "撤回 TXxxxxxxxx\n\n"
                "查訂單："
                "查單 TXxxxxxxxx"
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
