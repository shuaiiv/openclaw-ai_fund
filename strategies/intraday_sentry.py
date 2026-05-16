import json
import os
import sys
import time
import re
import requests
from datetime import datetime, timedelta
import pytz
from longbridge.openapi import Period, Market

# ==========================================================
# 📦 导入公共工具模块（路径设置、缓存、数据获取等均在 shared_utils 中初始化）
# ==========================================================
from shared_utils import (
    ROOT_DIR as _ROOT_DIR,
    PLAN_FILE, CACHE_DIR, OPENCLAW_URL, OPENCLAW_HEADERS,
    TAVILY_API_KEY,
    # 缓存读写
    read_cache, write_cache,
    # 交易计划读写
    load_plan, save_plan,
    # 标的代码转换
    lb_to_futu,
    # 基础数据获取
    fetch_static_info, fetch_financial_indexes, fetch_latest_news,
    # 期权数据
    fetch_option_snapshot,
    # AI 调用 (带重试 / 限流排队)
    call_ai_with_retry,
)

# ==========================================
# 📦 从 longbridge_server 导入哨兵专用函数
# 彻底解耦：此文件不再直接依赖 longbridge SDK 的上下文创建逻辑
# ==========================================
from longbridge_server import (
    get_account_asset,                  # 获取账户购买力与持仓
    _logic_get_live_quote,              # 获取单只股票实时行情（常规盘）
    _logic_get_extended_quote,          # 获取含盘前/盘后的最新价格
    _logic_get_capital_distribution,    # 获取主力资金分布
    _logic_get_history_kline,           # 获取历史 K 线 (返回 dict 列表)
    _logic_get_market_temperature,      # 获取市场温度
    submit_trade_order,                 # 下单 (内置 TG 通知)
    cancel_order_by_id,                 # 撤单
    get_order_status_by_id,             # 查询订单状态
    _logic_get_today_orders_by_symbol,  # 查询今日标的关联订单
    _logic_get_trading_days,            # 交易日查询
    close_contexts,                     # 释放 WebSocket 连接
)

# 释放富途 OpenD 连接
from futu_options_server import close_context as close_futu_context

# 导入 TG 发送工具
from tg_sender import send_message_async


TG_BOT_TOKEN         = os.getenv("TG_BOT_TOKEN_CLAW")
TG_BOT_TOKEN_QUANT   = os.getenv("TG_BOT_TOKEN_QUANT")
TG_CHANNEL_ANALYSIS  = os.getenv("TG_CHANNEL_ID_ANALYSIS")
TG_CHANNEL_ORDER     = os.getenv("TG_CHANNEL_ID_ORDER")
TG_CHANNEL_ID        = TG_CHANNEL_ANALYSIS  # 向下兼容

POLL_INTERVAL = 120  # 2分钟轮询

# 🚨 系统提示词
PROMPT_SENTRY_FILE = os.path.join(_ROOT_DIR, "prompts", "intraday_sentry_prompt.md")
with open(PROMPT_SENTRY_FILE, "r", encoding="utf-8") as f:
    INTRADAY_SENTRY_PROMPT = f.read()

PROMPT_REBUILD_FILE = os.path.join(_ROOT_DIR, "prompts", "intraday_rebuild_prompt.md")
with open(PROMPT_REBUILD_FILE, "r", encoding="utf-8") as f:
    INTRADAY_REBUILD_PROMPT = f.read()


# ===========================================================================
# 🛠️ 哨兵专用工具函数
# ===========================================================================

def tg_analysis(text: str):
    """发往 Analysis 频道（AI报告、盘前分析、系统状态）"""
    targets = [(TG_BOT_TOKEN, TG_CHANNEL_ANALYSIS)] if TG_CHANNEL_ANALYSIS else []
    send_message_async(text, targets=targets)


def tg_order(text: str):
    """发往 Order 频道（订单成交/撒单/告警）"""
    targets = [(TG_BOT_TOKEN_QUANT, TG_CHANNEL_ORDER)] if TG_CHANNEL_ORDER else []
    send_message_async(text, targets=targets)


def tg_send(text: str):
    """兼容旧代码：默认发往 Analysis 频道"""
    tg_analysis(text)


# fetch_latest_news 已移至 shared_utils


# ===========================================================================
# ⏰ 市场状态与交易日判断
# ===========================================================================

_HK_TZ = "Asia/Hong_Kong"
_US_TZ = "America/New_York"

