"""
共享工具模块 (Shared Utilities)
premarket_planner.py 和 intraday_sentry.py 的公共逻辑提取到此处，
消除代码重复，确保两个脚本的行为一致。

包含：
- 路径/环境初始化
- 缓存读写 (read_cache / write_cache)
- 交易计划读写 (load_plan / save_plan)
- 标的代码转换 (lb_to_futu)
- 基本面数据获取 (fetch_static_info / fetch_financial_indexes)
- Tavily 资讯获取 (fetch_latest_news)
- 期权数据获取 (fetch_option_snapshot)
"""

import json
import os
import sys
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv, find_dotenv

# ==========================================================
# 📦 路径设置（全局只初始化一次）
# ==========================================================
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # for_openclaw/strategies/
ROOT_DIR    = os.path.dirname(_SCRIPT_DIR)                  # for_openclaw/

# 确保 longbridge / futu / telegram 模块可寻址
for _sub in ("longbridge", "futu", "telegram"):
    _path = os.path.join(ROOT_DIR, _sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)

# 加载 .env
load_dotenv(find_dotenv(), override=True)

# ==========================================================
# ⚙️ 公共配置
# ==========================================================
PLAN_FILE = os.path.join(ROOT_DIR, "data", "daily_trading_plan.json")
CACHE_DIR = os.path.join(ROOT_DIR, "data", "cache")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

OPENCLAW_URL = "http://127.0.0.1:18789/v1/chat/completions"
OPENCLAW_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {os.getenv('OPENCLAW_GATEWAY_TOKEN', '')}",
    "x-openclaw-scopes": "operator.admin,operator.write",
}

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

# ==========================================================
# 🛠️ 缓存读写
# ==========================================================

