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
# 🤖 AI 调用 (带重试 / 限流排队)
# ==========================================================
#
# 🏗️ 两层重试架构说明：
#   第一层 (OpenClaw 内部)：OpenClaw 收到我们的请求后调用上游 LLM provider，
#     如果 provider 返回 429，OpenClaw SDK 层会做短间隔重试（通常几秒级），
#     但如果 Retry-After > 60s 则放弃并把 429 透传给我们。
#   第二层 (本客户端)：我们收到 OpenClaw 返回的 429 时，说明它内部已经放弃了，
#     此时做分钟级的长退避重试，等待 provider 配额窗口刷新后再尝试。
#
# ⚠️ 防雪崩措施：
#   - 全局 threading.Lock 串行化请求，避免并发请求互相挤占配额
#   - 随机抖动 (jitter ±25%)，防止盘前批量分析时多标的同步重试撞车
#   - 只对 OpenClaw 已放弃的 429 做长退避，不与其内部短重试冲突

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


def call_ai_with_retry(
    messages: list[dict],
    *,
    timeout: int = 360,
    caller_label: str = "AI",
) -> tuple[str | None, str | None]:
    """
    向 OpenClaw 发送 chat completion 请求，内置重试与限流排队。

    参数:
        messages     : OpenAI 兼容 messages 列表
        timeout      : 单次请求超时秒数
        caller_label : 日志/通知中使用的调用方标识

    返回: (content, error_msg)
        成功时 content 为 AI 回复文本, error_msg 为 None
        失败时 content 为 None, error_msg 为可读错误描述

    重试策略 (第二层 — 仅在 OpenClaw 内部重试放弃后触发):
        - 遇到 429 / 5xx 状态码时自动重试，最多 _MAX_RETRIES 次
        - 指数退避 + 随机抖动：~60s → ~120s → ~240s（受 _MAX_BACKOFF_SEC 上限约束）
        - 优先尊重服务端 Retry-After 头（如有）
        - 使用全局 threading.Lock 串行化请求，防止并发请求互相挤占配额
    """
    import time as _time

    with _ai_call_lock:
        last_error = ""
        for attempt in range(_MAX_RETRIES + 1):  # 0 = 首次请求, 1..N = 重试
            try:
                res = requests.post(
                    OPENCLAW_URL,
                    headers=OPENCLAW_HEADERS,
                    json={
                        "model": "openclaw/default",
                        "messages": messages,
                    },
                    timeout=timeout,
                )

                # ── 成功 ──
                if res.status_code == 200:
                    return res.json()["choices"][0]["message"]["content"], None

                # ── 提取 OpenClaw 返回的错误详情（用于日志诊断） ──
                error_detail = ""
                try:
                    body = res.json()
                    error_detail = body.get("error", {}).get("message", "") if isinstance(body, dict) else ""
                except Exception:
                    pass

                # ── 可重试错误 ──
                if res.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    # 计算退避时间（指数退避 + 随机抖动）
                    raw_backoff = min(_BASE_BACKOFF_SEC * (2 ** attempt), _MAX_BACKOFF_SEC)

                    # 尊重 Retry-After 头（秒数格式）
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
                return None, f"HTTP {res.status_code}{detail_suffix}"

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
                return None, f"异常: {e}"

        # 所有重试均耗尽
        return None, f"重试{_MAX_RETRIES}次后仍失败 (最后错误: {last_error})"