# 交易时段定义（每个元素: ((start_h, start_m), (end_h, end_m))）
_HK_SESSIONS = [((9, 30), (12, 0)), ((13, 0), (16, 0))]   # 港股 上午+下午
_US_REGULAR_SESSIONS    = [((9, 30), (15, 59))]             # 美股 盘中(09:30-15:59)
_US_AFTERHOURS_SESSIONS = [((16, 0), (20, 0))]             # 美股 盘后(16:00-20:00)
_US_SESSIONS = _US_REGULAR_SESSIONS + _US_AFTERHOURS_SESSIONS  # 合并（用于休眠计算）

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


def _get_market_status() -> tuple[bool, bool, bool, int]:
    """
    智能市场状态检测：基于 trading_days API + 精确交易时段判断。

    返回: (hk_active, us_active, us_after_hours, sleep_seconds)
      - 任意市场活跃 → sleep_seconds = 0
      - us_after_hours: True 表示美股当前处于盘后时段（需用 extended_quote 获取实时价）
      - 无市场活跃 → sleep_seconds = 距下一个时段的秒数
    """
    hk_trading = _check_trading_day("HK")
    us_trading = _check_trading_day("US")

    hk_active = hk_trading and _is_in_session(_HK_TZ, _HK_SESSIONS)
    us_active = us_trading and _is_in_session(_US_TZ, _US_SESSIONS)
    us_after_hours = us_active and _is_in_session(_US_TZ, _US_AFTERHOURS_SESSIONS)

    if hk_active or us_active:
        return hk_active, us_active, us_after_hours, 0

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
        return False, False, False, min(candidates)

    # 两个市场今日均不开市或已收盘，1小时后重新检查
    return False, False, False, 3600


# ===========================================================================
# 📡 数据采集层（每个函数只做一件事）
# ===========================================================================

def fetch_account_status(symbol: str) -> tuple[str, str, float, str]:
    """
    获取账户购买力与持仓信息，计算同市场仓位占比。

    返回: (money_str, pos_str, buying_power, all_positions_str)
      - money_str        : 当前市场货币的现金与购买力一行摘要
      - pos_str          : 当前标的持仓简述（空仓/已持仓 N 股）
      - buying_power     : 最大购买力浮点数（用于资金判断）
      - all_positions_str: 含已算好的仓位占比，供 AI 直接参考

    仓位占比公式：当前标的市值 ÷ (同市场所有持仓市值合计 + 同市场现金余额)
    分母用"现金"，不含融资授信，反映真实风险暴露。
    """
    try:
        asset = get_account_asset()
        buying_power = float(asset.get("buy_power", 0.0))
        cash_info = asset.get("cash_info", {})
        positions = asset.get("positions", [])

        target_currency = "USD" if symbol.upper().endswith(".US") else "HKD"
        target_market   = "US"  if symbol.upper().endswith(".US") else "HK"
        cash_val_str    = cash_info.get(target_currency, "0")
        try:
            cash_val = float(str(cash_val_str).replace(",", ""))
        except (ValueError, TypeError):
            cash_val = 0.0

        # ── 当前标的持仓（精确查找） ──────────────────────────────────────
        real_qty, real_cost = 0, 0.0
        current_mkt_val = 0.0
        for p in positions:
            p_sym = p.get("symbol", "")
            if p_sym == symbol or p_sym.lstrip('0') == symbol.lstrip('0'):
                real_qty  = int(float(p.get("available_qty", 0)))
                real_cost = float(p.get("cost_price", 0.0))
                # 当前标的市值：优先用 market_value，fallback 用 qty×cost
                mkt_raw = p.get("market_value")
                try:
                    current_mkt_val = float(str(mkt_raw).replace(",", ""))
                except (ValueError, TypeError, AttributeError):
                    current_mkt_val = real_qty * real_cost

        pos_str   = f"已持仓 {real_qty} 股 (成本价 ${real_cost:.2f})" if real_qty > 0 else "0 股 (空仓)"
        money_str = f"💵 可用现金({target_currency}): ${cash_val_str} | 💳 最大购买力: ${buying_power:.2f}"

        # ── 同市场所有持仓及市值 ─────────────────────────────────────────
        same_mkt_positions = []
        for p in positions:
            sym = p.get("symbol", "")
            is_same = (target_market == "HK" and sym.endswith(".HK")) or \
                      (target_market == "US" and sym.endswith(".US"))
            if not is_same:
                continue
            mkt_raw = p.get("market_value")
            try:
                mval = float(str(mkt_raw).replace(",", ""))
            except (ValueError, TypeError, AttributeError):
                qty  = float(p.get("available_qty", 0) or 0)
                cost = float(p.get("cost_price", 0)   or 0)
                mval = qty * cost  # fallback
            same_mkt_positions.append({**p, "_mkt_val": mval})

        total_holding_val = sum(p["_mkt_val"] for p in same_mkt_positions)
        nav = total_holding_val + cash_val   # 同市场净资产（分母）
        using_estimate = any(
            p.get("market_value") in (None, "N/A", "") for p in positions
        )
        nav_note = "⚠️市值使用成本估算" if using_estimate else "实时市值"

        # ── 组装 all_positions_str ─────────────────────────────────────────
        # 行1：同市场净资产概览
        lines = [
            f"📊 {target_currency} 市场净资产: ${nav:,.0f}"
            f" = 持仓市值 ${total_holding_val:,.0f} + 现金 ${cash_val:,.0f}  [{nav_note}]",
        ]

        # 行2：计算公式红线提醒
        if real_qty > 0 and nav > 0:
            cur_ratio = current_mkt_val / nav * 100
            warn = " ⚠️接近55%上限" if cur_ratio >= 45 else (" 🚨已超55%上限！禁止买入！" if cur_ratio >= 55 else "")
            lines.append(
                f"🎯 {symbol} 实时仓位占比: {cur_ratio:.1f}%"
                f" ({target_currency}${current_mkt_val:,.0f} ÷ ${nav:,.0f}){warn}"
            )
        else:
            lines.append(f"🎯 {symbol} 当前空仓，占比 0%")

        # 行3：同市场所有持仓明细
        lines.append(f"{'─'*36}")
        lines.append("📦 同市场全部持仓:")
        if same_mkt_positions:
            for p in same_mkt_positions:
                sym   = p.get("symbol", "N/A")
                qty   = p.get("available_qty", 0)
                cost  = p.get("cost_price", 0)
                mval  = p["_mkt_val"]
                ratio = (mval / nav * 100) if nav > 0 else 0
                warn  = " ⚠️" if ratio >= 45 else (" 🚨" if ratio >= 55 else "")
                lines.append(
                    f"  {sym}: {qty}股 | 成本${cost} | 市值${mval:,.0f} | 占比{ratio:.1f}%{warn}"
                )
        else:
            lines.append(f"  {target_currency} 市场当前全部空仓")

        # 行4：其他市场现金快照（简要）
        lines.append(f"{'─'*36}")
        cash_summary = " | ".join([f"{cur}: ${val}" for cur, val in cash_info.items()])
        lines.append(f"💰 账户现金快照: {cash_summary}")

        all_positions_str = "\n".join(lines)
        return money_str, pos_str, buying_power, all_positions_str
    except Exception as e:
        print(f"⚠️ 账户状态获取失败: {e}")
        return "0.00 (获取异常，限制买入)", "获取异常", 0.0, "持仓总览获取失败"


