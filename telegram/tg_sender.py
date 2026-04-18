import os
import re
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


def _extract_tags(text: str) -> list:
    """从文本中提取标的市场、代码、名称等作为 TG tags"""
    tags = []

    # 查找市场和标的代码 (如 AAPL.US, 0700.HK)
    symbols = re.findall(r'\b([A-Za-z0-9]+)\.(HK|US)\b', text)
    unique_symbols = list(dict.fromkeys(symbols))  # 保序去重

    added_markets = set()
    for code, market in unique_symbols:
        if market == 'HK' and 'HK' not in added_markets:
            if "🇭🇰" not in tags: tags.append("🇭🇰")
            if "#HK" not in tags: tags.append("#HK")
            added_markets.add('HK')
        elif market == 'US' and 'US' not in added_markets:
            if "🇺🇸" not in tags: tags.append("🇺🇸")
            if "#US" not in tags: tags.append("#US")
            added_markets.add('US')

        # Telegram Tag 不支持 "."，所以用 "_" 替代以保持整体连靠
        tag_code = f"#{code}_{market}"
        if tag_code not in tags:
            tags.append(tag_code)

    # 查找标的名称 (匹配 "名称: 腾讯控股")
    names = re.findall(r'名称:\s*([^\s\|<>\n]+)', text)
    seen_names = set()
    for name in names:
        if name and name not in ("N/A", "未知") and name not in seen_names:
            seen_names.add(name)
            clean_name = re.sub(r'[^\w\u4e00-\u9fa5]', '', name)
            if clean_name:
                tag_name = f"#{clean_name}"
                if tag_name not in tags:
                    tags.append(tag_name)

    return tags


def _escape_html(text: str) -> str:
    """仅转义 HTML 必要字符"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_markdown(text: str) -> str:
    """将 Markdown 中的粗体转为 TG HTML（在 HTML 转义之后调用）"""
    # 粗体 **text**
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # 单行代码 `code`
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    return text


# 切分规则：匹配所有 ``` 代码块（含或不含语言标记）
_CODE_BLOCK_RE = re.compile(r'(```(\w*)\s*\n?(.*?)\s*```)', re.DOTALL)


def _format_and_build_html(text: str, max_text_len: int = 500) -> str:
    """
    将原始文本转为 TG HTML。规则：
    - 代码块（```json / ``` 等）→ 格式化代码 + 自动折叠 <blockquote expandable>
    - 普通文字总长度 > max_text_len → 自动折叠
    - Tags 由调用方拼接在消息最底部（本函数不处理 tags）
    """
    # ── Step 1：切分普通文本与代码块 ──────────────────────────────────────────
    parts = []          # 元素: ('text', raw_str) 或 ('code', lang, content)
    last_end = 0

    for m in _CODE_BLOCK_RE.finditer(text):
        # 代码块之前的普通文本
        before = text[last_end:m.start()]
        if before:
            parts.append(('text', before))
        lang = m.group(2).lower() or 'plain'
        content = m.group(3).strip()
        parts.append(('code', lang, content))
        last_end = m.end()

    # 尾部剩余普通文本
    tail = text[last_end:]
    if tail:
        parts.append(('text', tail))

    # ── Step 2：计算普通文本总长度，决定是否折叠 ──────────────────────────────
    normal_text_total = sum(len(p[1]) for p in parts if p[0] == 'text')
    collapse_text = normal_text_total > max_text_len

    # ── Step 3：逐段渲染 HTML ─────────────────────────────────────────────────
    html_parts = []

    for part in parts:
        if part[0] == 'code':
            _, lang, content = part
            escaped_content = _escape_html(content)
            # JSON 代码块：带语言高亮标记；其他代码块：通用 <pre><code>
            if lang == 'json':
                inner = f'<pre><code class="language-json">{escaped_content}</code></pre>'
            else:
                inner = f'<pre><code>{escaped_content}</code></pre>'
            # 代码块始终折叠
            html_parts.append(f'<blockquote expandable>{inner}</blockquote>')

        else:  # 'text'
            raw = part[1].strip()
            if not raw:
                continue
            rendered = _render_markdown(_escape_html(raw))
            if collapse_text:
                html_parts.append(f'<blockquote expandable>{rendered}</blockquote>')
            else:
                html_parts.append(rendered)

    return "\n\n".join(html_parts)


def _sync_tg_send(text: str, targets: list, parse_mode: str = None):
    """底层直连 TG 遍历发送实时通知"""
    if not targets:
        print("❌ 推送配置(targets)为空，取消推送")
        return

    # 提取通用的内容 tags（在原始文本上提取，不受 HTML 转义影响）
    content_tags = _extract_tags(text)

    # 渲染正文 HTML（不含 tags）
    html_body = _format_and_build_html(text)

    for bot_token, chat_id in targets:
        if not bot_token or not chat_id:
            continue

        # ── 组装频道固定 tags ────────────────────────────────────────────────
        fixed_tags = []
        if str(chat_id) == str(os.getenv("TG_CHANNEL_ID_ORDER")):
            fixed_tags.append("#Order")
        if str(chat_id) == str(os.getenv("TG_CHANNEL_ID_ANALYSIS")):
            fixed_tags.append("#Analysis")

        # 内容 tags 数量上限（TG 单条消息建议 tag 不超过 10 个）
        allowed = 10 - len(fixed_tags)
        all_tags = fixed_tags + content_tags[:allowed]

        # ── Tags 固定追加在消息最底部 ────────────────────────────────────────
        message_html = html_body
        if all_tags:
            message_html = f"{html_body}\n\n{' '.join(all_tags)}"

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message_html,
            "parse_mode": "HTML",
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
