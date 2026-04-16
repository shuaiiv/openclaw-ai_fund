import os
import time
import threading
import requests
from dotenv import load_dotenv

# 加载环境变量
load_dotenv(override=True)

# 全局复用的 Session，提升请求效率并复用底层 TCP/TLS 连接
_tg_session = requests.Session()
_tg_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
})

def _sync_tg_send(text: str, targets: list, parse_mode: str = None):
    """底层直连 TG 遍历发送实时通知"""
    if not targets:
        print("❌ 推送配置(targets)为空，取消推送")
        return

    # targets 是形如 [(bot_token, chat_id), ...] 的列表
    for bot_token, chat_id in targets:
        if not bot_token or not chat_id:
            continue
            
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
            
        try:
            response = _tg_session.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                print(f"✅ 成功推送到 ID: {chat_id}")
            else:
                print(f"❌ 推送失败 ID: {chat_id}, 错误详情: {response.text}")
        except Exception as e:
            print(f"⚠️ TG 发送异常 (ID: {chat_id}): {e}")
        finally:
            time.sleep(4)  # 防止触发 TG API 发送频率限制

def send_message_async(text: str, targets: list, parse_mode: str = None):
    """发送 Telegram 消息 (后台线程免阻塞)"""
    t = threading.Thread(target=_sync_tg_send, args=(text, targets, parse_mode))
    t.daemon = True
    t.start()