# ===========================================================================
# 📁 静态信息 / 估值指标 / 市场温度（含阶段性缓存）
# ===========================================================================

# fetch_static_info, fetch_financial_indexes, lb_to_futu, fetch_option_snapshot 已移至 shared_utils


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


def fetch_today_orders(symbol: str) -> str:
    """获取今日标的所有相关订单状态"""
    try:
        orders = _logic_get_today_orders_by_symbol(symbol)
        if not orders:
            return "今日暂无该标的之相关订单。"
        lines = []
        for o in orders:
            lines.append(f"  - CODE:{o.get('CODE', 'N/A')} | 方向:{o.get('方向', 'N/A')} | 价格:{o.get('价格', 'N/A')} | 数量:{o.get('数量', 'N/A')} | 状态:{o.get('状态', 'N/A')} | 创建:{o.get('创建时间', 'N/A')} | 更新:{o.get('更新时间', 'N/A')}")
        return "\n".join(lines)
    except Exception as e:
        return f"查询今日相关订单异常: {e}"


def fetch_kline_data(symbol: str, current_price: float) -> tuple[str, float]:
    """
    获取当天全量 5 分钟 K 线（含盘前/盘后延伸时段）。
    对美股标的按美东时间拆分为：盘前(04:00-09:29) / 盘中(09:30-15:59) / 盘后(16:00-20:00)，
    分段标注后输出，让 AI 能清晰区分各时段走势。
    返回: (today_k_str, price_change_pct)
    """
    try:
        today_date = datetime.now().date()
        k_lines = _logic_get_history_kline(symbol, Period.Min_5, today_date, today_date)

        if not k_lines or "error" in k_lines[0]:
            return "今日暂无 K 线数据（可能刚开盘）", 0.0

        # 非美股：直接输出全量（港股无延伸时段）
        if not symbol.endswith(".US"):
            day_open = k_lines[0]["o"]
            price_change_pct = abs((current_price - day_open) / day_open) * 100
            k_data_list = [
                f"{k['t']} | O:{k['o']} H:{k['h']} L:{k['l']} C:{k['c']} V:{k['v']}"
                for k in k_lines
            ]
            return "\n".join(k_data_list), price_change_pct

        # 美股：按美东时间分段
        et = pytz.timezone("America/New_York")
        premarket, regular, afterhours = [], [], []

        for k in k_lines:
            try:
                t = datetime.strptime(k["t"], "%Y-%m-%d %H:%M")
                t_et = pytz.utc.localize(t).astimezone(et)
                h, m = t_et.hour, t_et.minute
                line = f"{t_et.strftime('%H:%M')} | O:{k['o']} H:{k['h']} L:{k['l']} C:{k['c']} V:{k['v']}"
                if h < 4:
                    pass  # 夜盘，忽略
                elif h < 9 or (h == 9 and m < 30):
                    premarket.append(line)
                elif h < 16:
                    regular.append(line)
                elif h < 20:
                    afterhours.append(line)
            except Exception:
                regular.append(
                    f"{k['t']} | O:{k['o']} H:{k['h']} L:{k['l']} C:{k['c']} V:{k['v']}"
                )

        # 涨跌幅以常规盘开盘价为基准（如果有），否则用首根 K 线
        day_open = k_lines[0]["o"]
        if regular:
            # 从第一根常规盘 K 线提取开盘价
            first_regular_k = None
            for k in k_lines:
                try:
                    t = datetime.strptime(k["t"], "%Y-%m-%d %H:%M")
                    t_et = pytz.utc.localize(t).astimezone(et)
                    if (t_et.hour == 9 and t_et.minute >= 30) or t_et.hour >= 10:
                        first_regular_k = k
                        break
                except Exception:
                    continue
            if first_regular_k:
                day_open = first_regular_k["o"]
        price_change_pct = abs((current_price - day_open) / day_open) * 100

        # 组装分段输出
        sections = []
        if premarket:
            sections.append(f"🌅 盘前 ({len(premarket)}条):")
            sections.extend(premarket)
        if regular:
            sections.append(f"📈 盘中 ({len(regular)}条):")
            sections.extend(regular)
        if afterhours:
            sections.append(f"🌙 盘后 ({len(afterhours)}条):")
            sections.extend(afterhours)

        return "\n".join(sections) if sections else "今日暂无 K 线数据", price_change_pct

    except Exception as e:
        return f"当日K线拉取失败: {e}", 0.0


