import os
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, abort
import gspread
from google.oauth2.service_account import Credentials

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

# =========================
# LINE 設定
# =========================
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)


# =========================
# Google Sheet 安全設定
# 只允許 Google Sheets API
# 不使用 Google Drive API
# =========================
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = "/etc/secrets/google-service-account.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets"
]

credentials = Credentials.from_service_account_file(
    GOOGLE_CREDENTIALS_FILE,
    scopes=SCOPES
)

gc = gspread.authorize(credentials)

# 只用指定 Spreadsheet ID 開啟「小仙女」
spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)

account_sheet = spreadsheet.worksheet("帳務表")
serial_sheet = spreadsheet.worksheet("序號表")
log_sheet = spreadsheet.worksheet("交易紀錄")


def now_tw():
    return datetime.now(
        ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M:%S")


def get_operator(event):
    """記錄 LINE 操作者 ID，不要求額外個資權限。"""
    try:
        return event.source.user_id or "未知"
    except Exception:
        return "未知"


def reply(event, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)],
            )
        )


# =========================
# 網站健康檢查
# =========================
@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running!", 200


# =========================
# LINE Webhook
# =========================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK", 200


# =========================
# LINE 指令
# =========================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    operator = get_operator(event)

    try:

        # -------------------------
        # 測試
        # -------------------------
        if text == "測試":
            reply(event, "收到！機器人正常運作")
            return


        # -------------------------
        # 記帳
        # 範例：小美 +1900
        #       小美 -1900
        # -------------------------
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
                        amount_text.replace(",", "")
                    )
                except ValueError:
                    reply(event, "⚠️ 金額格式不正確")
                    return

                account_type = (
                    "加帳" if amount > 0 else "收款"
                )

                account_sheet.append_row([
                    now_tw(),
                    customer,
                    amount,
                    account_type,
                    "",
                    operator,
                ])

                balance = get_customer_balance(customer)

                reply(
                    event,
                    f"✅ 已記帳\n"
                    f"客戶：{customer}\n"
                    f"本次：{amount:+,}\n"
                    f"目前餘額：{balance:,}"
                )
                return


        # -------------------------
        # 查帳
        # 範例：查帳 小美
        # -------------------------
        if text.startswith("查帳 "):
            customer = text[3:].strip()

            if not customer:
                reply(event, "請輸入客戶名稱")
                return

            balance = get_customer_balance(customer)

            reply(
                event,
                f"👤 {customer}\n"
                f"目前帳款：${balance:,}"
            )
            return


        # -------------------------
        # 庫存
        # -------------------------
        if text == "庫存":
            records = serial_sheet.get_all_records()

            inventory = {}

            for row in records:
                product = str(row.get("商品", "")).strip()
                status = str(row.get("狀態", "")).strip()

                if product and status == "未售":
                    inventory[product] = (
                        inventory.get(product, 0) + 1
                    )

            if not inventory:
                reply(event, "📦 目前沒有未售庫存")
                return

            lines = ["📦 目前庫存"]

            for product in sorted(inventory):
                lines.append(
                    f"{product}：{inventory[product]} 張"
                )

            reply(event, "\n".join(lines))
            return


        # -------------------------
        # 發序號
        # 範例：發 MyCard1000 2
        # -------------------------
        if text.startswith("發 "):
            parts = text.split()

            if len(parts) != 3:
                reply(
                    event,
                    "格式：發 商品 數量\n"
                    "例如：發 MyCard1000 2"
                )
                return

            product = parts[1]

            try:
                quantity = int(parts[2])
            except ValueError:
                reply(event, "⚠️ 數量必須是數字")
                return

            if quantity <= 0 or quantity > 20:
                reply(
                    event,
                    "⚠️ 一次發送數量需為 1～20"
                )
                return

            send_serials(
                event,
                operator,
                product,
                quantity
            )
            return


        # -------------------------
        # 使用說明
        # -------------------------
        if text == "指令":
            reply(
                event,
                "📋 可用指令\n"
                "小美 +1900\n"
                "小美 -1900\n"
                "查帳 小美\n"
                "庫存\n"
                "發 MyCard1000 2"
            )
            return

        reply(
            event,
            "看不懂這個指令 😆\n"
            "輸入「指令」查看使用方式"
        )

    except Exception as e:
        print("BOT ERROR:", repr(e))
        reply(
            event,
            "⚠️ 系統處理失敗，請稍後再試。"
        )


# =========================
# 查客戶帳款
# =========================
def get_customer_balance(customer):
    records = account_sheet.get_all_records()

    balance = 0

    for row in records:
        if str(row.get("客戶", "")).strip() == customer:
            try:
                balance += int(
                    str(row.get("金額", 0))
                    .replace(",", "")
                )
            except Exception:
                pass

    return balance


# =========================
# 發序號 + 扣庫存
# =========================
def send_serials(event, operator, product, quantity):

    values = serial_sheet.get_all_values()

    if len(values) <= 1:
        reply(event, "⚠️ 序號表目前沒有資料")
        return

    headers = values[0]

    try:
        product_col = headers.index("商品")
        serial_col = headers.index("序號")
        status_col = headers.index("狀態")
        sold_time_col = headers.index("售出時間")
        operator_col = headers.index("操作人")
    except ValueError:
        reply(
            event,
            "⚠️ 序號表欄位名稱不正確"
        )
        return

    available = []

    # 第 1 列是標題，所以從第 2 列開始
    for sheet_row, row in enumerate(
        values[1:],
        start=2
    ):
        # 補足空白欄位
        while len(row) < len(headers):
            row.append("")

        row_product = row[product_col].strip()
        row_status = row[status_col].strip()

        if (
            row_product == product
            and row_status == "未售"
        ):
            available.append({
                "row": sheet_row,
                "serial": row[serial_col],
            })

            if len(available) == quantity:
                break

    if len(available) < quantity:
        reply(
            event,
            f"⚠️ {product} 庫存不足\n"
            f"目前只有 {len(available)} 張"
        )
        return

    sold_time = now_tw()
    serials = []

    for item in available:
        row_number = item["row"]
        serial = item["serial"]

        # 狀態 → 已售
        serial_sheet.update_cell(
            row_number,
            status_col + 1,
            "已售"
        )

        # 售出時間
        serial_sheet.update_cell(
            row_number,
            sold_time_col + 1,
            sold_time
        )

        # 操作人
        serial_sheet.update_cell(
            row_number,
            operator_col + 1,
            operator
        )

        serials.append(serial)

    # 寫交易紀錄
    log_sheet.append_row([
        sold_time,
        "發序號",
        product,
        quantity,
        "",
        operator,
        "",
    ])

    remaining = count_stock(product)

    serial_text = "\n".join(serials)

    reply(
        event,
        f"✅ 已發 {product} × {quantity}\n\n"
        f"{serial_text}\n\n"
        f"剩餘庫存：{remaining} 張"
    )


# =========================
# 計算單一商品庫存
# =========================
def count_stock(product):
    records = serial_sheet.get_all_records()

    count = 0

    for row in records:
        if (
            str(row.get("商品", "")).strip() == product
            and str(row.get("狀態", "")).strip() == "未售"
        ):
            count += 1

    return count


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=port
    )
