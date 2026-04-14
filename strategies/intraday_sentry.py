import json
import os
import sys
import time
import re
import requests
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv, find_dotenv
from longbridge.openapi import Period, Market

# ==========================================================
# 📦 路径设置：将 longbridge/ 加入 sys.path，以便寻址 longbridge_server
# 目录结构: for_openclaw/longbridge/, for_openclaw/strategies/
# ==========================================================
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # for_openclaw/strategies/
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)                  # for_openclaw/
sys.path.insert(0, os.path.join(_ROOT_DIR, "longbridge"))  # 将 longbridge/ 加入搜索路径
sys.path.insert(0, os.path.join(_ROOT_DIR, "futu"))        # 将 futu/ 加入搜索路径

# ==========================================
# 📦 从 longbridge_server 导入所有封装好的函数
# 彻底解耦：此文件不再直接依赖 longbridge SDK 的上下文创建逻辑
# ==========================================
from longbridge_server import (
    get_account_asset,                  # 获取账户购买力与持仓
    _logic_get_live_quote,              # 获取单只股票实时行情
    _logic_get_capital_distribution,    # 获取主力资金分布
    _logic_get_history_kline,           # 获取历史 K 线 (返回 dict 列表)
    _logic_get_static_info,             # 获取标的基本静态信息
    _logic_get_financial_indexes,       # 获取估值指标 (PE/PB/市値等)
    _logic_get_market_temperature,      # 获取市场温度
    submit_trade_order,                 # 下单 (内置 TG 通知)
    cancel_order_by_id,                 # 撤单
    get_order_status_by_id,             # 查询订单状态
    _logic_get_trading_days,            # 交易日查询
    close_contexts,                     # 释放 WebSocket 连接
)

# 期权模块使用富途 API
from futu_options_server import (
    _logic_get_expiry_dates,            # 获取期权到期日列表
    _logic_get_option_chain,            # 获取指定到期日的期权链 (futu 格式)
    _logic_get_option_snapshots,        # 获取期权深度行情 IV/OI (含 YahooQuery 降级)
    close_context as close_futu_context, # 释放富途 OpenD 连接
)


# override=True 确保覆盖系统环境中可能存在的同名旧变量
load_dotenv(load_dotenv(), override=True)


TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN_CLAW")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID")
_ROOT_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # OpenClaw/
PLAN_FILE     = os.path.join(_ROOT_DIR, "data", "daily_trading_plan.json")
CACHE_DIR     = os.path.join(_ROOT_DIR, "data", "cache")   # 与 premarket_planner 共享同一缓存目录
POLL_INTERVAL = 300  # 5分钟轮询

# 🚨 系统提示词
PROMPT_FILE = os.path.join(_ROOT_DIR, "prompts", "intraday_sentry_prompt.md")
with open(PROMPT_FILE, "r", encoding="utf-8") as f:
    INTRADAY_SENTRY_PROMPT = f.read()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

OPENCLAW_URL = "http://127.0.0.1:18789/v1/chat/completions"
OPENCLAW_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {os.getenv('OPENCLAW_GATEWAY_TOKEN', '')}",
    "x-openclaw-scopes": "operator.admin,operator.write",
}

# ===========================================================================
# 🛠️ 工具函数层
# ===========================================================================