def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def read_cache(filename: str) -> dict | list | None:
    """读取本地 JSON 缓存文件，失败返回 None"""
    path = os.path.join(CACHE_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def write_cache(filename: str, data):
    """写入本地 JSON 缓存文件"""
    _ensure_cache_dir()
    path = os.path.join(CACHE_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==========================================================
# 📋 交易计划读写
# ==========================================================

def load_plan() -> dict:
    """读取当前交易计划 JSON"""
    if os.path.exists(PLAN_FILE):
        with open(PLAN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_plan(plan: dict):
    """保存交易计划 JSON"""
    os.makedirs(os.path.dirname(PLAN_FILE), exist_ok=True)
    with open(PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=4)


# ==========================================================
# 🔄 标的代码转换
# ==========================================================

def lb_to_futu(symbol: str) -> str:
    """
    将长桥格式代码转换为富途格式代码。
    长桥: 'AAPL.US' / '0700.HK' / '9988.HK'
    富途: 'US.AAPL' / 'HK.00700' / 'HK.09988'

    注意: 富途港股代码固定 5 位，需补零；美股代码直接拼接。
    """
    parts = symbol.rsplit(".", 1)
    if len(parts) != 2:
        return symbol
    ticker, market = parts[0], parts[1].upper()
    if market == "HK":
        ticker = ticker.lstrip("0").zfill(5)   # 去掉多余前导零后补足为 5 位
    return f"{market}.{ticker}"


# ==========================================================
# 📡 基础数据获取（with 缓存策略）
# ==========================================================

# 延迟导入：避免循环导入，只在函数内使用
def _get_lb():
    from longbridge_server import (
        _logic_get_static_info,
        _logic_get_financial_indexes,
        _logic_get_market_temperature,
    )
    return _logic_get_static_info, _logic_get_financial_indexes, _logic_get_market_temperature


def _get_futu():
    from futu_options_server import (
        _logic_get_expiry_dates,
        _logic_get_option_chain,
        _logic_get_option_snapshots,
    )
    return _logic_get_expiry_dates, _logic_get_option_chain, _logic_get_option_snapshots


def fetch_static_info(symbol: str) -> str:
    """
    获取标的基本静态信息（名称/板块/货币/手数/总股本）—— 永久缓存。
    两个脚本共用 cache/static_info_<symbol>.json。
    """
    _logic_get_static_info, _, _ = _get_lb()

    cache_file = f"static_info_{symbol.replace('.', '_')}.json"
    cached = read_cache(cache_file)

    if cached:
        static = cached
    else:
        static = _logic_get_static_info(symbol)
        if static and "error" not in static:
            write_cache(cache_file, static)

    if not static or "error" in static:
        return f"标的基本信息获取失败: {static.get('error', 'Unknown') if static else 'Unknown'}"

    return (
        f"  名称: {static.get('name', 'N/A')} | "
        f"板块: {static.get('board', 'N/A')} | "
        f"货币: {static.get('currency', 'N/A')} | "
        f"每手: {static.get('lot_size', 'N/A')} | "
        f"总股本: {static.get('total_shares', 'N/A')}"
    )


def fetch_financial_indexes(symbol: str) -> str:
    """
    获取标的估值指标（PE/PB/总市值/股息率/换手率/量比）—— 当日缓存。
    """
    _, _logic_get_financial_indexes, _ = _get_lb()

    today_str  = datetime.now().strftime("%Y-%m-%d")
    cache_file = f"financials_{symbol.replace('.', '_')}.json"
    cached     = read_cache(cache_file)

    if cached and isinstance(cached, dict) and cached.get("_date") == today_str:
        fin = cached
    else:
        fin = _logic_get_financial_indexes(symbol)
        if fin and "error" not in fin:
            fin["_date"] = today_str
            write_cache(cache_file, fin)

    if not fin or "error" in fin:
        return f"估值指标获取失败: {fin.get('error', 'Unknown') if fin else 'Unknown'}"

    labels = {
        "total_market_value":  "总市值",
        "pe_ttm_ratio":        "PE(TTM)",
        "pb_ratio":            "PB",
        "dividend_ratio_ttm": "股息率(TTM)",
        "turnover_rate":       "换手率",
        "volume_ratio":        "量比",
    }
    parts = [f"{label}: {fin[key]}" for key, label in labels.items() if fin.get(key)]
    return " | ".join(parts) if parts else "估值指标暂不可用"


# ==========================================================
# 📰 Tavily 资讯
# ==========================================================

def fetch_latest_news(symbol: str, mode: str = "basic") -> str:
    """
    通过 Tavily 拉取最新资讯。
    mode: "basic"  — 盘中使用（速度优先，3 条结果）
          "advanced" — 盘前使用（深度搜索，5 条结果）
    """
    if not TAVILY_API_KEY:
        return "未配置 Tavily API，暂无资讯。"
    try:
        ticker = symbol.split(".")[0]
        is_advanced = (mode == "advanced")
        query = (
            f"{ticker} stock latest news analysis earnings outlook"
            if is_advanced
            else f"{ticker} stock latest news today financial"
        )
        res = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": mode,
                "include_answer": True,
                "max_results": 5 if is_advanced else 3,
            },
            timeout=15 if is_advanced else 10,
        )
        if res.status_code == 200:
            data = res.json()
            text = f"【AI 总结】: {data.get('answer', '无')}\n"
            for idx, r in enumerate(data.get("results", [])):
                text += f"  {idx+1}. {r.get('title', '')}\n"
            return text
        return "资讯获取失败。"
    except Exception as e:
        return f"资讯拉取异常: {e}"


# ==========================================================
# 🛡️ 期权 ATM 探针
# ==========================================================

def fetch_option_snapshot(symbol: str, current_price: float) -> str:
    """
    期权异动探针：智能选期（本周末 + 两周后 + 四周后）→ 提取 ATM 合约 → 查 IV/OI（含 YahooQuery 降级）。
    输入 symbol 使用长桥格式，内部自动转换为富途格式。
    返回可读字符串。
    """
    _logic_get_expiry_dates, _logic_get_option_chain, _logic_get_option_snapshots = _get_futu()

    try:
        futu_code = lb_to_futu(symbol)  # e.g. "AAPL.US" -> "US.AAPL"
        opt_dates_raw = _logic_get_expiry_dates(futu_code)

        if isinstance(opt_dates_raw, dict) and "error" in opt_dates_raw:
            return f"期权到期日获取失败: {opt_dates_raw['error']}"
        if not isinstance(opt_dates_raw, list) or not opt_dates_raw:
            return "该标的暂无期权到期日。"

        today = datetime.now().date()
        future_dates = []
        for r in opt_dates_raw:
            date_str = r.get("strike_time", "")
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                if d > today:
                    future_dates.append(d)
            except (ValueError, TypeError):
                pass
        future_dates = sorted(future_dates)
        if not future_dates:
            return "该标的暂无未来期权到期日。"

        days_to_friday = (4 - today.weekday()) % 7 or 7
        this_friday    = today + timedelta(days=days_to_friday)

        # 自适应中期到期日：
        #   如果距本周五 <=3 天（周三/周四/周五），选"下下周末"，避免前两个日期太贴近
        #   否则（周一/周二）选"下周末"
        next_weeks = 2 if days_to_friday <= 3 else 1
        middle_friday  = today + timedelta(days=days_to_friday + next_weeks * 7)
        four_weeks_out = today + timedelta(days=28)

        def pick_closest(dates, target):
            return min(dates, key=lambda d: abs((d - target).days))

        near_date       = pick_closest(future_dates, this_friday)
        middle_date     = pick_closest(future_dates, middle_friday)
        four_week_date  = pick_closest(future_dates, four_weeks_out)
        middle_label    = "下周末" if next_weeks == 1 else "下下周末"

        # 去重：三个目标日期可能重合
        seen = set()
        targets = []
        for d, label in [(near_date, "本周末"), (middle_date, middle_label), (four_week_date, "四周后")]:
            if d not in seen:
                seen.add(d)
                targets.append((d, label))

        # 如果没有现价（盘前场景），先获取实时报价
        if current_price <= 0:
            try:
                from longbridge_server import _logic_get_live_quote
                quote = _logic_get_live_quote(symbol)
                current_price = quote.get("price", 0)
            except Exception:
                pass

        target_symbols: list[str] = []
        date_labels: dict[str, str] = {}

        for exp_date, label in targets:
            exp_str = exp_date.strftime("%Y-%m-%d")
            chain = _logic_get_option_chain(futu_code, exp_str)
            if not isinstance(chain, list) or not chain:
                continue

            # 富途链每条有 option_type("CALL"/"PUT") + strike_price + futu_code
            calls = [c for c in chain if "CALL" in str(c.get("option_type", "")).upper()]
            puts  = [c for c in chain if "PUT"  in str(c.get("option_type", "")).upper()]

            for contracts, direction in [(calls, "Call"), (puts, "Put")]:
                if not contracts:
                    continue
                atm = min(contracts, key=lambda o: abs((o.get("strike_price") or 0) - current_price))
                fc  = atm.get("futu_code", "")
                sp  = atm.get("strike_price", "N/A")
                if fc:
                    target_symbols.append(fc)
                    date_labels[fc] = f"{label}({exp_str}) {direction} 行权价={sp}"

        if not target_symbols:
            return "该标的暂无查询到有效期权合约。"

        # 批量查行情（富途官方 → YahooQuery 自动降级）
        opt_quotes = _logic_get_option_snapshots(target_symbols)
        if not isinstance(opt_quotes, list) or not opt_quotes:
            return "期权行情请求返回空。"

        lines = []
        for q in opt_quotes:
            if "error" in q:
                continue
            fc  = q.get("futu_code", "")
            lbl = date_labels.get(fc, fc)
            try:
                iv_val = q.get("implied_volatility")
                iv_str = f"{float(iv_val):.1f}%" if iv_val is not None else "N/A"
            except (TypeError, ValueError):
                iv_str = "N/A"
            src = q.get("_source", "")
            lines.append(
                f"  📌 {lbl} | IV={iv_str} | "
                f"OI={q.get('open_interest', 'N/A')} | "
                f"Vol={q.get('volume', 'N/A')} | "
                f"Last={q.get('last_price', 'N/A')}"
                + (f" [{src}]" if src else "")
            )

        if not lines:
            return "期权行情匹配为空。"

        result = "期权深度分析 (ATM IV/OI 探针):\n" + "\n".join(lines)
        if any("降级" in q.get("_source", "") or "Fallback" in q.get("_source", "") for q in opt_quotes):
            result = "[降级通道 YahooQuery] " + result
        return result

    except Exception as e:
        return f"期权数据暂不可用: {e}"


# ==========================================================
# 🤖 AI 调用 (带重试 / 限流排队 / 多通道降级)
# ==========================================================
#
# 🏗️ 架构说明：
#   _call_openai_compatible  — 通用底层引擎，适配任何 OpenAI 兼容端点
#   _call_deepseek / _call_openclaw / _call_new_api — 薄包装，提供各自的 URL/Header/Model
#   call_ai_with_retry       — 入口方法，按优先级列表依次尝试，失败降级
#
# ⚠️ 防雪崩措施：
#   - 全局 threading.Lock 串行化请求，避免并发请求互相挤占配额
#   - 随机抖动 (jitter ±25%)，防止盘前批量分析时多标的同步重试撞车

import threading as _threading
import random as _random

# 全局互斥锁：同一时刻只允许一个 AI 请求在飞，避免并发请求导致 429 叠加
_ai_call_lock = _threading.Lock()

# 可重试的 HTTP 状态码
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# 默认重试参数
_MAX_RETRIES = 3          # 最多重试次数（不含首次请求）
_BASE_BACKOFF_SEC = 60    # 首次重试基础等待秒数（429 场景通常需要较长等待）
_MAX_BACKOFF_SEC = 360    # 单次最大等待秒数
_JITTER_RATIO = 0.25      # 退避时间随机抖动幅度 (±25%)


def _backoff_with_jitter(base: float) -> float:
    """在 base 基础上添加 ±25% 随机抖动，防止多请求同步重试撞车"""
    jitter = base * _JITTER_RATIO
    return base + _random.uniform(-jitter, jitter)


# ----------------------------------------------------------
# 🔧 通用 OpenAI 兼容端点调用引擎（内部方法）
# ----------------------------------------------------------

def _call_openai_compatible(
    *,
    url: str,
    headers: dict,
    model: str,
    messages: list[dict],
    max_tokens: int,
    timeout: int,
    caller_label: str,
) -> tuple[str | None, str | None, dict | None]:
    """
    向任意 OpenAI Chat Completions 兼容端点发送请求，内置重试与退避。

    调用方须在 _ai_call_lock 外部自行加锁（或由上层入口统一加锁）。
    本方法 **不** 获取 _ai_call_lock，以支持入口方法跨通道降级时共享同一把锁。

    返回: (content, error_msg, metadata)
    """
    import time as _time

    last_error = ""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            res = requests.post(
                url,
                headers=headers,
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                },
                timeout=timeout,
            )

            # ── 成功 ──
            if res.status_code == 200:
                resp_body = res.json()
                content = resp_body["choices"][0]["message"]["content"]
                finish_reason = resp_body["choices"][0].get("finish_reason", "stop")

                usage = resp_body.get("usage", {})
                raw_model = resp_body.get("model", model)
                metadata = {
                    "name": raw_model,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }

                if finish_reason == "length":
                    warn = (
                        f"⚠️ [{caller_label}] AI 回复被截断 (finish_reason=length, "
                        f"max_tokens={max_tokens})，输出不完整！"
                    )
                    print(warn)
                    return content, warn, metadata

                return content, None, metadata

            # ── 提取错误详情 ──
            error_detail = ""
            try:
                body = res.json()
                error_detail = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
            except Exception:
                pass

            # ── 可重试错误 ──
            if res.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                raw_backoff = min(_BASE_BACKOFF_SEC * (2 ** attempt), _MAX_BACKOFF_SEC)

                retry_after = res.headers.get("Retry-After")
                if retry_after:
                    try:
                        raw_backoff = max(raw_backoff, int(retry_after))
                    except (ValueError, TypeError):
                        pass

                backoff = _backoff_with_jitter(raw_backoff)
                last_error = f"HTTP {res.status_code}"
                detail_suffix = f" ({error_detail})" if error_detail else ""
                print(
                    f"⚠️ [{caller_label}] 请求返回 {res.status_code}{detail_suffix}，"
                    f"{backoff:.0f}秒后重试 ({attempt + 1}/{_MAX_RETRIES})..."
                )
                _time.sleep(backoff)
                continue

            # ── 不可重试的非 200 ──
            detail_suffix = f" ({error_detail})" if error_detail else ""
            return None, f"HTTP {res.status_code}{detail_suffix}", None

        except requests.exceptions.Timeout:
            last_error = f"请求超时 ({timeout}s)"
            if attempt < _MAX_RETRIES:
                backoff = _backoff_with_jitter(
                    min(_BASE_BACKOFF_SEC * (2 ** attempt), _MAX_BACKOFF_SEC)
                )
                print(
                    f"⚠️ [{caller_label}] 请求超时，"
                    f"{backoff:.0f}秒后重试 ({attempt + 1}/{_MAX_RETRIES})..."
                )
                _time.sleep(backoff)
                continue

        except Exception as e:
            return None, f"异常: {e}", None

    return None, f"重试{_MAX_RETRIES}次后仍失败 (最后错误: {last_error})", None


