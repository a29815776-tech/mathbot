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

FREE_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
PAID_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"  # 之後換成 Claude

def get_model(user_id):
    return PAID_MODEL if user_id in PAID_USER_IDS else FREE_MODEL

def call_ai(model, messages):
    return groq_client.chat.completions.create(model=model, messages=messages)

def clean_response(text):
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
    text = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

conversation_history = {}
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
- 空間中兩直線可能為歪斜線（不相交也不平行）"""

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text
    model = get_model(user_id)
    is_paid = user_id in PAID_USER_IDS
    logger.info(f"User {user_id} ({'paid' if is_paid else 'free'}): {user_message}")

    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({"role": "user", "content": user_message})

    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]

    try:
        response = call_ai(model, [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history[user_id])
        reply_text = clean_response(response.choices[0].message.content)[:4900]
        conversation_history[user_id].append({"role": "assistant", "content": reply_text})
        logger.info(f"Reply: {reply_text[:100]}")
    except Exception as e:
        logger.error(f"AI error: {e}\n{traceback.format_exc()}")
        reply_text = "抱歉，系統暫時無法回應，請稍後再試。"
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        logger.info("Reply sent successfully")
    except Exception as e:
        logger.error(f"LINE reply error: {e}\n{traceback.format_exc()}")

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    model = get_model(user_id)
    logger.info(f"User {user_id} sent an image")
    try:
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = b"".join(chunk for chunk in message_content.iter_content())
        image_base64 = base64.b64encode(image_data).decode("utf-8")

        response = call_ai(model, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "請看這張圖片中的數學題目並解題。"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]}
        ])
        reply_text = clean_response(response.choices[0].message.content)[:4900]
        logger.info(f"Vision reply: {reply_text[:100]}")
    except Exception as e:
        logger.error(f"Image handling error: {e}\n{traceback.format_exc()}")
        reply_text = "抱歉，無法處理圖片，請稍後再試。"
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        logger.error(f"LINE reply error: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)