def tg_send(text: str):
    """发送 Telegram 消息"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"TG 发送失败: {e}")


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def read_cache(filename: str) -> dict | list | None:
    """读取本地 JSON 缓存（与 premarket_planner 共用同一目录）"""
    path = os.path.join(CACHE_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def write_cache(filename: str, data):
    """写入本地 JSON 缓存"""
    _ensure_cache_dir()
    path = os.path.join(CACHE_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_latest_news(symbol: str) -> str:
    """通过 Tavily 拉取最新资讯"""
    if not TAVILY_API_KEY:
        return "未配置 Tavily API，暂无资讯。"
    try:
        res = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": f"{symbol} stock latest news today financial",
                "search_depth": "basic",
                "include_answer": True,
                "max_results": 3,
            },
            timeout=10,
        )
        if res.status_code == 200:
            data = res.json()
            text = f"【AI 总结】: {data.get('answer', '无')}\n"
            for idx, r in enumerate(data.get("results", [])):
                text += f"{idx+1}. {r.get('title')}\n"
            return text
        return "资讯获取失败。"
    except Exception as e:
        return f"资讯拉取异常: {e}"


# ===========================================================================
# ⏰ 市场状态与交易日判断
# ===========================================================================

_HK_TZ = "Asia/Hong_Kong"
_US_TZ = "America/New_York"

# 交易时段定义（每个元素: ((start_h, start_m), (end_h, end_m))）
_HK_SESSIONS = [((9, 30), (12, 0)), ((13, 0), (16, 0))]   # 港股 上午+下午
_US_SESSIONS = [((9, 30), (16, 0)), ((16, 0), (20, 0))]   # 美股 盘中 + 盘后

# 交易日缓存（每日只查询一次 API）
_trading_day_cache: dict = {"date": None, "hk": None, "us": None}


def _check_trading_day(market: str) -> bool:
    """判断今天是否为指定市场的交易日，结果按日缓存。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    cache_key = market.lower()

    if _trading_day_cache["date"] == today_str and _trading_day_cache[cache_key] is not None:
        return _trading_day_cache[cache_key]

    try:
        resp = _logic_get_trading_days(market)
        is_trading = False
        if isinstance(resp, dict) and "is_today_trading_day" in resp:
            is_trading = resp["is_today_trading_day"]

        _trading_day_cache["date"] = today_str
        _trading_day_cache[cache_key] = is_trading
        return is_trading
    except Exception as e:
        print(f"⚠️ 交易日查询异常 ({market}): {e}，默认视为交易日")
        return True


def _is_in_session(tz_name: str, sessions: list) -> bool:
    """判断当前时间是否在指定时区的交易时段内。"""
    now = datetime.now(pytz.timezone(tz_name))
    current_mins = now.hour * 60 + now.minute
    for (sh, sm), (eh, em) in sessions:
        if sh * 60 + sm <= current_mins <= eh * 60 + em:
            return True
    return False


def _seconds_to_next_session(tz_name: str, sessions: list) -> int:
    """
    计算距下一个交易时段开始的秒数。
    如果今日所有时段已结束，返回 0（让调用方回退到默认休眠）。
    """
    now = datetime.now(pytz.timezone(tz_name))
    current_mins = now.hour * 60 + now.minute
    for (sh, sm), (eh, em) in sessions:
        start_mins = sh * 60 + sm
        if current_mins < start_mins:
            return (start_mins - current_mins) * 60
    return 0  # 今日所有时段已结束


def _get_market_status() -> tuple[bool, bool, int]:
    """
    智能市场状态检测：基于 trading_days API + 精确交易时段判断。

    返回: (hk_active, us_active, sleep_seconds)
      - 任意市场活跃 → sleep_seconds = 0
      - 无市场活跃 → sleep_seconds = 距下一个时段的秒数
    """
    hk_trading = _check_trading_day("HK")
    us_trading = _check_trading_day("US")

    hk_active = hk_trading and _is_in_session(_HK_TZ, _HK_SESSIONS)
    us_active = us_trading and _is_in_session(_US_TZ, _US_SESSIONS)

    if hk_active or us_active:
        return hk_active, us_active, 0

    # 计算距下一个交易时段的秒数
    candidates = []
    if hk_trading:
        s = _seconds_to_next_session(_HK_TZ, _HK_SESSIONS)
        if s > 0:
            candidates.append(s)
    if us_trading:
        s = _seconds_to_next_session(_US_TZ, _US_SESSIONS)
        if s > 0:
            candidates.append(s)

    if candidates:
        return False, False, min(candidates)

    # 两个市场今日均不开市或已收盘，1小时后重新检查
    return False, False, 3600


# ===========================================================================
# 📡 数据采集层（每个函数只做一件事）
# ===========================================================================

def fetch_account_status(symbol: str) -> tuple[str, str, float]:
    """
    获取账户购买力与指定标的持仓。
    返回: (money_str, pos_str, buying_power)
    """
    try:
        asset = get_account_asset()
        buying_power = float(asset.get("buy_power", 0.0))
        cash_info = asset.get("cash_info", {})
        positions = asset.get("positions", [])

        target_currency = "USD" if symbol.upper().endswith(".US") else "HKD"
        cash_val = cash_info.get(target_currency, "0")

        real_qty, real_cost = 0, 0.0
        for p in positions:
            p_sym = p.get("symbol", "")
            if p_sym == symbol or p_sym.lstrip('0') == symbol.lstrip('0'):
                real_qty = int(float(p.get("available_qty", 0)))
                real_cost = float(p.get("cost_price", 0.0))

        pos_str = f"已持仓 {real_qty} 股 (成本价 ${real_cost:.2f})" if real_qty > 0 else "0 股 (空仓)"
        money_str = f"💵 可用现金({target_currency}): ${cash_val} | 💳 最大购买力: ${buying_power:.2f}"
        return money_str, pos_str, buying_power
    except Exception as e:
        print(f"⚠️ 账户状态获取失败: {e}")
        return "0.00 (获取异常，限制买入)", "获取异常", 0.0