# ===========================================================================
# 📡 公共数据采集层
# ===========================================================================

def _collect_market_data(symbol: str, current_price: float) -> dict:
    """
    公共数据采集层：一次性采集所有维度的盘面数据。
    网格触线流程和订单事件流程共享此函数，避免代码重复。
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 正在采集 {symbol} 全维度盘面数据...")

    money_str, pos_str, _, all_positions_str = fetch_account_status(symbol)
    static_str       = fetch_static_info(symbol)
    financials_str   = fetch_financial_indexes(symbol)
    temperature_str  = fetch_market_temperature(symbol)
    flow_str         = fetch_capital_flow(symbol)
    today_k_str, change_pct = fetch_kline_data(symbol, current_price)
    opt_str          = fetch_option_snapshot(symbol, current_price)
    today_orders_str = fetch_today_orders(symbol)
    premarket_memo   = read_cache(f"premarket_memo_{symbol.replace('.', '_')}.json")

    return {
        "money_str": money_str, "pos_str": pos_str,
        "all_positions_str": all_positions_str,
        "static_str": static_str, "financials_str": financials_str,
        "temperature_str": temperature_str, "flow_str": flow_str,
        "today_k_str": today_k_str, "change_pct": change_pct,
        "opt_str": opt_str, "today_orders_str": today_orders_str,
        "premarket_memo": premarket_memo,
    }


# ===========================================================================
# 📝 消息构建层
# ===========================================================================

def _build_premarket_review(pm: dict) -> str:
    """提取盘前缓存数据并格式化为回顾段落（两个消息构建器共用）"""
    return (
        f"📈 60日日K线与10分钟结构：\n{pm.get('daily_kline_str', '暂无记录')}\n\n"
        f"{pm.get('min_kline_str', '暂无记录')}\n\n"
        f"🌅 美股盘前5分钟动能：\n{pm.get('premarket_kline_str', '暂无记录')}\n\n"
        f"🌡️ 盘前历史温度与期权阵地：\n{pm.get('temp_str', '暂无记录')}\n\n"
        f"{pm.get('option_str', '暂无记录')}\n\n"
        f"📰 盘前核心资讯：\n{pm.get('news_str', '暂无记录')}"
    )


def _build_realtime_section(ctx: dict, news_info: str) -> str:
    """构建当日实时盘面数据段落（两个消息构建器共用）"""
    return (
        f"🌡️ 实时市场温度：{ctx['temperature_str']}\n\n"
        f"💰 实时主力资金分布：\n{ctx['flow_str']}\n\n"
        f"🛡️ 实时期权异动探针：\n{ctx['opt_str']}\n\n"
        f"🛒 今日相关订单：\n{ctx['today_orders_str']}\n\n"
        f"📈 **【今日 5 分钟微观全息数据】**：\n"
        f"```\n{ctx['today_k_str']}\n```\n\n"
        f"📰 **【此时此刻最新突发资讯】**：\n"
        f"{news_info}"
    )


def build_grid_trigger_message(
    symbol: str,
    current_price: float,
    zone_name: str,
    trigger_price: float,
    macro_thesis: str,
    ctx: dict,
    news_info: str,
) -> str:
    """将盘面数据组装为【网格触线裁决】专用唤醒提示词"""
    pm = ctx["premarket_memo"] or {}
    pm_review = _build_premarket_review(pm)
    realtime = _build_realtime_section(ctx, news_info)

    # 市场时区时间戳
    _mkt_tz = pytz.timezone("America/New_York") if symbol.endswith(".US") else pytz.timezone("Asia/Hong_Kong")
    mkt_now = datetime.now(_mkt_tz)
    time_header = f"⏰ 触发时间: {mkt_now.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    return (
        f"🚨 **【突发盘面裁决警报】**：{symbol} 现价 ${current_price}，"
        f"已触击网格【{zone_name}】(触发价 ${trigger_price})！\n"
        f"{time_header}\n\n"

        f"🏦 **【一、当前账户火力状态】**\n"
        f"   💵 {ctx['money_str']}\n"
        f"   📦 **{symbol} 当前持仓**: {ctx['pos_str']}\n\n"
        f"   **持仓总览** (⚠️ 评估 55% 仓位上限时必须参考)：\n"
        f"{ctx['all_positions_str']}\n\n"

        f"💭 **【你的盘前宏观记忆 (大局观锚点)】**：\n"
        f"> {macro_thesis}\n\n"

        f"📚 **【二、回顾盘前信息(战略底色)】**\n"
        f"ℹ️ 标的基本面：\n{ctx['static_str']}\n\n"
        f"📊 估值指标：\n{ctx['financials_str']}\n\n"
        f"{pm_review}\n\n"

        f"🚨 **【三、当日实时盘面(战术校准)】**\n"
        f"{realtime}\n\n"

        f"⚠️ **【要求】**：\n"
        f"你现在的唯一任务是进行最终风控并重构阵地！**绝对禁止尝试调用任何外部工具！**\n"
        f"请严格遵守 Prompt 中对应状态的指令输出格式！"
    )


def build_order_event_message(
    symbol: str,
    current_price: float,
    special_event_msg: str,
    macro_thesis: str,
    ctx: dict,
    news_info: str,
) -> str:
    """将盘面数据组装为【订单状态变更重构】专用唤醒提示词"""
    pm = ctx["premarket_memo"] or {}
    pm_review = _build_premarket_review(pm)
    realtime = _build_realtime_section(ctx, news_info)

    # 市场时区时间戳
    _mkt_tz = pytz.timezone("America/New_York") if symbol.endswith(".US") else pytz.timezone("Asia/Hong_Kong")
    mkt_now = datetime.now(_mkt_tz)
    time_header = f"⏰ 事件时间: {mkt_now.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    return (
        f"🔔 **【底层系统事件注入】**: {special_event_msg}\n"
        f"{time_header}\n\n"

        f"📐 **【订单状态变更 · 网格重构请求】**：{symbol} 现价 ${current_price}\n\n"

        f"🏦 **【一、最新账户状态】**\n"
        f"   💵 {ctx['money_str']}\n"
        f"   📦 **{symbol} 当前持仓**: {ctx['pos_str']}\n\n"
        f"   **持仓总览** (⚠️ 评估 55% 仓位上限时必须参考)：\n"
        f"{ctx['all_positions_str']}\n\n"

        f"💭 **【你的盘前宏观记忆 (大局观锚点)】**：\n"
        f"> {macro_thesis}\n\n"

        f"📚 **【二、回顾盘前信息(战略底色)】**\n"
        f"ℹ️ 标的基本面：\n{ctx['static_str']}\n\n"
        f"📊 估值指标：\n{ctx['financials_str']}\n\n"
        f"{pm_review}\n\n"

        f"🚨 **【三、当日实时盘面(战术校准)】**\n"
        f"{realtime}\n\n"

        f"⚠️ **【要求】**：\n"
        f"你现在的唯一任务是根据最新持仓与盘面重构交易网格！**绝对禁止尝试调用任何外部工具！**\n"
        f"请严格按照 Prompt 要求输出 JSON 网格，禁止输出任何交易指令！"
    )


# ===========================================================================
# 🤖 AI 交互层
# ===========================================================================

def call_ai(wake_msg: str, sys_prompt: str) -> str | None:
    """
    将唤醒消息发送给本地 OpenClaw，返回 AI 回复文本（内置 429 重试 + 限流排队），失败返回 None。
    """
    content, error = call_ai_with_retry(
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": wake_msg},
        ],
        max_tokens=8192,   # 盘中裁决输出空间
        timeout=180,
        caller_label="盘中哨兵",
    )
    if content:
        if error:
            # finish_reason=length 截断警告：内容已返回但不完整
            tg_send(f"⚠️ 盘中哨兵 AI 回复被截断: {error}")
        return content
    tg_send(f"❌ 后台唤醒 AI 失败: {error}")
    return None


def handle_ai_verdict(symbol: str, ai_reply: str, zone_name: str = "", current_price: float = 0.0):
    """
    解析【网格触线裁决】的 AI 回复：
    1. 正则提取 [ACTION:...] 指令并执行下单
    2. 提取 ```json...``` 更新本地网格文件（含冷却时间写入）
    3. 将 AI 裁决报告推送到 TG Analysis 频道
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
                if not traded:
                    # 如果未发生交易 (HOLD) 或属于盘中网格重构 (rebuild_prompt)，则全面吸收 AI 提供的网格
                    latest_plan[symbol] = new_grid_data[symbol]
                    latest_plan[symbol]["cooldown_until"] = cooldown_until
                    latest_plan[symbol]["update_time"] = time_str
                else:
                    # 如果发生了真实交易 (BUY/SELL)，为防止过早画网格，只更新冷却时间并写入挂单锁
                    latest_plan[symbol]["cooldown_until"] = cooldown_until
                    latest_plan[symbol]["update_time"] = time_str
                    if generated_order_id:
                        latest_plan[symbol]["pending_order"] = {
                            "id": generated_order_id,
                            "time": time_str
                        }

            with open(PLAN_FILE, "w", encoding="utf-8") as f:
                json.dump(latest_plan, f, ensure_ascii=False, indent=4)
        except json.JSONDecodeError as e:
            print(f"❌ JSON 格式损坏: {e}")

    tg_reply = ai_reply  # 默认 fallback
    try:
        with open(PLAN_FILE, "r", encoding="utf-8") as f:
            final_plan = json.load(f)
        # 只推送当前 symbol 的最新网格，避免把全部标的都塞进消息
        canonical_json_str = json.dumps(
            {symbol: final_plan[symbol]}, ensure_ascii=False, indent=2
        )
        # 将 ai_reply 中的原始 JSON 块整体替换为落盘后的权威数据
        tg_reply = re.sub(
            r'```json\s*.*?\s*```',
            f'```json\n{canonical_json_str}\n```',
            ai_reply,
            flags=re.DOTALL,
        )
    except Exception as e:
        # 降级：文件读取失败时原样推送，至少保证消息不丢失
        print(f"⚠️ 读取最终 PLAN_FILE 失败，降级推送原始 ai_reply: {e}")

    # ── 构建 TG 推送消息（含裁决标签与价格信息）──
    if action_match:
        action_type = action_match.group(1)
        reason_str = action_match.group(4) or ""
        if action_type == "BUY":
            verdict_header = f"🟢 **买入裁决** ┃ **{symbol}** ┃ ${current_price}"
        elif action_type == "SELL":
            verdict_header = f"🔴 **卖出裁决** ┃ **{symbol}** ┃ ${current_price}"
        else:
            verdict_header = f"⏸️ **观望裁决** ┃ **{symbol}** ┃ ${current_price}"
    else:
        verdict_header = f"🦞 **自动裁决** ┃ **{symbol}** ┃ ${current_price}"
        reason_str = ""

    tg_parts = [verdict_header]
    if zone_name:
        tg_parts[0] += f" → {zone_name}"
    tg_parts.append("━━━━━━━━━━━━━━━━━━━━━")
    if reason_str:
        tg_parts.append(f"📊 理由: {reason_str}")
        tg_parts.append("━━━━━━━━━━━━━━━━━━━━━")
    tg_parts.append(tg_reply)

    tg_analysis("\n".join(tg_parts))