def resolve_ai_model_name(ai_self_report: str | None, metadata: dict | None) -> str | None:
    """
    根据 channel 选择准确的模型标识。

    - OpenClaw 链路：信任 AI 自报 _ai_model（网关有身份注入，准确）
    - NewAPI 链路：AI 自报不可靠，使用 metadata 中的 provider/name 构造准确值

    返回: 准确的模型标识字符串（如 "Vertex_AI/gemini-3.1-pro-preview"），
          无数据时返回 None。
    """
    if not metadata:
        return ai_self_report  # 无 metadata 时只能用 AI 自报

    channel = metadata.get("channel", "")
    if channel == "OpenClaw" and ai_self_report:
        return ai_self_report

    # NewAPI 或其他：用 API metadata 构造准确值
    provider = metadata.get("provider", "")
    name = metadata.get("name", "")
    if provider and name:
        return f"{provider}/{name}"
    return name or ai_self_report


def format_ai_meta_footer(ai_model_name: str | None, metadata: dict | None) -> str:
    """
    格式化 AI 元数据尾注（channel / provider / name + Token 用量）。

    参数:
        ai_model_name : resolve_ai_model_name() 的返回值（准确的模型标识）
        metadata      : call_ai_with_retry 返回的 metadata dict

    返回: 带分隔线的完整尾注字符串，无数据时返回空字符串。
          格式示例:
          ━━━━━━━━━━━━━━━━━━━━━
          📡 通道: NewAPI | 提供商: Vertex_AI | 模型: gemini-3.1-pro-preview
          📊 Token: 输入 81 + 输出 604 = 合计 685
    """
    if not metadata:
        return ""

    parts: list[str] = []

    # 通道 + 提供商/模型 信息行
    channel = metadata.get("channel", "N/A")
    provider = metadata.get("provider", "")
    model_name = metadata.get("name", "")
    if channel == "OpenClaw" and ai_model_name:
        # OpenClaw 链路：AI 自报的提供商/模型名更可靠
        parts.append(f"📡 通道: {channel} | 🤖 模型: {ai_model_name}")
    elif provider:
        # NewAPI 链路（或 OpenClaw 无自报时）：使用显式传入的提供商 + API 返回的模型名
        parts.append(f"📡 通道: {channel} | 提供商: {provider} | 模型: {model_name}")
    else:
        parts.append(f"📡 通道: {channel} | 模型: {model_name}")

    # Token 用量行
    parts.append(
        f"📊 Token: "
        f"输入 {metadata.get('prompt_tokens', 0):,} + "
        f"输出 {metadata.get('completion_tokens', 0):,} = "
        f"合计 {metadata.get('total_tokens', 0):,}"
    )

    return "\n━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(parts)