# ===========================================================================
# 📁 静态信息 / 估值指标 / 市场温度（含阶段性缓存）
# ===========================================================================

def fetch_static_info(symbol: str) -> str:
    """
    获取标的基本静态信息（名称/板块/货币/手数/总股本）—— 永久缓存。
    与 premarket_planner 共用 cache/static_info_<symbol>.json。
    """
    cache_file = f"static_info_{symbol.replace('.', '_')}.json"
    cached = read_cache(cache_file)

    if cached:
        static = cached
    else:
        static = _logic_get_static_info(symbol)
        if static and "error" not in static:
            write_cache(cache_file, static)

    if not static or "error" in static:
        return f"标的基本信息获取失败: {static.get('error', 'Unknown')}"

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


def fetch_market_temperature(symbol: str) -> str:
    """获取当前市场温度指数（根据标的后缀自动判断港/美股）"""
    market = "US" if symbol.upper().endswith(".US") else "HK"
    try:
        t = _logic_get_market_temperature(market)
        if "error" in t:
            return f"市场温度获取失败: {t['error']}"
        return (
            f"🌡️ 温度: {t.get('temp', 'N/A')} | "
            f"{t.get('desc', '')} | "
            f"估值: {t.get('val', 'N/A')} | "
            f"情绪: {t.get('sent', 'N/A')}"
        )
    except Exception as e:
        return f"市场温度获取异常: {e}"


def fetch_capital_flow(symbol: str) -> str:
    """获取当日资金流向与分布信息，返回可读字符串。"""
    try:
        cap = _logic_get_capital_distribution(symbol)
        if "error" not in cap:
            net_large  = cap["in_large"]  - cap["out_large"]
            net_medium = cap.get("in_medium", 0) - cap.get("out_medium", 0)
            net_small  = cap["in_small"]  - cap["out_small"]
            net_total  = net_large + net_medium + net_small
            sentiment = "🟢 主力净流入" if net_large > 0 else "🔴 主力净流出"
            return (
                f"{sentiment} | 合计净流: {net_total:+.0f}\n"
                f"    🟡 主力(大单): 流入 {cap['in_large']:.0f} / 流出 {cap['out_large']:.0f} = 净 {net_large:+.0f}\n"
                f"    🟠 中单:       流入 {cap.get('in_medium', 0):.0f} / 流出 {cap.get('out_medium', 0):.0f} = 净 {net_medium:+.0f}\n"
                f"    ⚪️ 散户(小单): 流入 {cap['in_small']:.0f} / 流出 {cap['out_small']:.0f} = 净 {net_small:+.0f}"
            )
    except Exception:
        pass
    return "资金流向拉取失败"


def fetch_kline_data(symbol: str, current_price: float) -> tuple[str, float]:
    """
    获取当天全量 5 分钟 K 线。
    返回: (today_k_str, price_change_pct)
    """
    try:
        today_date = datetime.now().date()
        k_lines = _logic_get_history_kline(symbol, Period.Min_5, today_date, today_date)

        if k_lines and "error" not in k_lines[0]:
            day_open = k_lines[0]["o"]
            price_change_pct = abs((current_price - day_open) / day_open) * 100
            k_data_list = [
                f"{k['t']} | O:{k['o']} H:{k['h']} L:{k['l']} C:{k['c']} V:{k['v']}"
                for k in k_lines
            ]
            return "\n".join(k_data_list), price_change_pct

        return "今日暂无 K 线数据（可能刚开盘）", 0.0
    except Exception as e:
        return f"当日K线拉取失败: {e}", 0.0


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


