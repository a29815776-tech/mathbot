from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
from groq import Groq
import os
import logging
import traceback
import base64
import re
import requests
import psycopg2
import json
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# 付費用戶的 LINE user ID，用逗號分隔存在環境變數 PAID_USER_IDS
PAID_USER_IDS = set(uid.strip() for uid in os.environ.get("PAID_USER_IDS", "").split(",") if uid.strip())
ADMIN_LINE_ID = os.environ.get("ADMIN_LINE_ID", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
MONTHLY_QUOTA = 200
FREE_DAILY_QUOTA = 14

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usage (
                    user_id TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    month TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    user_id TEXT PRIMARY KEY,
                    history TEXT,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)

def get_usage(user_id, period):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count, month FROM usage WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if not row or row[1] != period:
                cur.execute("""
                    INSERT INTO usage (user_id, count, month) VALUES (%s, 0, %s)
                    ON CONFLICT (user_id) DO UPDATE SET count = 0, month = %s
                """, (user_id, period, period))
                return 0
            return row[0]

def increment_usage(user_id, period):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO usage (user_id, count, month) VALUES (%s, 1, %s)
                ON CONFLICT (user_id) DO UPDATE SET count = usage.count + 1, month = %s
            """, (user_id, period, period))

def load_history(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT history FROM conversations WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
            return []

def save_history(user_id, history):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO conversations (user_id, history, updated_at) VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET history = %s, updated_at = NOW()
            """, (user_id, json.dumps(history), json.dumps(history)))

try:
    init_db()
    logger.info("Database initialized")
except Exception as e:
    logger.error(f"DB init error: {e}")
RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "")

def add_paid_user(new_user_id):
    current = set(uid.strip() for uid in os.environ.get("PAID_USER_IDS", "").split(",") if uid.strip())
    current.add(new_user_id)
    PAID_USER_IDS.add(new_user_id)
    new_value = ",".join(current)

    query = """
    mutation variableUpsert($input: VariableUpsertInput!) {
        variableUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "projectId": RAILWAY_PROJECT_ID,
            "environmentId": RAILWAY_ENVIRONMENT_ID,
            "serviceId": RAILWAY_SERVICE_ID,
            "name": "PAID_USER_IDS",
            "value": new_value
        }
    }
    resp = requests.post(
        "https://backboard.railway.app/graphql/v2",
        headers={"Authorization": f"Bearer {RAILWAY_API_TOKEN}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=10
    )
    return resp.json()

FREE_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
PAID_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"  # 之後換成 Claude

def get_model(user_id):
    return PAID_MODEL if user_id in PAID_USER_IDS else FREE_MODEL

def call_ai(model, messages):
    return groq_client.chat.completions.create(model=model, messages=messages)

def notify_admin(msg):
    if ADMIN_LINE_ID:
        try:
            line_bot_api.push_message(ADMIN_LINE_ID, TextSendMessage(text=f"[Bot錯誤通知]\n{msg}"))
        except Exception:
            pass

def clean_response(text):
    text = text.replace('¥', '\\')
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text)
    text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', text)
    text = re.sub(r'\\sqrt\{([^}]+)\}', r'√\1', text)
    text = re.sub(r'\\sqrt\s+(\S+)', r'√\1', text)
    text = re.sub(r'\\(?:vec|overrightarrow)\{([^}]+)\}', r'\1向量', text)
    text = re.sub(r'\\cdot', '×', text)
    text = re.sub(r'\\times', '×', text)
    text = re.sub(r'\\hat\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\begin\{[^}]+\}.*?\\end\{[^}]+\}', '(行列式計算)', flags=re.DOTALL, string=text)
    text = re.sub(r'\$+', '', text)
    # 重複處理直到沒有殘留（處理巢狀結構）
    for _ in range(8):
        prev = text
        text = re.sub(r'\\frac\{([^{}]*)\}\{([^{}]*)\}', r'\1/\2', text)
        text = re.sub(r'\\sqrt\{([^{}]*)\}', r'√\1', text)
        text = re.sub(r'\\(?:vec|overrightarrow)\{([^{}]*)\}', r'\1向量', text)
        text = re.sub(r'\\[a-zA-Z]+\{([^{}]*)\}', r'\1', text)
        text = re.sub(r'\\[a-zA-Z]+', '', text)
        text = re.sub(r'\{([^{}]*)\}', r'\1', text)
        if text == prev:
            break
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

MAX_HISTORY = 10

SYSTEM_PROMPT = """你是一個專門幫助台灣高中生解數學題的助手，針對108課綱設計。