# ----------------------------------------------------------
# 📡 Channel 薄包装
# ----------------------------------------------------------
#
# metadata 统一字段命名：
#   channel  — 调用链路 (DeepSeek / OpenClaw / NewAPI)
#   provider — 模型提供商 (DeepSeek / Vertex_AI / Google / ...)
#   name     — 模型名 (gemini-3.1-pro-preview / ...)

def _call_deepseek(messages, *, max_tokens, timeout, caller_label):
    """DeepSeek 官方 API 通道（OpenAI 兼容格式）。"""
    if not DEEPSEEK_API_KEY:
        return None, "未配置 DeepSeek API key，请在 .env 中设置 DEEPSEEK_API_KEY", None

    content, error, metadata = _call_openai_compatible(
        url=f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        model=DEEPSEEK_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        timeout=timeout,
        caller_label=f"{caller_label}|DeepSeek",
    )
    if metadata:
        metadata["channel"] = "DeepSeek"
        metadata["provider"] = "DeepSeek"
    return content, error, metadata


def _call_openclaw(messages, *, max_tokens, timeout, caller_label):
    """OpenClaw 网关通道（provider 和 name 由 AI 自报 _ai_model 字段提供）"""
    content, error, metadata = _call_openai_compatible(
        url=OPENCLAW_URL,
        headers=OPENCLAW_HEADERS,
        model="openclaw/default",
        messages=messages,
        max_tokens=max_tokens,
        timeout=timeout,
        caller_label=f"{caller_label}|OpenClaw",
    )
    if metadata:
        metadata["channel"] = "OpenClaw"
        # provider/name 由下游从 AI 回复 JSON 的 _ai_model 字段提取，此处不设定
    return content, error, metadata


