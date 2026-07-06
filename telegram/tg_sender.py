import os
import re
import time
import threading
import queue
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
_TG_MAX_MESSAGE_CHARS = 4096
_TG_SAFE_MARGIN_CHARS = 32
_send_queue = queue.Queue()
_send_worker_lock = threading.Lock()
_send_worker_started = False


def _parse_markdown_parts(text: str) -> list:
    """切分普通文本与 Markdown fenced code block。"""
    parts = []          # 元素: ('text', raw_str) 或 ('code', lang, content)
    last_end = 0

    for m in _CODE_BLOCK_RE.finditer(text):
        before = text[last_end:m.start()]
        if before:
            parts.append(('text', before))
        lang = m.group(2).lower() or 'plain'
        content = m.group(3).strip()
        parts.append(('code', lang, content))
        last_end = m.end()

    tail = text[last_end:]
    if tail:
        parts.append(('text', tail))
    return parts


def _render_code_html(lang: str, content: str) -> str:
    escaped_content = _escape_html(content)
    if lang == 'json':
        inner = f'<pre><code class="language-json">{escaped_content}</code></pre>'
    else:
        inner = f'<pre><code>{escaped_content}</code></pre>'
    return f'<blockquote expandable>{inner}</blockquote>'


def _render_text_html(raw: str, collapse_text: bool) -> str:
    rendered = _render_markdown(_escape_html(raw.strip()))
    if collapse_text:
        return f'<blockquote expandable>{rendered}</blockquote>'
    return rendered


def _render_part_html(part: tuple, collapse_text: bool) -> str:
    if part[0] == 'code':
        _, lang, content = part
        return _render_code_html(lang, content)
    return _render_text_html(part[1], collapse_text)


def _format_and_build_html(text: str, max_text_len: int = 500) -> str:
    """
    将原始文本转为 TG HTML。规则：
    - 代码块（```json / ``` 等）→ 格式化代码 + 自动折叠 <blockquote expandable>
    - 普通文字总长度 > max_text_len → 自动折叠
    - Tags 由调用方拼接在消息最底部（本函数不处理 tags）
    """
    parts = _parse_markdown_parts(text)

    # ── Step 2：计算普通文本总长度，决定是否折叠 ──────────────────────────────
    normal_text_total = sum(len(p[1]) for p in parts if p[0] == 'text')
    collapse_text = normal_text_total > max_text_len

    # ── Step 3：逐段渲染 HTML ─────────────────────────────────────────────────
    html_parts = [
        _render_part_html(part, collapse_text)
        for part in parts
        if part[0] == 'code' or part[1].strip()
    ]
    return "\n\n".join(html_parts)


def _fits_html(raw: str, max_html_len: int, collapse_text: bool) -> bool:
    return len(_render_text_html(raw, collapse_text)) <= max_html_len


def _take_text_prefix(raw: str, max_html_len: int, collapse_text: bool) -> str:
    """二分寻找渲染后不超过 TG 预算的最长普通文本前缀。"""
    lo, hi = 1, len(raw)
    best = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _fits_html(raw[:mid], max_html_len, collapse_text):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return raw[:best]


def _split_text_html(raw: str, max_html_len: int, collapse_text: bool) -> list:
    """按段落/换行/空格切普通文本，避免切坏 HTML 标签。"""
    chunks = []
    remaining = raw.strip()
    while remaining:
        if _fits_html(remaining, max_html_len, collapse_text):
            chunks.append(_render_text_html(remaining, collapse_text))
            break

        prefix = _take_text_prefix(remaining, max_html_len, collapse_text)
        cut = len(prefix)
        split_at = -1
        for sep in ("\n\n", "\n", " "):
            pos = remaining.rfind(sep, 0, cut)
            if pos > max(1, int(cut * 0.55)):
                split_at = pos + len(sep)
                break

        if split_at <= 0:
            split_at = cut

        piece = remaining[:split_at].strip()
        if piece:
            chunks.append(_render_text_html(piece, collapse_text))
        remaining = remaining[split_at:].strip()
    return chunks


def _split_oversized_code_html(lang: str, content: str, max_html_len: int) -> list:
    """
    仅在单个代码块超过 Telegram 单条硬上限时降级切分。
    正常 JSON 会作为原子块保留在同一条消息中。
    """
    chunks = []
    remaining = content.strip()
    while remaining:
        if len(_render_code_html(lang, remaining)) <= max_html_len:
            chunks.append(_render_code_html(lang, remaining))
            break

        lo, hi = 1, len(remaining)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if len(_render_code_html(lang, remaining[:mid])) <= max_html_len:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        split_at = remaining.rfind("\n", 0, best)
        if split_at <= 0:
            split_at = best
        piece = remaining[:split_at].strip()
        if piece:
            chunks.append(_render_code_html(lang, piece))
        remaining = remaining[split_at:].strip()
    return chunks


