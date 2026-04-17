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

import re

def _extract_tags(text: str) -> list:
    """从文本中提取标的市场、代码、名称等作为 TG tags"""
    tags = []
    
    # 查找市场和标的代码 (如 AAPL.US, 0700.HK)
    symbols = re.findall(r'\b([A-Za-z0-9]+)\.(HK|US)\b', text)
    unique_symbols = list(set(symbols))
    
    added_markets = set()
    for code, market in unique_symbols:
        if market == 'HK' and 'HK' not in added_markets:
            if "🇭🇰" not in tags: tags.append("🇭🇰")
            if "#HongKong" not in tags: tags.append("#HongKong")
            added_markets.add('HK')
        elif market == 'US' and 'US' not in added_markets:
            if "🇺🇸" not in tags: tags.append("🇺🇸")
            if "#US" not in tags: tags.append("#US")
            added_markets.add('US')
            
        tag_code = f"#{code}"
        if tag_code not in tags:
            tags.append(tag_code)

    # 查找标的名称 (匹配 "名称: 腾讯控股")
    names = re.findall(r'名称:\s*([^\s\|<>\n]+)', text)
    for name in set(names):
        if name and name not in ("N/A", "未知"):
            # 过滤掉非字母、数字、下划线、中文的特殊字符
            clean_name = re.sub(r'[^\w\u4e00-\u9fa5]', '', name)
            if clean_name:
                tag_name = f"#{clean_name}"
                if tag_name not in tags:
                    tags.append(tag_name)
                
    return tags

def _format_text_to_html(text: str) -> str:
    """将普通的 Markdown 文本转义并格式化为 TG 支持的 HTML 形态"""
    # 替换预置符号防爆
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # 1. 匹配 json 代码块并包装成可折叠 (expandable) 的 blockquote
    def replace_json(match):
        return f'<blockquote expandable><pre><code class="language-json">{match.group(1)}</code></pre></blockquote>'
    text = re.sub(r'```json\s*(.*?)\s*```', replace_json, text, flags=re.DOTALL)
    
    # 2. 匹配其余的普通代码块
    def replace_code(match):
        return f'<pre><code>{match.group(1)}</code></pre>'
    text = re.sub(r'```\s*(.*?)\s*```', replace_code, text, flags=re.DOTALL)
    
    # 3. 解析粗体 ** **
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    return text

def _sync_tg_send(text: str, targets: list, parse_mode: str = None):
    """底层直连 TG 遍历发送实时通知"""
    if not targets:
        print("❌ 推送配置(targets)为空，取消推送")
        return

    # 提取通用的内容 tags
    content_tags = _extract_tags(text)
    
    # 强制进行 HTML 转换
    html_base_text = _format_text_to_html(text)
    
    # 如果正文内容过长(超过 500 字符)，自动将主体包裹在可折叠区域中
    if len(text) > 500:
        html_base_text = f"<blockquote expandable>{html_base_text}</blockquote>"

    # targets 是形如 [(bot_token, chat_id), ...] 的列表
    for bot_token, chat_id in targets:
        if not bot_token or not chat_id:
            continue
            
        fixed_tags = []
        # 根据目标频道添加固定的标签
        if str(chat_id) == str(os.getenv("TG_CHANNEL_ID_ORDER")):
            fixed_tags.append("#Order")
        if str(chat_id) == str(os.getenv("TG_CHANNEL_ID_ANALYSIS")):
            fixed_tags.append("#Analysis")
            
        # 其他内容 tags 最多放入 (10 - 固定tag数量) 个
        allowed_content_tags_count = 10 - len(fixed_tags)
        trimmed_content_tags = content_tags[:allowed_content_tags_count]
        
        message_html = html_base_text
        if fixed_tags:
            message_html = " ".join(fixed_tags) + "\n\n" + message_html
            
        if trimmed_content_tags:
            message_html = message_html + "\n\n" + " ".join(trimmed_content_tags)
            
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id, 
            "text": message_html,
            "parse_mode": "HTML"
        }
            
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