# ----------------------------------------------------------
# 🌐 New-API 多提供商配置
# ----------------------------------------------------------
# new-api 通过不同的 api-key 区分渠道/提供商。
# 每个提供商一条配置，新增渠道只需：
#   1. 在 .env 中添加对应的 API key（如 NEW_API_KEY_OPENAI=sk-xxx）
#   2. 在 _NEW_API_PROVIDERS 中添加一条配置
#   3. 在 _AI_PROVIDER_CHAIN 中添加对应的降级条目

NEW_API_BASE_URL = os.getenv("NEW_API_BASE_URL", "http://127.0.0.1:23000/v1")

_NEW_API_PROVIDERS = {
    "Vertex_AI": {
        "api_key": os.getenv("NEW_API_KEY_Vertex_AI", ""),
        "model": "gemini-3.1-pro-preview",
    },
    # 扩展示例（取消注释并在 .env 中配置即可启用）：
    # "OpenAI": {
    #     "api_key": os.getenv("NEW_API_KEY_OPENAI", ""),
    #     "model": "gpt-4o",
    # },
}


def _call_new_api(messages, *, max_tokens, timeout, caller_label, provider="Vertex_AI"):
    """
    VPS new-api 通道。
    provider: 提供商名称，对应 _NEW_API_PROVIDERS 中的 key。
              不同提供商使用不同的 api-key 和模型。
    """
    cfg = _NEW_API_PROVIDERS.get(provider)
    if not cfg:
        return None, f"未知的 NewAPI 提供商: {provider}", None
    api_key = cfg["api_key"]
    if not api_key:
        return None, f"未配置 {provider} 的 API key，请在 .env 中设置", None

    content, error, metadata = _call_openai_compatible(
        url=f"{NEW_API_BASE_URL.rstrip('/')}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        model=cfg["model"],
        messages=messages,
        max_tokens=max_tokens,
        timeout=timeout,
        caller_label=f"{caller_label}|NewAPI/{provider}",
    )
    if metadata:
        metadata["channel"] = "NewAPI"
        metadata["provider"] = provider
    return content, error, metadata


