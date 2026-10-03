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

    text = event.message.text.strip()

    operator = get_operator(event)

    try:

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

            reply(
                event,
                f"✅ 出庫完成\n"
                f"客戶：{customer}\n"
                f"商品："
                f"{result['product']['code']}\n"
                f"數量：{quantity}\n"
                f"訂單："
                f"{result['order_no']}\n\n"
                f"{serial_text}\n\n"
                f"剩餘庫存："
                f"{result['remaining']} 張"
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
                "記帳：小美 +1900\n"
                "收款：小美 -1900\n"
                "查帳：查帳 小美\n\n"
                "全部庫存：庫存\n"
                "單品庫存："
                "查庫存 MyCard1000\n\n"
                "出庫："
                "發 小美 MyCard1000 5\n\n"
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
