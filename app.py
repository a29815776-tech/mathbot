from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from groq import Groq
import os
import logging
import traceback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

SYSTEM_PROMPT = """你是一個專門幫助台灣高中生解數學題的助手，針對108課綱設計。
解題規則：
1. 用繁體中文回答
2. 解題步驟清楚，符合學測格式
3. 每個步驟說明理由
4. 最後寫出「答：」
5. 只回答數學相關問題"""

@app.route("/test")
def test_groq():
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "say hi in traditional chinese"}]
        )
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
    user_message = event.message.text
    logger.info(f"User message: {user_message}")
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ]
        )
        reply_text = response.choices[0].message.content
        logger.info(f"Groq reply: {reply_text[:100]}")
    except Exception as e:
        logger.error(f"Groq error: {e}\n{traceback.format_exc()}")
        reply_text = "抱歉，系統暫時無法回應，請稍後再試。"
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
        logger.info("Reply sent successfully")
    except Exception as e:
        logger.error(f"LINE reply error: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)