# ----------------------------------------------------------
# 🗂️ 优先级列表 — 按顺序尝试，前者失败则降级到后者
# ----------------------------------------------------------
# 每项: (名称, 调用函数)
# 调整顺序即可改变优先级，新增渠道只需添加一行。
# 如需指定不同的 NewAPI 提供商，用 lambda 传参：
#   ("NewAPI/OpenAI", lambda m, **kw: _call_new_api(m, provider="OpenAI", **kw)),
_AI_PROVIDER_CHAIN = [
    ("DeepSeek",         _call_deepseek),   # 默认 provider="DeepSeek"
    ("NewAPI/Vertex_AI", _call_new_api),
    ("OpenClaw",         _call_openclaw),
]


# ----------------------------------------------------------
# 🚀 入口方法 (对外接口不变)
# ----------------------------------------------------------

def call_ai_with_retry(
    messages: list[dict],
    *,
    max_tokens: int = 8192,
    timeout: int = 360,
    caller_label: str = "AI",
) -> tuple[str | None, str | None, dict | None]:
    """
    AI 调用入口 — 按优先级依次尝试多个 channel，失败自动降级。

    当前优先级: DeepSeek → NewAPI/Vertex_AI → OpenClaw

    参数:
        messages     : OpenAI 兼容 messages 列表
        max_tokens   : AI 回复的最大 token 数（防截断，默认 8192）
        timeout      : 单次请求超时秒数
        caller_label : 日志/通知中使用的调用方标识

    返回: (content, error_msg, metadata)
        成功时 content 为 AI 回复文本, error_msg 为 None
        失败时 content 为 None, error_msg 为所有通道的错误汇总
        metadata 字段: channel(调用链路), provider(提供商), name(模型名),
                       prompt_tokens, completion_tokens, total_tokens
        ⚠️ 若 AI 回复被截断 (finish_reason=length)，content 仍会返回（截断的内容），
           同时 error_msg 会包含截断警告信息。
    """
    errors: list[str] = []

    with _ai_call_lock:
        for chain_name, chain_fn in _AI_PROVIDER_CHAIN:
            print(f"🔄 [{caller_label}] 尝试通道: {chain_name}")
            content, error, metadata = chain_fn(
                messages,
                max_tokens=max_tokens,
                timeout=timeout,
                caller_label=caller_label,
            )

            # 成功 或 截断（content 有值）→ 直接返回，不再降级
            if content is not None:
                return content, error, metadata

            # 记录失败，继续降级
            errors.append(f"[{chain_name}] {error}")
            print(f"❌ [{caller_label}] 通道 {chain_name} 失败: {error}，尝试降级...")

    # 所有通道均失败
    combined = "; ".join(errors)
    return None, f"所有 AI 通道均失败: {combined}", None