def fetch_option_snapshot(symbol: str, current_price: float) -> str:
    """
    期权异动探针：智能选期（本周末 + 两周后 + 四周后）→ 提取 ATM 合约 → 查 IV/OI（含 YahooQuery 降级）。
    输入 symbol 使用长桥格式，内部自动转换为富途格式。
    返回可读字符串。
    """
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
        two_weeks_out  = today + timedelta(days=14)
        four_weeks_out = today + timedelta(days=28)

        def pick_closest(dates, target):
            return min(dates, key=lambda d: abs((d - target).days))

        near_date       = pick_closest(future_dates, this_friday)
        two_week_date   = pick_closest(future_dates, two_weeks_out)
        four_week_date  = pick_closest(future_dates, four_weeks_out)

        # 去重：三个目标日期可能重合
        seen = set()
        targets = []
        for d, label in [(near_date, "本周末"), (two_week_date, "两周后"), (four_week_date, "四周后")]:
            if d not in seen:
                seen.add(d)
                targets.append((d, label))

        target_symbols: list[str] = []
        date_labels: dict[str, str] = {}

        for exp_date, label in targets:
            exp_str = exp_date.strftime("%Y-%m-%d")
            chain = _logic_get_option_chain(futu_code, exp_str)
            if not isinstance(chain, list) or not chain:
                continue

            # 富途链每条有 option_type(“CALL”/“PUT”) + strike_price + futu_code
            calls = [c for c in chain if "CALL" in str(c.get("option_type", "")).upper()]
            puts  = [c for c in chain if "PUT"  in str(c.get("option_type", "")).upper()]

            for contracts, direction in [(calls, "真 Call"), (puts, "真 Put")]:
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


# ===========================================================================
# 📝 消息构建层
# ===========================================================================

def build_wake_message(
    symbol: str,
    current_price: float,
    zone_name: str,
    trigger_price: float,
    macro_thesis: str,
    money_str: str,
    pos_str: str,
    static_str: str,
    financials_str: str,
    temperature_str: str,
    flow_str: str,
    opt_str: str,
    today_k_str: str,
    news_info: str,
    special_event_msg: str = "",
) -> str:
    """将所有盘面数据组装成发给 AI 的完整唤醒提示词"""
    event_header = f"🔔 **【底层系统事件注入】**: {special_event_msg}\n\n" if special_event_msg else ""

    return event_header + (
        f"🚨 **【突发盘面裁决警报】**：{symbol} 现价 ${current_price}，"
        f"已触击网格【{zone_name}】(触发价 ${trigger_price})！\n\n"

        f"🏦 **【当前账户真实火力状态】** (⚠️ 裁决前必须过资金这关！)：\n"
        f"   - 💵 {money_str}\n"
        f"   - 📦 {pos_str}\n\n"

        f"🧠 **【你的盘前宏观记忆 (大局观锚点)】**：\n"
        f"> {macro_thesis}\n\n"

        f"ℹ️ **【标的基本信息】**：\n{static_str}\n\n"

        f"📊 **【估值指标】**：{financials_str}\n\n"

        f"🌡️ **【市场温度】**：{temperature_str}\n\n"

        f"📊 **底层实时盘面全家桶**：\n"
        f"   - 💰 资金面：{flow_str}\n"
        f"   - 🛡️ 衍生品：{opt_str}\n"

        f"📈 **【今日 5 分钟微观全息数据 (寻觅量价信号)】**：\n"
        f"```\n{today_k_str}\n```\n\n"

        f"📰 **【此时此刻最新突发资讯 (黑天鹅排雷)】**：\n"
        f"{news_info}\n\n"

        f"⚠️ **【最高执行指令 (纯文本输出契约)】**：\n"
        f"你现在的唯一任务是进行下单前的最终风控！**绝对禁止尝试调用任何外部工具！**"
        f" 结合上方所有信息，严格按照以下两部分格式直接在回复中输出（底层系统会自动正则提取并执行）：\n\n"

        f"**步骤一：动作裁决 (Action Hook)**\n"
        f"判断突发新闻和微观 K 线是否破坏了你的【盘前宏观记忆】逻辑？资金是否够用？\n"
        f"👉 如果逻辑成立、排雷通过且决定交易：请在一行内输出动作指令。"
        f"格式严格为：`[ACTION: <BUY或SELL>, QTY: <计算出的股数>, PRICE: <挂单价>, REASON: <核心理由>]`\n"
        f"   *(⚠️ 注意：加仓抄底用 BUY，触及止盈/止损线用 SELL！)*\n"
        f"👉 如果发现基本面突变、量价形态恶劣或资金不足：果断放弃，"
        f"输出如：`[ACTION: HOLD, REASON: 发现资金大幅流出，放弃原定抄底计划]`\n\n"

        f"**步骤二：网格重铸 (JSON Update)**\n"
        f"无论是否交易，都必须重新划定该标的的价格区间，并在回复最末尾更新网格。\n"
        f"🛑 **红线警告**：必须且只能使用 Markdown 的 ` ```json ... ``` ` 代码块！"
        f"你**只准**输出 {symbol} 这一只标的的局部 JSON (务必保留 status 和 macro_thesis 字段)。"
    )