def _format_and_split_html(text: str, max_html_len: int, max_text_len: int = 500) -> list:
    """
    将 Markdown 渲染成 TG HTML，并按 Telegram 长度限制切分。

    fenced code block（尤其 ```json）作为原子块处理：只要单个代码块本身
    没超过 TG 硬上限，就不会被切到两条消息里。
    """
    parts = _parse_markdown_parts(text)
    normal_text_total = sum(len(p[1]) for p in parts if p[0] == 'text')
    collapse_text = normal_text_total > max_text_len

    chunks = []
    current = []
    current_len = 0

    def flush():
        nonlocal current, current_len
        if current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0

    def append_block(block: str):
        nonlocal current_len
        sep_len = 2 if current else 0
        if current and current_len + sep_len + len(block) > max_html_len:
            flush()
            sep_len = 0
        current.append(block)
        current_len += sep_len + len(block)

    i = 0
    while i < len(parts):
        part = parts[i]
        if part[0] == 'text':
            raw = part[1].strip()
            if not raw:
                i += 1
                continue
            for block in _split_text_html(raw, max_html_len, collapse_text):
                append_block(block)
            i += 1
            continue

        _, lang, content = part
        block = _render_code_html(lang, content)
        if lang == 'json' and i + 1 < len(parts) and parts[i + 1][0] == 'text':
            following_raw = parts[i + 1][1].strip()
            if following_raw:
                tail_block = f"{block}\n\n{_render_text_html(following_raw, collapse_text)}"
                if len(tail_block) <= max_html_len:
                    # JSON 通常位于报告末尾；优先绑定 JSON + meta footer。
                    # 但只有当前消息放不下时，append_block 才会切到下一条。
                    append_block(tail_block)
                    i += 2
                    continue

        if len(block) <= max_html_len:
            append_block(block)
        else:
            print(f"⚠️ TG 代码块超过单条消息上限，降级切分: lang={lang}, len={len(block)}")
            flush()
            for code_block in _split_oversized_code_html(lang, content, max_html_len):
                append_block(code_block)
                flush()
        i += 1

    flush()
    return chunks or [""]


def _sync_tg_send(text: str, targets: list, parse_mode: str = None):
    """底层直连 TG 遍历发送实时通知"""
    if not targets:
        print("❌ 推送配置(targets)为空，取消推送")
        return

    # 提取通用的内容 tags（在原始文本上提取，不受 HTML 转义影响）
    content_tags = _extract_tags(text)

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

        # ── Tags 固定追加在消息最底部，并计入每条消息的 TG 长度预算 ─────────
        tag_suffix = f"\n\n{' '.join(all_tags)}" if all_tags else ""
        max_body_len = _TG_MAX_MESSAGE_CHARS - len(tag_suffix) - _TG_SAFE_MARGIN_CHARS
        if max_body_len < 1000:
            max_body_len = _TG_MAX_MESSAGE_CHARS - _TG_SAFE_MARGIN_CHARS

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        message_chunks = _format_and_split_html(text, max_body_len)

        for idx, html_body in enumerate(message_chunks, start=1):
            message_html = f"{html_body}{tag_suffix}" if tag_suffix else html_body
            payload = {
                "chat_id": chat_id,
                "text": message_html,
                "parse_mode": "HTML",
            }

            try:
                response = _tg_session.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    suffix = f" ({idx}/{len(message_chunks)})" if len(message_chunks) > 1 else ""
                    print(f"✅ 成功推送到 ID: {chat_id}{suffix}")
                else:
                    print(f"❌ 推送失败 ID: {chat_id}, 错误详情: {response.text}")
            except Exception as e:
                print(f"⚠️ TG 发送异常 (ID: {chat_id}): {e}")
            finally:
                time.sleep(4)  # 防止触发 TG API 发送频率限制


def _tg_send_worker():
    while True:
        text, targets, parse_mode = _send_queue.get()
        try:
            _sync_tg_send(text, targets, parse_mode)
        except Exception as e:
            print(f"⚠️ TG 队列发送异常: {e}")
        finally:
            _send_queue.task_done()


def _ensure_send_worker():
    global _send_worker_started
    if _send_worker_started:
        return
    with _send_worker_lock:
        if _send_worker_started:
            return
        t = threading.Thread(target=_tg_send_worker, daemon=True)
        t.start()
        _send_worker_started = True


def send_message_async(text: str, targets: list, parse_mode: str = None):
    """发送 Telegram 消息 (后台队列免阻塞，并保持调用顺序)"""
    _ensure_send_worker()
    _send_queue.put((text, targets, parse_mode))