def call_new_api(
    messages: list[dict],
    *,
    provider: str = "Vertex_AI",
    max_tokens: int = 8192,
    timeout: int = 360,
    caller_label: str = "NewAPI",
) -> tuple[str | None, str | None, dict | None]:
    """
    直接调用 VPS new-api 的独立入口（不走降级链）。
    provider: 提供商名称，默认 Vertex_AI。
    """
    with _ai_call_lock:
        return _call_new_api(
            messages,
            provider=provider,
            max_tokens=max_tokens,
            timeout=timeout,
            caller_label=caller_label,
        )


def call_deepseek(
    messages: list[dict],
    *,
    max_tokens: int = 8192,
    timeout: int = 360,
    caller_label: str = "DeepSeek",
) -> tuple[str | None, str | None, dict | None]:
    """
    直接调用 DeepSeek 官方 API 的独立入口（不走降级链）。
    """
    with _ai_call_lock:
        return _call_deepseek(
            messages,
            max_tokens=max_tokens,
            timeout=timeout,
            caller_label=caller_label,
        )


def call_openclaw(
    messages: list[dict],
    *,
    max_tokens: int = 8192,
    timeout: int = 360,
    caller_label: str = "OpenClaw",
) -> tuple[str | None, str | None, dict | None]:
    """
    直接调用 OpenClaw 网关的独立入口（不走降级链）。
    适用于明确只想使用 OpenClaw 的场景。
    """
    with _ai_call_lock:
        return _call_openclaw(
            messages,
            max_tokens=max_tokens,
            timeout=timeout,
            caller_label=caller_label,
        )