def handle_rebuild_result(symbol: str, ai_reply: str):
    """
    解析【订单状态变更重构】的 AI 回复：
    1. 提取 ```json...``` 全面吸收为新网格（不解析 ACTION 指令）
    2. 将 AI 重构报告推送到 TG Analysis 频道
    """
    # 1. 更新本地网格文件（全面吸收 AI 新网格）
    json_match = re.search(r'```json\s*(.*?)\s*```', ai_reply, re.DOTALL)
    if json_match:
        try:
            new_grid_data = json.loads(json_match.group(1))
            with open(PLAN_FILE, "r", encoding="utf-8") as f:
                latest_plan = json.load(f)

            time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if symbol in new_grid_data:
                latest_plan[symbol] = new_grid_data[symbol]
                latest_plan[symbol]["update_time"] = time_str
                # cooldown_until 由 AI 在 JSON 中设定（rebuild prompt 要求）
                # 如果 AI 未设定，给默认 30 分钟冷却
                if "cooldown_until" not in latest_plan[symbol]:
                    cooldown_until = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
                    latest_plan[symbol]["cooldown_until"] = cooldown_until

            with open(PLAN_FILE, "w", encoding="utf-8") as f:
                json.dump(latest_plan, f, ensure_ascii=False, indent=4)
            print(f"✅ {symbol} 订单事件重构网格已落盘")

        except json.JSONDecodeError as e:
            print(f"❌ 重构 JSON 格式损坏: {e}")
    else:
        print(f"⚠️ AI 重构回复中未找到 JSON 代码块")

    # 2. 推送到 TG — 使用落盘后的权威数据
    tg_reply = ai_reply
    try:
        with open(PLAN_FILE, "r", encoding="utf-8") as f:
            final_plan = json.load(f)
        if symbol in final_plan:
            canonical_json_str = json.dumps(
                {symbol: final_plan[symbol]}, ensure_ascii=False, indent=2
            )
            tg_reply = re.sub(
                r'```json\s*.*?\s*```',
                f'```json\n{canonical_json_str}\n```',
                ai_reply,
                flags=re.DOTALL,
            )
    except Exception as e:
        print(f"⚠️ 读取最终 PLAN_FILE 失败，降级推送原始 ai_reply: {e}")

    tg_analysis(
        f"📐 **网格重构完成** ┃ **{symbol}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{tg_reply}"
    )