# ===========================================================================
# 🤖 AI 交互层
# ===========================================================================

def call_ai(wake_msg: str) -> str | None:
    """
    将唤醒消息发送给本地 OpenClaw，返回 AI 回复文本，失败返回 None。
    """
    try:
        res = requests.post(
            OPENCLAW_URL,
            headers=OPENCLAW_HEADERS,
            json={
                "model": "openclaw/default",
                "messages": [
                    {"role": "system", "content": INTRADAY_SENTRY_PROMPT},
                    {"role": "user",   "content": wake_msg},
                ],
            },
            timeout=180,
        )
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"]
        tg_send(f"❌ 后台唤醒 AI 失败: HTTP {res.status_code}")
        return None
    except Exception as e:
        tg_send(f"❌ 后台唤醒 AI 超时或异常: {e}")
        return None


def handle_ai_verdict(symbol: str, ai_reply: str):
    """
    解析 AI 回复：
    1. 正则提取 [ACTION:...] 指令并执行下单
    2. 提取 ```json...``` 更新本地网格文件（含冷却时间写入）
    3. 将 AI 原始分析报告推送到 TG
    """
    # 用于捕获真实的订单ID
    generated_order_id = None

    # 1. 解析行动指令
    action_match = re.search(
        r'\[ACTION:\s*(BUY|SELL|HOLD)(?:,\s*QTY:\s*(\d+))?(?:,\s*PRICE:\s*([\d\.]+))?(?:,\s*REASON:\s*(.*?))?\]',
        ai_reply,
    )
    if action_match:
        action_type = action_match.group(1)
        if action_type in ("BUY", "SELL"):
            qty  = int(action_match.group(2))
            price = float(action_match.group(3))
            reason = action_match.group(4) or "无理由"
            print(f"🔥 AI 裁决下达: {action_type} {symbol} | 数量: {qty} | 价格: {price}")
            order_res = submit_trade_order(
                symbol=symbol,
                side="Buy" if action_type == "BUY" else "Sell",
                quantity=qty,
                price=price,
                reason=reason,
            )
            if isinstance(order_res, dict) and "order_id" in order_res:
                generated_order_id = order_res["order_id"]

    # 2. 更新本地网格文件
    json_match = re.search(r'```json\s*(.*?)\s*```', ai_reply, re.DOTALL)
    if json_match:
        try:
            new_grid_data = json.loads(json_match.group(1))
            with open(PLAN_FILE, "r", encoding="utf-8") as f:
                latest_plan = json.load(f)

            traded = action_match and action_match.group(1) in ("BUY", "SELL")
            cooldown_min = 5 if traded else 30
            dt_now = datetime.now()
            cooldown_until = (dt_now + timedelta(minutes=cooldown_min)).strftime("%Y-%m-%d %H:%M:%S")

            # 格式化当前时间字符串
            time_str = dt_now.strftime("%Y-%m-%d %H:%M:%S")

            if symbol in new_grid_data:
                latest_plan[symbol] = new_grid_data[symbol]
                latest_plan[symbol]["cooldown_until"] = cooldown_until
                # 注入或覆盖更新时间
                latest_plan[symbol]["update_time"] = time_str

                # 如果刚刚下单成功了，将这把锁烙印进 JSON
                if traded and generated_order_id:
                    latest_plan[symbol]["pending_order"] = {
                        "id": generated_order_id,
                        "time": time_str
                    }

            with open(PLAN_FILE, "w", encoding="utf-8") as f:
                json.dump(latest_plan, f, ensure_ascii=False, indent=4)
        except json.JSONDecodeError as e:
            print(f"❌ JSON 格式损坏: {e}")

    # 3. 推送 AI 报告
    tg_send(f"🧠 **【🦞 自动裁决报告】**\n{ai_reply}")


# ===========================================================================
# 🎯 核心触发层
# ===========================================================================