格式規則（非常重要）：
- 只能使用純文字，不可使用 Markdown（禁止 ##、**、- 列表符號）
- 不可使用 LaTeX（禁止 $、\frac、\vec、\times、\begin 等符號）
- 分數用 a/b 表示，例如 15/2
- 向量用 AB向量 表示，例如 AB向量 = (-3, 4, 0)
- 根號用 √ 表示，例如 √769
- 換行用空白行分隔步驟

解題規則：
1. 用繁體中文回答
2. 解題步驟清楚，符合學測格式
3. 每個步驟必須說明理由
4. 最後寫出「答：」
5. 只回答數學相關問題

數學計算規則：
- 每一步的數字運算必須精確，絕對不能猜測或跳步
- 得到答案後，必須代回原方程式驗算，確認正確再輸出
- 若題目矛盾或無正數解，明確告知學生題目可能有誤

常見觀念澄清（學生容易誤解，請主動說明清楚）：
- 外積不需要垂直：|AB向量 × AC向量| = |AB||AC|sin θ，sin θ 已包含夾角，所以任意兩向量都能用外積算平行四邊形面積，再除以2得三角形面積，不需要兩向量垂直
- 三維向量 (a,b,c) 的長度 = √(a²+b²+c²)，這是基本定義
- 內積為零才代表垂直，外積的大小代表平行四邊形面積
- 相似三角形面積比 = 邊長比的平方
- 排列組合：有限制條件的要先處理限制條件
- 對數：log(a×b) = log a + log b，不是 log a × log b
- 絕對值方程式：|x| = a 若 a < 0 則無解
- 二次函數判別式 < 0 表示無實數根，不是無解（複數根存在）
- 向量共線：AB向量 = k × AC向量，k 為實數
- 空間中兩直線可能為歪斜線（不相交也不平行）

訂閱資訊（當用戶詢問訂閱、付費、升級等相關問題時告知）：
- 免費版：每天 14 則訊息
- 進階版：每月 70 元，每月 200 則訊息，用完後降回每日 14 則，下月重置
- 付款方式：第一銀行（代碼007）帳號 21257048971，或街口支付帳號 905432635
- 付款後將截圖傳至 LINE ID：a0970801250，並告知自己的 LINE ID（傳「我的ID」可查詢）
- 確認後將開通進階版"""

@app.route("/test")
def test_api():
    try:
        response = call_ai(FREE_MODEL, [{"role": "user", "content": "say hi in traditional chinese"}])
        return f"OK: {response.choices[0].message.content}"
    except Exception as e:
        return f"ERROR: {e}\n{traceback.format_exc()}", 500

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.info(f"Webhook received, body length: {len(body)}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        abort(400)
    except Exception as e:
        logger.error(f"Webhook handler error: {e}\n{traceback.format_exc()}")
        abort(500)
    return 'OK'

SUBSCRIBE_MSG = """訂閱進階版數學機器人

費用：每月 70 元

付款方式：
1. 第一銀行（代碼 007）帳號 21257048971
2. 街口支付 帳號 905432635

付款後請將截圖傳送至 LINE ID：a0970801250，並告知您的 LINE ID（傳「我的ID」可查詢），確認後將為您開通進階版。