# ===========================================================================
# 🎯 核心触发层
# ===========================================================================

def process_grid_trigger(symbol: str, data: dict, current_price: float, zone_name: str, zone_info: dict):
    """
    【网格触线裁决】完整处理流程：
    数据采集 → 构建唤醒词 → 发送 L5 通知 → 调用 AI → 执行裁决
    """
    trigger_price = float(zone_info["price"])
    macro_thesis = data.get("macro_thesis", "未配置盘前宏观剧本，请仅依赖短期盘面判断。")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 💥 网格触线: {symbol} [{zone_name}]，正在采集盘面数据...")

    # 1. 统一数据采集
    ctx = _collect_market_data(symbol, current_price)

    # 2. 新闻策略：波动超 2% 时才调用，节省 Tavily API 配额
    if ctx["change_pct"] >= 2.0:
        print(f"🚨 {symbol} 日内波动 {ctx['change_pct']:.1f}% 达标，触发 Tavily 新闻搜索...")
        news_info = fetch_latest_news(symbol)
    else:
        news_info = "☑️ 日内未见异常基本面消息，纯技术面博弈。"

    # 3. 构建网格触线专用唤醒词
    wake_msg = build_grid_trigger_message(
        symbol=symbol,
        current_price=current_price,
        zone_name=zone_name,
        trigger_price=trigger_price,
        macro_thesis=macro_thesis,
        ctx=ctx,
        news_info=news_info,
    )

    # 4. 通知已接管（仅网格触线时发送 L5 通知）
    tg_analysis(
        f"🤖 **L5 全自动接管** ┃ **{symbol}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 触发: {zone_name} ┃ 现价 ${current_price} ┃ 触发价 ${trigger_price}\n"
        f"🦞 正在后台唤醒 AI 进行裁决..."
    )

    # 5. 调用 AI 并处理裁决
    ai_reply = call_ai(wake_msg, INTRADAY_SENTRY_PROMPT)
    if ai_reply:
        handle_ai_verdict(symbol, ai_reply, zone_name=zone_name, current_price=current_price)

    # 防洪闸：处理完一只雷区股票强制冷却 60 秒
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 哨兵进入 60 秒战术冷却...")
    time.sleep(60)