def process_zone_hit(symbol: str, data: dict, current_price: float, zone_name: str, zone_info: dict, special_event_msg: str = ""):
    """
    网格触线后的完整处理流程：
    收集盘面数据 → 构建唤醒词 → 调用 AI → 执行裁决
    """
    trigger_price = float(zone_info["price"])
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 💥 捕获 {symbol} 异动，正在提取记忆与当日盘面...")

    # 各维度数据采集（独立函数，各自 try/except）
    macro_thesis              = data.get("macro_thesis", "未配置盘前宏观剧本，请仅依赖短期盘面判断。")
    money_str, pos_str, _    = fetch_account_status(symbol)
    static_str               = fetch_static_info(symbol)
    financials_str           = fetch_financial_indexes(symbol)
    temperature_str          = fetch_market_temperature(symbol)
    flow_str                  = fetch_capital_flow(symbol)
    today_k_str, change_pct  = fetch_kline_data(symbol, current_price)
    opt_str                   = fetch_option_snapshot(symbol, current_price)

    # 条件触发新闻（波动超 2% 才调用，节省 Tavily API 配额；网格触线已是异动，无需再判断价格方向）
    if change_pct >= 2.0:
        print(f"🚨 {symbol} 日内波动 {change_pct:.1f}% 达标，触发 Tavily 新闻搜索...")
        news_info = fetch_latest_news(symbol)
    else:
        news_info = "☑️ 日内未见异常基本面消息，纯技术面博弈。"

    # 构建唤醒词
    wake_msg = build_wake_message(
        symbol=symbol,
        current_price=current_price,
        zone_name=zone_name,
        trigger_price=trigger_price,
        macro_thesis=macro_thesis,
        money_str=money_str,
        pos_str=pos_str,
        static_str=static_str,
        financials_str=financials_str,
        temperature_str=temperature_str,
        flow_str=flow_str,
        opt_str=opt_str,
        today_k_str=today_k_str,
        news_info=news_info,
        special_event_msg=special_event_msg,
    )

    # 通知老板已接管
    tg_send(f"🤖 **【L5 全自动接管】**\n哨兵已捕获 {symbol} 警报，正在后台隐形唤醒 🦞 进行裁决...")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 💥 自动接管触发: {symbol}")

    # 调用 AI 并处理裁决
    ai_reply = call_ai(wake_msg)
    if ai_reply:
        handle_ai_verdict(symbol, ai_reply)

    # 防洪闸：处理完一只雷区股票强制冷却 60 秒
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 为防止 AI 接口限流，哨兵进入 60 秒战术冷却...")
    time.sleep(60)


# ===========================================================================
# 🛡️ 主循环辅助函数
# ===========================================================================

# 长桥 SDK 返回的订单状态枚举字符串精确值（通过 str(order.status) 取得）
# 将撤单中的状态单独拎出来，防止超时逻辑再触发一次重复撤单请求
_CANCELING_STATUSES = {"WaitToCancel", "PendingCancel"}

_PENDING_STATUSES = {
    "NotReported", "ReplacedNotReported", "ProtectedNotReported", "VarietiesNotReported",
    "WaitToNew", "New", "Submitted", "WaitToReplace", "PendingReplace",
    "Replaced", "PartialFilled"
}
_FILLED_STATUS = "Filled"
# 注意：PartialWithdrawal (部分撤单) 属于终结态，剩下的单子不会再成交了，必须抛给 AI 重新裁决
_CANCELED_STATUSES = {"Canceled", "Rejected", "Expired", "PartialWithdrawal"}


def _load_plan() -> dict | None:
    """
    读取并校验 daily_trading_plan.json。
    文件不存在或为空时返回 None，调用方直接 sleep 跳过本轮。
    """
    if not os.path.exists(PLAN_FILE):
        return None
    with open(PLAN_FILE, "r", encoding="utf-8") as f:
        plan = json.load(f)
    return plan if plan else None


def _fetch_live_prices(plan: dict) -> dict[str, float]:
    """
    批量拉取 plan 中所有标的的实时价格。
    拉取失败的标的不会出现在返回 dict 中，调用方 continue 跳过即可。
    """
    live_prices: dict[str, float] = {}
    for sym in plan.keys():
        result = _logic_get_live_quote(sym)
        if "error" not in result:
            live_prices[sym] = result["price"]
    return live_prices


