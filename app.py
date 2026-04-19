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

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

def clean_response(text):
    # 移除 Markdown 標題
    text = re.sub(r'#{1,6}\s*', '', text)
    # 移除粗體/斜體
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text)
    # \frac{a}{b} → a/b
    text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', text)
    # \sqrt{x} → √x
    text = re.sub(r'\\sqrt\{([^}]+)\}', r'√\1', text)
    text = re.sub(r'\\sqrt\s+(\S+)', r'√\1', text)
    # \vec{AB} 或 \overrightarrow{AB} → AB向量
    text = re.sub(r'\\(?:vec|overrightarrow)\{([^}]+)\}', r'\1向量', text)
    # \cdot → ×
    text = re.sub(r'\\cdot', '×', text)
    # \times → ×
    text = re.sub(r'\\times', '×', text)
    # \hat{i} → i
    text = re.sub(r'\\hat\{([^}]+)\}', r'\1', text)
    # 移除 \begin{...}...\end{...} 行列式區塊
    text = re.sub(r'\\begin\{[^}]+\}.*?\\end\{[^}]+\}', '(行列式計算)', flags=re.DOTALL, string=text)
    # 移除多餘的 $ 符號
    text = re.sub(r'\$+', '', text)
    # 移除反斜線開頭的其他 LaTeX 指令
    text = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', text)
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    # 清理多餘空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# 每個用戶保留最近 10 則對話
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
- 若題目矛盾或無正數解，明確告知學生題目可能有誤"""

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
    user_id = event.source.user_id
    user_message = event.message.text
    logger.info(f"User {user_id}: {user_message}")

    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({"role": "user", "content": user_message})

    # 只保留最近 MAX_HISTORY 則
    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history[user_id]
        )
        reply_text = clean_response(response.choices[0].message.content)[:4900]
        conversation_history[user_id].append({"role": "assistant", "content": reply_text})
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

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    user_id = event.source.user_id
    logger.info(f"User {user_id} sent an image")
    try:
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = b"".join(chunk for chunk in message_content.iter_content())
        image_base64 = base64.b64encode(image_data).decode("utf-8")

        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": "請看這張圖片中的數學題目並解題。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                ]}
            ]
        )
        reply_text = clean_response(response.choices[0].message.content)[:4900]
        logger.info(f"Vision reply: {reply_text[:100]}")
    except Exception as e:
        logger.error(f"Image handling error: {e}\n{traceback.format_exc()}")
        reply_text = "抱歉，無法處理圖片，請稍後再試。"
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        logger.error(f"LINE reply error: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port)