def process_order_event(symbol: str, data: dict, current_price: float, special_event_msg: str):
    """
    【订单状态变更重构】完整处理流程：
    数据采集 → 构建唤醒词 → 调用 AI → 全面吸收新网格
    不发送 L5 接管通知，不执行下单操作。
    """
    macro_thesis = data.get("macro_thesis", "未配置盘前宏观剧本，请仅依赖短期盘面判断。")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📐 订单状态变更: {symbol}，正在采集盘面数据...")

    # 1. 统一数据采集
    ctx = _collect_market_data(symbol, current_price)

    # 2. 新闻策略：订单事件场景无条件拉取，确保 AI 掌握最新信息
    print(f"📰 {symbol} 订单事件场景，无条件触发 Tavily 新闻搜索...")
    news_info = fetch_latest_news(symbol)

    # 3. 构建订单事件专用唤醒词
    wake_msg = build_order_event_message(
        symbol=symbol,
        current_price=current_price,
        special_event_msg=special_event_msg,
        macro_thesis=macro_thesis,
        ctx=ctx,
        news_info=news_info,
    )

    # 4. 调用 AI 并处理重构结果（不发 L5 通知，不解析 ACTION）
    ai_reply = call_ai(wake_msg, INTRADAY_REBUILD_PROMPT)
    if ai_reply:
        handle_rebuild_result(symbol, ai_reply)

    # 防洪闸
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 哨兵进入 60 秒战术冷却...")
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
                tg_order(f"🆘 **【撤单失败警报】**\n标的: {symbol}\n原因: {cancel_result['error']}\n⚠️ 请手动处理！")
                return False, "", True  # 保持锁定，等下一轮重试

            tg_order(f"⚠️ **【战术撤单】**\n标的: {symbol}\n原因: 挂单超时15分钟未成交，已由哨兵自动撤单以释放资金。")
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
        tg_order(f"🎉 **【订单成交捷报】**\n标的: {symbol}\n状态: 完全成交 (Filled)\n下一步: 正在唤醒大脑重铸网格...")
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
        tg_order(msg)
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
    # 优先级排序：止损 > 止盈 > 买入类
    # 防止极端行情下价格同时触及止损和买入区间时，错误触发加仓而非止损
    PRIORITY_ORDER = ["stop_loss", "take_profit", "add_position", "buy_oversold", "buy_dip", "buy_breakout"]

    zones = data.get("zones", {})
    # 先按优先级检查已知类型
    for z_name in PRIORITY_ORDER:
        if z_name not in zones:
            continue
        z_info = zones[z_name]
        try:
            trigger_price = float(z_info["price"])
            cond = z_info["condition"]
            if (cond == "<=" and current_price <= trigger_price) or \
               (cond == ">=" and current_price >= trigger_price):
                return True, z_name, z_info
        except (KeyError, ValueError, TypeError):
            continue

    # 再检查未在优先级列表中的自定义 zone（如 tp_1, tp_2 等）
    for z_name, z_info in zones.items():
        if z_name in PRIORITY_ORDER:
            continue  # 已检查过
        try:
            trigger_price = float(z_info["price"])
            cond = z_info["condition"]
            if (cond == "<=" and current_price <= trigger_price) or \
               (cond == ">=" and current_price >= trigger_price):
                return True, z_name, z_info
        except (KeyError, ValueError, TypeError):
            continue

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
            hk_active, us_active, us_after_hours, sleep_seconds = _get_market_status()

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
            if hk_active: active_markets.append("🇭🇰港股")
            if us_active: active_markets.append("🇺🇸美股盘后" if us_after_hours else "🇺🇸美股")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🟢 {' / '.join(active_markets)}，执行 {POLL_INTERVAL // 60} 分钟例行扫描...")

            # 2. 读取作战计划
            plan = _load_plan()
            if plan is None:
                time.sleep(POLL_INTERVAL)
                continue

            # 3. 批量拉取实时价格（只拉当前活跃市场的标的）
            #    ⚠️ 盘后时段使用 extended_quote 获取盘后实时价，避免 last_done 停留在收盘价
            live_prices: dict[str, float] = {}
            for sym in plan.keys():
                # 按市场过滤：港股只在港股开盘时扫描，美股只在美股时段扫描
                if sym.endswith(".HK") and not hk_active:
                    continue
                if sym.endswith(".US") and not us_active:
                    continue
                # 盘后时段：必须用 extended_quote 获取 post_market_quote 中的实时价
                if sym.endswith(".US") and us_after_hours:
                    result = _logic_get_extended_quote(sym)
                else:
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

                # 命中网格 or 强制唤醒 → 分流到对应处理流程
                if force_wakeup:
                    # 订单状态变更 → 重构网格（不下单）
                    process_order_event(symbol, data, current_price, special_event_msg)
                elif hit:
                    # 网格触线 → AI 裁决（可能下单）
                    process_grid_trigger(symbol, data, current_price, zone_name, zone_info)

        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚨 哨兵监控遭遇异常: {e}")

        print(f"[{datetime.now().strftime('%H:%M:%S')}] ♻️ 本轮巡视结束，休眠 {POLL_INTERVAL // 60} 分钟...\n")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_sentry()