def _check_pending_order(symbol: str, data: dict, plan: dict) -> tuple[bool, str, bool]:
    """
    订单状态机：检查 data 中是否有 pending_order，并根据最新状态触发撤单/成交/失效处理。

    ⚠️  data 必须是 plan[symbol] 的直接引用（非 deepcopy），
        这样 del data["pending_order"] 才能同步修改 plan，后续 JSON 持久化才正确。

    返回: (force_wakeup, special_event_msg, should_skip)
      - force_wakeup    : True 表示需要无视冷却直接唤醒 AI
      - special_event_msg : 注入给 AI 的系统事件描述
      - should_skip     : True 表示该标的本轮应跳过后续所有判定（挂单等待中）
    """
    pending_order = data.get("pending_order", {})
    if not pending_order:
        return False, "", False

    order_id      = pending_order.get("id")
    order_time_str = pending_order.get("time")
    order_status  = get_order_status_by_id(order_id)


    force_wakeup = False
    special_event_msg = ""

    # 1：卫语句提前拦截网络/探针异常
    if order_status in ["Unknown", "Error"]:
        print(f"⚠️ [{datetime.now().strftime('%H:%M:%S')}] {symbol} 挂单 [{order_id}] 状态查询异常 ({order_status})，网络重试中...")
        return False, "", True  # should_skip = True, 保持上锁状态，等待下次轮询

    # 2. 核心状态机：处理已知的合法业务状态
    if order_status in _CANCELING_STATUSES:
        print(f"⏳ {symbol} 撤单请求已在处理中，等待交易所响应...")
        return False, "", True  # should_skip，等下一轮确认

    elif order_status in _PENDING_STATUSES:
        # RISK-4：order_time_str 可能因 JSON 手动编辑而缺失，加守卫防 strptime 崩溃
        if not order_time_str:
            print(f"⚠️ {symbol} 的 pending_order 缺少 time 字段，跳过超时检查，等待下次轮询")
            return False, "", True  # should_skip

        order_time = datetime.strptime(order_time_str, "%Y-%m-%d %H:%M:%S")
        if datetime.now() - order_time > timedelta(minutes=15):
            # 挂单超时 → 战术撤单 + 强制唤醒
            print(f"⏳ [{datetime.now().strftime('%H:%M:%S')}] {symbol} 挂单 [{order_id}] 超时15分钟未成交，执行战术撤单！")
            cancel_result = cancel_order_by_id(order_id)

            if cancel_result and "error" in cancel_result:
                tg_send(f"🆘 **【撤单失败警报】**\n标的: {symbol}\n原因: {cancel_result['error']}\n⚠️ 请手动处理！")
                return False, "", True  # 保持锁定，等下一轮重试

            tg_send(f"⚠️ **【战术撤单】**\n标的: {symbol}\n原因: 挂单超时未成交，已由哨兵自动撤单以释放资金。")
            force_wakeup = True
            special_event_msg = "⚠️ 【系统强制事件：超时撤单】你之前的挂单因价格偏离已超时被系统撤销。资金已释放，请根据当前最新盘面重新进行动作裁决！"
            del data["pending_order"]
        else:
            # 挂单未超时 → 继续等待撮合
            print(f"⏳ {symbol} 存在未成交挂单，等待撮合...")
            return False, "", True  # should_skip

    elif order_status == _FILLED_STATUS:
        # 完全成交 → 强制唤醒重铸网格
        print(f"✅ {symbol} 挂单 [{order_id}] 已完全成交！")
        tg_send(f"🎉 **【订单成交捷报】**\n标的: {symbol}\n状态: 完全成交 (Filled)\n下一步: 正在唤醒大脑重铸网格...")
        force_wakeup = True
        special_event_msg = "🎉 【系统强制事件：订单成交】你上一笔订单已完全成交！当前底牌已变，请根据最新资金和仓位更新 status 并重铸网格！⚠️警告：本次唤醒纯为了更新网格以配合新仓位。你必须严格输出 [ACTION: HOLD, REASON: 订单成交重铸网格]，绝对禁止在成交后立刻再次下单！"
        del data["pending_order"]

    elif order_status in _CANCELED_STATUSES:
        # 订单失效 → 强制唤醒重铸网格
        force_wakeup = True
        special_event_msg = f"⚠️ 【系统强制事件：订单失效】你之前的挂单已失效 (状态: {order_status})。资金已释放，请重新审视盘面！"
        del data["pending_order"]

    # 3：终极兜底未知“暗物质”状态
    else:
        msg = f"🆘 **【未知订单状态告警】**\n标的: {symbol}\n状态: {order_status}\n⚠️ 请立即核对长桥文档！"
        print(msg)
        tg_send(msg)
        return False, "", True  # should_skip = True, 不敢乱动，锁住等人类介入

    # 发生了状态流转（锁被拆掉）→ 立即持久化，清除硬盘上的 pending_order 痕迹
    if "pending_order" not in data:
        with open(PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=4)

    return force_wakeup, special_event_msg, False