進階版功能：每月 200 則訊息，用完後自動降回每日 14 則免費版，下月自動重置。"""

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    quota_id = f"math:{user_id}"
    user_message = event.message.text
    model = get_model(user_id)
    is_paid = user_id in PAID_USER_IDS
    logger.info(f"User {user_id} ({'paid' if is_paid else 'free'}): {user_message}")

    if user_id == ADMIN_LINE_ID and user_message.strip().startswith("!approve "):
        target_id = user_message.strip().split(" ", 1)[1].strip()
        try:
            add_paid_user(target_id)
            reply = f"已開通付費版：{target_id}"
        except Exception as e:
            reply = f"開通失敗：{e}"
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception as ex:
            logger.error(f"Admin reply error: {ex}")
        return

    if user_message.strip() in ["我的id", "my id", "myid", "我的ID", "MY ID"]:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"你的 LINE ID 是：{user_id}"))
        except Exception as e:
            logger.error(f"ID reply error: {e}")
        return

    if user_message.strip() in ["訂閱", "subscribe", "付費", "升級"]:
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=SUBSCRIBE_MSG))
        except Exception as e:
            logger.error(f"Subscribe reply error: {e}")
        return

    if is_paid:
        month_period = datetime.now().strftime("%Y-%m")
        monthly_usage = get_usage(quota_id, month_period)
        if monthly_usage < MONTHLY_QUOTA:
            period = month_period
            quota = MONTHLY_QUOTA
            quota_msg = ""
        else:
            period = datetime.now().strftime("%Y-%m-%d")
            quota = FREE_DAILY_QUOTA
            quota_msg = f"本月 {MONTHLY_QUOTA} 則訊息已用完，已降回每日 {FREE_DAILY_QUOTA} 則免費版。"
    else:
        period = datetime.now().strftime("%Y-%m-%d")
        quota = FREE_DAILY_QUOTA
        quota_msg = f"今日免費額度（{FREE_DAILY_QUOTA} 則）已用完，明天再來或傳「訂閱」升級進階版。"

    try:
        usage = get_usage(quota_id, period)
        if usage >= quota:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=quota_msg))
            return
    except Exception as e:
        logger.error(f"Usage check error: {e}")

    try:
        history = load_history(user_id)
    except Exception as e:
        logger.error(f"Load history error: {e}")
        history = []

    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    reply_text = "抱歉，系統暫時無法回應，請稍後再試。"
    try:
        response = call_ai(model, [{"role": "system", "content": SYSTEM_PROMPT}] + history)
        reply_text = clean_response(response.choices[0].message.content)[:4900]
        history.append({"role": "assistant", "content": reply_text})
        try:
            save_history(user_id, history)
        except Exception as e:
            logger.error(f"Save history error: {e}")
        try:
            increment_usage(quota_id, period)
        except Exception as e:
            logger.error(f"Usage increment error: {e}")
        logger.info(f"Reply: {reply_text[:100]}")
    except Exception as e:
        logger.error(f"AI error: {e}\n{traceback.format_exc()}")
        notify_admin(f"AI error for user {user_id}: {e}")
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        logger.info("Reply sent successfully")
    except Exception as e:
        logger.error(f"LINE reply error: {e}\n{traceback.format_exc()}")

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    quota_id = f"math:{user_id}"
    is_paid = user_id in PAID_USER_IDS
    model = get_model(user_id)
    logger.info(f"User {user_id} sent an image")

    if is_paid:
        month_period = datetime.now().strftime("%Y-%m")
        monthly_usage = get_usage(quota_id, month_period)
        if monthly_usage < MONTHLY_QUOTA:
            period = month_period
            quota = MONTHLY_QUOTA
            quota_msg = ""
        else:
            period = datetime.now().strftime("%Y-%m-%d")
            quota = FREE_DAILY_QUOTA
            quota_msg = f"本月 {MONTHLY_QUOTA} 則訊息已用完，已降回每日 {FREE_DAILY_QUOTA} 則免費版。"
    else:
        period = datetime.now().strftime("%Y-%m-%d")
        quota = FREE_DAILY_QUOTA
        quota_msg = f"今日免費額度（{FREE_DAILY_QUOTA} 則）已用完，明天再來或傳「訂閱」升級進階版。"

    try:
        usage = get_usage(quota_id, period)
        if usage >= quota:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=quota_msg))
            return
    except Exception as e:
        logger.error(f"Usage check error: {e}")

    try:
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = b"".join(chunk for chunk in message_content.iter_content())
        image_base64 = base64.b64encode(image_data).decode("utf-8")

        response = call_ai(model, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "請自己獨立計算解題，不可直接採用圖片中看到的答案。若圖片中有學生的解題過程，請同時檢查他的步驟哪裡出錯，並說明正確做法。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]}
        ])
        reply_text = clean_response(response.choices[0].message.content)[:4900]
        try:
            increment_usage(quota_id, period)
        except Exception as e:
            logger.error(f"Usage increment error: {e}")
        logger.info(f"Vision reply: {reply_text[:100]}")
    except Exception as e:
        logger.error(f"Image handling error: {e}\n{traceback.format_exc()}")
        notify_admin(f"Image error for user {user_id}: {e}")
        reply_text = "抱歉，無法處理圖片，請稍後再試。"
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        logger.error(f"LINE reply error: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)