def _check_zone_hit(data: dict, current_price: float) -> tuple[bool, str, dict]:
    """
    网格触线判定：遍历 data["zones"]，找到第一个满足触发条件的区间。

    返回: (hit, zone_name, zone_info)
      - hit       : 是否命中任何网格
      - zone_name : 命中的网格名称（未命中时为空字符串）
      - zone_info : 命中的网格详情 dict（未命中时为空 dict）
    """
    for z_name, z_info in list(data.get("zones", {}).items()):
        trigger_price = float(z_info["price"])
        cond = z_info["condition"]
        if (cond == "<=" and current_price <= trigger_price) or \
           (cond == ">=" and current_price >= trigger_price):
            return True, z_name, z_info
    return False, "", {}


# ===========================================================================
# 🛡️ 主循环
# ===========================================================================

def run_sentry():
    """哨兵主循环：基于交易日 API + 精确时段判断，开盘时 5 分钟轮询网格触线"""
    print("🛡️ 底层高频哨兵已启动，0 Token 耗损盯盘中...")
    while True:
        try:
            # 1. 市场状态检测（基于 trading_days API + 精确交易时段）
            hk_active, us_active, sleep_seconds = _get_market_status()

            if sleep_seconds > 0:
                hrs, remainder = divmod(sleep_seconds, 3600)
                mins = remainder // 60
                status_parts = []
                if not _check_trading_day("HK"):
                    status_parts.append("港股今日休市")
                if not _check_trading_day("US"):
                    status_parts.append("美股今日休市")
                status_info = " | ".join(status_parts) if status_parts else "非交易时段"
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 💤 {status_info}，休眠 {hrs}h{mins}m 后重新检查...")
                close_contexts()        # 释放长桥 WebSocket：宿眠前断开长连接
                close_futu_context()    # 释放富途 OpenD：同步回收连接配额
                time.sleep(sleep_seconds)
                continue

            active_markets = []
            if hk_active: active_markets.append("🇭🇰港")
            if us_active: active_markets.append("🇺🇸美")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🟢 {'/'.join(active_markets)}股盘中，执行 5 分钟例行扫描...")

            # 2. 读取作战计划
            plan = _load_plan()
            if plan is None:
                time.sleep(POLL_INTERVAL)
                continue

            # 3. 批量拉取实时价格（只拉当前活跃市场的标的）
            live_prices: dict[str, float] = {}
            for sym in plan.keys():
                # 按市场过滤：美股只在盘中扫描，港股只在港股开盘时扫描
                if sym.endswith(".HK") and not hk_active:
                    continue
                if sym.endswith(".US") and not us_active:
                    continue
                result = _logic_get_live_quote(sym)
                if "error" not in result:
                    live_prices[sym] = result["price"]

            # 4. 逐标的进行三阶段判定
            for symbol, data in plan.items():
                if symbol not in live_prices:
                    continue

                current_price = live_prices[symbol]

                # 阶段一：订单状态机（超时撤单 / 成交 / 失效）
                force_wakeup, special_event_msg, should_skip = _check_pending_order(symbol, data, plan)
                if should_skip:
                    continue

                # 阶段二：战术冷却检查（强制唤醒可绕过）
                cooldown_str = data.get("cooldown_until")
                if cooldown_str and not force_wakeup:
                    if datetime.now() < datetime.strptime(cooldown_str, "%Y-%m-%d %H:%M:%S"):
                        print(f"⏳ {symbol} 仍在战术冷却中，跳过本次判定。")
                        continue

                # 阶段三：网格触线判定
                hit, zone_name, zone_info = _check_zone_hit(data, current_price)

                # 命中网格 or 强制唤醒 → 启动完整业务流
                if hit or force_wakeup:
                    if not hit:
                        zone_name = "强制唤醒"
                        zone_info = {"price": current_price}
                    process_zone_hit(symbol, data, current_price, zone_name, zone_info, special_event_msg)

        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚨 哨兵监控遭遇异常: {e}")
            
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ♻️ 本轮巡视结束，休眠 5 分钟...\n")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_sentry()
