"""
盘前谋划脚本 (Premarket Planner)
定时在周一至周五 8:30 (港股) / 20:30 (美股) 运行。
采集多维数据 → 推送给 AI → AI 输出网格 JSON → 写入 daily_trading_plan.json → TG 通知。
"""

import json
import os
import sys
import time
import re
import requests
import schedule
from datetime import datetime, timedelta, date
import pytz
from longbridge.openapi import Period, Market

# ==========================================================
# 📦 导入公共工具模块（路径设置、缓存、数据获取等均在 shared_utils 中初始化）
# ==========================================================
from shared_utils import (
    ROOT_DIR as _ROOT_DIR,
    PLAN_FILE, CACHE_DIR, OPENCLAW_URL, OPENCLAW_HEADERS,
    # 缓存读写
    read_cache, write_cache,
    # 交易计划读写
    load_plan, save_plan,
    # 标的代码转换
    lb_to_futu,
    # 资讯获取
    fetch_latest_news,
    # 期权数据
    fetch_option_snapshot,
    # AI 调用 (带重试 / 限流排队)
    call_ai_with_retry,
)

# ==========================================
# 📦 从 longbridge_server 导入盘前专用函数
# ==========================================
from longbridge_server import (
    _logic_get_trading_days,                  # Step 0: 交易日查询
    get_account_asset,                        # Step 1: 账户持仓与购买力
    _logic_get_live_quote,                    # 常规盘实时报价
    _logic_get_extended_quote,                # 含盘前/盘后的最新价格
    _logic_get_market_temperature,            # Step 2: 当前市场温度
    _logic_get_market_temperature_history,    # Step 2: 历史市场温度
    _logic_get_static_info,                   # Step 3: 标的基本信息
    _logic_get_financial_indexes,             # Step 3: 估值指标 (支持自定义)
    _logic_get_history_kline,                 # Step 4+5: K 线数据
    close_contexts,                           # 释放 WebSocket 连接
)

# 释放富途 OpenD 连接
from futu_options_server import close_context as close_futu_context

# 导入 TG 发送工具
from tg_sender import send_message_async


# ==========================================
# ⚙️ 盘前专用配置
# ==========================================

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN_CLAW")
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID_ANALYSIS")

PROMPT_FILE = os.path.join(_ROOT_DIR, "prompts", "premarket_planner_prompt.md")

# 标的列表
HK_SYMBOLS = ["0700.HK", "09988.HK", "01810.HK", "00100.HK", "02513.HK", "06082.HK"]
US_SYMBOLS = ["NVDA.US", "TSLA.US", "GOOGL.US", "AMD.US", "AAPL.US", "MU.US", "SNDK.US", "INTC.US", "GLD.US"]

# 标的间隔 (秒)
SYMBOL_INTERVAL = 300  # 5 分钟

# 加载 Prompt
with open(PROMPT_FILE, "r", encoding="utf-8") as f:
    PREMARKET_PROMPT = f.read()


# ==========================================
# 🛠️ 盘前专用工具
# ==========================================

def tg_send(text: str):
    """发送 Telegram 消息 (后台免阻塞)"""
    targets = [(TG_BOT_TOKEN, TG_CHANNEL_ID)] if TG_CHANNEL_ID else []
    send_message_async(text, targets=targets)


# ==========================================
# 📡 Step 0: 开市判断
# ==========================================

def is_trading_day(market: str) -> bool:
    """
    判断今天是否为指定市场的交易日。
    直接调用底层 _logic_get_trading_days，统一在服务端处理时区。
    """
    try:
        resp = _logic_get_trading_days(market)
        if isinstance(resp, dict) and "is_today_trading_day" in resp:
            return resp["is_today_trading_day"]
        return False
    except Exception as e:
        print(f"⚠️ 开市判断异常: {e}，默认视为交易日")
        return True  # 异常时保守执行


# ==========================================
# 📡 Step 1: 账户状态
# ==========================================

def fetch_account_status(market: str) -> str:
    """
    获取账户购买力与全部持仓，计算各标的仓位占比并返回可读文本。

    仓位占比公式：
        当前标的市值 ÷ (同市场所有持仓市值合计 + 同市场现金余额)
    分母使用"现金"而非"购买力"，不含融资授信，反映真实风险暴露。
    """
    try:
        asset = get_account_asset()
        buying_power = asset.get("buy_power", "0")
        cash_info = asset.get("cash_info", {})
        positions = asset.get("positions", [])

        target_currency = "HKD" if market == "HK" else "USD"
        cash_val_str = cash_info.get(target_currency, "0")
        try:
            cash_val = float(str(cash_val_str).replace(",", ""))
        except (ValueError, TypeError):
            cash_val = 0.0

        # ── 筛选同市场持仓，计算每只标的的市值 ──────────────────────────
        same_market_positions = []
        for p in positions:
            sym = p.get("symbol", "")
            is_same = (market == "HK" and sym.endswith(".HK")) or \
                      (market == "US" and sym.endswith(".US"))
            if not is_same:
                continue

            # 优先用实时市值，fallback 用 available_qty × cost_price（成本估算）
            mkt_raw = p.get("market_value")
            try:
                mkt_val = float(str(mkt_raw).replace(",", ""))
            except (ValueError, TypeError, AttributeError):
                qty  = float(p.get("available_qty", 0) or 0)
                cost = float(p.get("cost_price", 0) or 0)
                mkt_val = qty * cost  # fallback 估算

            same_market_positions.append({
                **p,
                "_mkt_val": mkt_val,
            })

        # ── 计算同市场净资产（分母）───────────────────────────────────────
        total_holding_val = sum(p["_mkt_val"] for p in same_market_positions)
        nav = total_holding_val + cash_val  # 净资产 = 持仓市值 + 现金

        # ── 组装输出文本 ──────────────────────────────────────────────────
        mkt_val_src = "⚠️(成本估算)" if any(
            p.get("market_value") in (None, "N/A", "") for p in positions
        ) else "实时"

        lines = [
            f"💵 可用现金({target_currency}): ${cash_val_str} | 💳 最大购买力(含融资): ${buying_power}",
            f"📊 {target_currency} 市场净资产: ${nav:,.0f} (持仓市值 ${total_holding_val:,.0f} + 现金 ${cash_val:,.0f}) [{mkt_val_src}]",
            f"{'='*40}",
        ]

        if same_market_positions:
            for p in same_market_positions:
                sym   = p.get("symbol", "N/A")
                qty   = p.get("available_qty", 0)
                cost  = p.get("cost_price", 0)
                cur   = p.get("currency", target_currency)
                mval  = p["_mkt_val"]
                ratio = (mval / nav * 100) if nav > 0 else 0
                warn  = " ⚠️接近上限" if ratio >= 45 else (" 🚨超限" if ratio >= 55 else "")
                lines.append(
                    f"  📦 {sym}: {qty}股 | 成本{cur}${cost} | 市值${mval:,.0f}"
                    f" | **占比 {ratio:.1f}%**{warn}"
                )
        else:
            lines.append(f"  📦 {target_currency} 市场当前空仓")

        # 其他市场持仓简要附上
        other = [p for p in positions if p not in same_market_positions
                 and p.get("symbol", "") not in [q.get("symbol", "") for q in same_market_positions]]
        if other:
            lines.append(f"{'='*40}")
            lines.append("🌐 其他市场持仓（参考）:")
            for p in other:
                lines.append(f"  {p.get('symbol')}: {p.get('available_qty')}股 成本${p.get('cost_price')}")

        return "\n".join(lines)
    except Exception as e:
        return f"账户状态获取失败: {e}"


def get_symbol_position(symbol: str) -> tuple[int, float]:
    """获取指定标的的持仓数量和成本价"""
    try:
        asset = get_account_asset()
        for p in asset.get("positions", []):
            # 长桥返回的港股代码格式可能是 700.HK, 9988.HK
            # 我们传进来的可能是 0700.HK, 09988.HK，所以要做个去前导 0 的兼容对比
            p_sym = p.get("symbol", "")
            if p_sym == symbol or p_sym.lstrip('0') == symbol.lstrip('0'):
                return int(float(p.get("available_qty", 0))), float(p.get("cost_price", 0.0))
    except Exception:
        pass
    return 0, 0.0


# ==========================================
# 📡 Step 2: 市场温度
# ==========================================

def _get_last_n_trading_days(market: str, n: int) -> tuple[date, date]:
    """
    返回最近 n 个交易日的 (start_date, end_date)。
    调用官方交易日历 API，避免攼标日、展期、连续假期误判。
    """
    today = datetime.now().date()
    # 向前拉 30 天保证能覆盖到足够的交易日
    start_d = today - timedelta(days=30)
    try:
        resp = _logic_get_trading_days(market, str(start_d), str(today))
        if isinstance(resp, dict) and "trade_days" in resp:
            days = sorted(
                [d["date"] for d in resp["trade_days"]],
                reverse=True  # 降序，最新在前
            )
            # 取最近 n 个，转回 date 对
            recent = [datetime.strptime(d, "%Y-%m-%d").date() for d in days[:n]]
            if recent:
                return min(recent), max(recent)
    except Exception as e:
        print(f"  ⚠️ 交易日查询失败({market})，退化为日历日: {e}")
    # 降级：日历日失败备份
    return today - timedelta(days=n * 2), today


# ==========================================
# 📡 Step 2: 市场温度
# ==========================================

def fetch_market_temperature(market: str) -> str:
    """获取当前市场温度 + 最近 5 个交易日历史温度"""
    try:
        # 当前温度
        current = _logic_get_market_temperature(market)
        lines = [f"🌡️ 当前市场温度: {current.get('temp', 'N/A')} | {current.get('desc', '')}"]
        lines.append(f"   估值: {current.get('val', 'N/A')} | 情绪: {current.get('sent', 'N/A')}")

        # 最近 5 个交易日历史温度
        try:
            # 拉足够多天的历史数据确保能覆盖 5 个交易日
            history = _logic_get_market_temperature_history(market, days=14)
            if isinstance(history, list) and history:
                # 按日期降序取最新的 5 个交易日
                start_td, end_td = _get_last_n_trading_days(market, 5)
                start_str = str(start_td)
                history_filtered = [
                    item for item in history
                    if item.get("date", "") >= start_str
                ]
                if history_filtered:
                    lines.append("📊 近5个交易日温度趋势:")
                    for item in sorted(history_filtered, key=lambda x: x.get("date", "")):
                        lines.append(f"   {item.get('date')}: {item.get('temp')}")
                else:
                    # 无交易日过滤结果时直接取最后 5 条
                    lines.append("📊 近5日温度趋势 (已尽力覆盖交易日):")
                    for item in history[-5:]:
                        lines.append(f"   {item.get('date')}: {item.get('temp')}")
            elif isinstance(history, dict) and "error" in history:
                lines.append(f"   (历史温度拉取失败: {history['error']})")
        except Exception as e:
            lines.append(f"   (历史温度拉取发生异常: {e})")

        return "\n".join(lines)
    except Exception as e:
        return f"市场温度获取失败: {e}"


# ==========================================
# 📡 Step 3: 标的基本信息 (支持本地缓存)
# ==========================================

def fetch_static_info(symbol: str) -> str:
    """
    获取标的基本信息 + 估值指标。
    恒定信息（名称、行业、每手）从缓存读取，估值数据每次刷新。
    """
    # 读取恒定信息缓存
    cache_file = f"static_info_{symbol.replace('.', '_')}.json"
    cached = read_cache(cache_file)

    if cached:
        static = cached
    else:
        static = _logic_get_static_info(symbol)
        if static and "error" not in static:
            write_cache(cache_file, static)

    # 每次刷新估值指标
    fin = _logic_get_financial_indexes(symbol, [
        "TotalMarketValue", "PeTtmRatio", "PbRatio",
        "DividendRatioTtm", "TurnoverRate", "VolumeRatio",
        "Amplitude", "FiveDayChangeRate", "TenDayChangeRate",
    ])

    lines = ["📋 标的基本信息:"]
    lines.append(f"  名称: {static.get('name', 'N/A')}")
    lines.append(f"  板块: {static.get('board', 'N/A')}")
    lines.append(f"  币种: {static.get('currency', 'N/A')}")
    lines.append(f"  每手: {static.get('lot_size', 'N/A')}")
    lines.append(f"  总股本: {static.get('total_shares', 'N/A')}")

    if fin and "error" not in fin:
        lines.append("📊 估值指标:")
        label_map = {
            "total_market_value": "总市值",
            "pe_ttm_ratio": "市盈率(TTM)",
            "pb_ratio": "市净率",
            "dividend_ratio_ttm": "股息率(TTM)",
            "turnover_rate": "换手率",
            "volume_ratio": "量比",
            "amplitude": "振幅",
            "five_day_change_rate": "五日涨跌幅",
            "ten_day_change_rate": "十日涨跌幅",
        }
        for key, label in label_map.items():
            val = fin.get(key)
            if val is not None:
                lines.append(f"  {label}: {val}")
    else:
        lines.append(f"  估值指标获取失败: {fin}")

    return "\n".join(lines)


# ==========================================
# 📡 Step 4: 60日日K线 (支持本地缓存)
# ==========================================

def fetch_daily_kline(symbol: str) -> str:
    """
    获取最近60天的日K线数据。
    本地缓存策略：如果缓存存在且最新日期足够新，仅拉增量追尾。
    """
    cache_file = f"daily_kline_{symbol.replace('.', '_')}.json"
    cached = read_cache(cache_file)

    today = datetime.now().date()
    need_full_fetch = True
    existing_data = []

    if cached and isinstance(cached, list) and len(cached) > 0:
        latest_date_str = cached[-1].get("t", "")[:10]
        try:
            latest_date = datetime.strptime(latest_date_str, "%Y-%m-%d").date()
            # 如果缓存的最新日期是昨天或今天，只需拉增量
            if (today - latest_date).days <= 2 and len(cached) >= 50:
                need_full_fetch = False
                existing_data = cached
                # 拉增量部分
                incr_start = latest_date + timedelta(days=1)
                if incr_start <= today:
                    new_data = _logic_get_history_kline(symbol, Period.Day, incr_start, today)
                    if new_data and "error" not in new_data[0]:
                        existing_data.extend(new_data)
                        # 保持最多60条
                        existing_data = existing_data[-60:]
                        write_cache(cache_file, existing_data)
        except (ValueError, IndexError):
            need_full_fetch = True

    if need_full_fetch:
        start_d = today - timedelta(days=90)  # 拉90天确保覆盖60个交易日
        data = _logic_get_history_kline(symbol, Period.Day, start_d, today)
        if data and "error" not in data[0]:
            existing_data = data[-60:]  # 保留最近60条
            write_cache(cache_file, existing_data)
        else:
            return f"日K线拉取失败: {data}"

    if not existing_data:
        return "日K线数据为空"

    # 格式化输出
    lines = [f"📈 60日日K线 ({len(existing_data)}条):"]
    lines.append("日期 | 开 | 高 | 低 | 收 | 成交量")
    for k in existing_data:
        lines.append(f"{k['t']} | {k['o']} | {k['h']} | {k['l']} | {k['c']} | {k['v']}")

    # 附加统计
    highs = [k['h'] for k in existing_data]
    lows = [k['l'] for k in existing_data]
    lines.append(f"\n📊 30日极值: 最高 {max(highs[-30:])} / 最低 {min(lows[-30:])}")
    lines.append(f"📊 60日极值: 最高 {max(highs)} / 最低 {min(lows)}")

    return "\n".join(lines)


# ==========================================
# 📡 Step 5: 3日10分钟K线 (支持本地缓存)
# ==========================================

def fetch_min10_kline(symbol: str) -> str:
    """
    获取最近 3 个交易日的 10 分钟 K 线数据。
    - 交易日范围通过 API 交易日历获取，避免连续假期漏缺。
    - 美股只保留常规盘中数据（09:30-16:00 美东时间），过滤盘前/盘后。
    """
    market = "US" if symbol.endswith(".US") else "HK"
    cache_file = f"min10_kline_{symbol.replace('.', '_')}.json"
    cached = read_cache(cache_file)

    today = datetime.now().date()
    need_fetch = True

    if cached and isinstance(cached, list) and len(cached) > 0:
        latest_date_str = cached[-1].get("t", "")[:10]
        try:
            latest_date = datetime.strptime(latest_date_str, "%Y-%m-%d").date()
            # 如果最新日期是昨天或今天，且数据量足够，跳过
            if (today - latest_date).days <= 1 and len(cached) >= 30:
                need_fetch = False
        except (ValueError, IndexError):
            pass

    if need_fetch:
        # 通过交易日历 API 获取最近 3 个交易日范围
        start_d, end_d = _get_last_n_trading_days(market, 3)
        data = _logic_get_history_kline(symbol, Period.Min_10, start_d, end_d)
        if data and isinstance(data, list) and "error" not in data[0]:
            cached = data
            write_cache(cache_file, data)
        else:
            return f"10分钟K线拉取失败: {data}"

    if not cached:
        return "短周期K线数据为空"

    # 美股：按美东时间分段标注（盘前/盘中/盘后），保留完整延伸时段数据
    if market == "US":
        et = pytz.timezone("America/New_York")
        sections = []
        current_session = None
        count = 0

        for k in cached:
            try:
                t = datetime.strptime(k["t"], "%Y-%m-%d %H:%M")
                t_et = pytz.utc.localize(t).astimezone(et)
                h, m = t_et.hour, t_et.minute
                time_str = t_et.strftime("%m-%d %H:%M")

                if h < 4:
                    continue  # 夜盘，忽略
                elif h < 9 or (h == 9 and m < 30):
                    session = "🌅 盘前"
                elif h < 16:
                    session = "📈 盘中"
                elif h < 20:
                    session = "🌙 盘后"
                else:
                    continue

                if session != current_session:
                    sections.append(f"--- {session} ---")
                    current_session = session

                sections.append(f"{time_str} | {k['o']} | {k['h']} | {k['l']} | {k['c']} | {k['v']}")
                count += 1
            except Exception:
                sections.append(f"{k['t']} | {k['o']} | {k['h']} | {k['l']} | {k['c']} | {k['v']}")
                count += 1

        lines = [f"📈 近3个交易日10分钟K线 ({count}条，含盘前/盘后):"]
        lines.append("时间(美东) | 开 | 高 | 低 | 收 | 成交量")
        lines.extend(sections[-180:])
        return "\n".join(lines)

    # 非美股：直接输出全量
    lines = [f"📈 近3个交易日10分钟K线 ({len(cached)}条):"]
    lines.append("时间 | 开 | 高 | 低 | 收 | 成交量")
    for k in cached[-120:]:
        lines.append(f"{k['t']} | {k['o']} | {k['h']} | {k['l']} | {k['c']} | {k['v']}")
    return "\n".join(lines)


# ==========================================
# 📡 Step 5b: 美股盘前 5 分钟线 (US Only)
# ==========================================

def fetch_premarket_kline_us(symbol: str) -> str:
    """
    获取美股当日盘前时段的 5 分钟 K 线（常规盘开盘之前的盘前交易数据）。
    时段范围：美东时间 04:00–09:29。
    只适用于美股（.US 后缀），其他市场返回空字符串。
    """
    if not symbol.endswith(".US"):
        return ""

    try:
        import pytz
        et = pytz.timezone("America/New_York")
        today = datetime.now().date()
        k_lines = _logic_get_history_kline(symbol, Period.Min_5, today, today)

        if not k_lines or "error" in k_lines[0]:
            return "盘前 5 分钟线暂无数据。"

        premarket = []
        for k in k_lines:
            try:
                t = datetime.strptime(k["t"], "%Y-%m-%d %H:%M")
                t_et = pytz.utc.localize(t).astimezone(et)
                h, m = t_et.hour, t_et.minute
                # 盘前时段：04:00–09:29
                if 4 <= h < 9 or (h == 9 and m < 30):
                    premarket.append((t_et, k))
            except Exception:
                pass

        if not premarket:
            return "盘前时段暂无 5 分钟 K 线数据（可能常规盘尚未开始）。"

        lines = [f"🌅 美股盘前 5min K 线 ({len(premarket)}条):",
                 "时间(美东) | 开 | 高 | 低 | 收 | 成交量"]
        for t_et, k in premarket:
            lines.append(f"{t_et.strftime('%H:%M')} | {k['o']} | {k['h']} | {k['l']} | {k['c']} | {k['v']}")
        return "\n".join(lines)

    except Exception as e:
        return f"盘前 K 线获取失败: {e}"


# ==========================================
# 🛠️ 股票代码格式转换工具
# ==========================================

# lb_to_futu 已移至 shared_utils


# ==========================================
# 📡 Step 6: 期权信息
# ==========================================

# fetch_option_data 已移至 shared_utils (fetch_option_snapshot)



# ==========================================
# 📡 Step 7: Tavily 资讯
# ==========================================

# fetch_latest_news 已移至 shared_utils


# ==========================================
# 📝 消息构建层 (Step 8)
# ==========================================

def build_premarket_message(
    symbol: str,
    market: str,
    account_str: str,
    position_str: str,
    temp_str: str,
    static_str: str,
    daily_kline_str: str,
    min_kline_str: str,
    premarket_kline_str: str,
    option_str: str,
    news_str: str,
    current_price: float = 0.0,
    price_session: str = "",
) -> str:
    """将所有盘面数据组装成发给 AI 的完整盘前分析提示词"""

    # 仅美股且有盘前K线数据时插入独立段落
    premarket_section = (
        f"{'='*50}\n"
        f"🌅 **五b、当日美股盘前 5min K 线**\n"
        f"{premarket_kline_str}\n\n"
    ) if premarket_kline_str else ""

    # 实时价格行（标注时段来源，让 AI 明确知道这是盘前/盘后价格）
    _mkt_tz = pytz.timezone("America/New_York") if market == "US" else pytz.timezone("Asia/Hong_Kong")
    _session_labels = {"pre_market": "盘前", "post_market": "盘后", "regular": "盘中", "over_night": "夜盘"}
    session_tag = _session_labels.get(price_session, "")
    if current_price > 0:
        mkt_time_str = datetime.now(_mkt_tz).strftime("%H:%M %Z")
        price_line = f"💲 **当前实时价格: ${current_price:.2f}** ({session_tag} {mkt_time_str})\n"
    else:
        price_line = ""

    return (
        f"📋 **【盘前谋划数据投喂】** {symbol} ({market}股)\n"
        f"⏰ 生成时间: {datetime.now(_mkt_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"{price_line}\n"

        f"{'='*50}\n"
        f"🏦 **一、账户状态**\n"
        f"{account_str}\n"
        f"  📦 {symbol} 持仓: {position_str}\n\n"

        f"{'='*50}\n"
        f"🌡️ **二、市场环境**\n"
        f"{temp_str}\n\n"

        f"{'='*50}\n"
        f"📋 **三、标的基本面**\n"
        f"{static_str}\n\n"

        f"{'='*50}\n"
        f"📈 **四、60日日K线趋势**\n"
        f"{daily_kline_str}\n\n"

        f"{'='*50}\n"
        f"📈 **五、近3日10分钟K线微观结构**\n"
        f"{min_kline_str}\n\n"

        + premarket_section +

        f"{'='*50}\n"
        f"🛡️ **六、期权链深度数据**\n"
        f"{option_str}\n\n"

        f"{'='*50}\n"
        f"📰 **七、最新市场资讯**\n"
        f"{news_str}\n\n"

        f"{'='*50}\n"
        f"⚠️ **【最高执行指令】**\n"
        f"你现在的任务是作为盘前首席策略师，综合以上全部数据，为 {symbol} 生成一份网格交易计划。\n"
        f"**绝对禁止调用任何工具！** 直接在回复中输出分析 + JSON。\n\n"
        f"**输出要求：**\n"
        f"1. 先输出你的多维分析（技术面/估值面/期权面/资讯面）\n"
        f"2. 在回复最末尾用 ` ```json ... ``` ` 输出该标的的网格交易计划（只输出 {symbol} 一只）\n"
        f"3. JSON 必须包含 status, macro_thesis, update_time, zones 字段\n"
    )



# ==========================================
# 🤖 AI 交互层
# ==========================================

def call_ai(wake_msg: str) -> str | None:
    """将唤醒消息发送给本地 OpenClaw，返回 AI 回复文本（内置 429 重试 + 限流排队）"""
    content, error = call_ai_with_retry(
        messages=[
            {"role": "system", "content": PREMARKET_PROMPT},
            {"role": "user",   "content": wake_msg},
        ],
        timeout=300,  # 盘前分析数据量大，给更多时间
        caller_label="盘前谋划",
    )
    if content:
        return content
    tg_send(f"❌ 盘前谋划 AI 调用失败: {error}")
    return None


def handle_ai_result(symbol: str, ai_reply: str):
    """
    解析 AI 回复：
    1. 提取 ```json...``` 更新 daily_trading_plan.json
    2. 推送 AI 分析报告到 TG
    """
    # 1. 提取并更新 JSON
    json_match = re.search(r'```json\s*(.*?)\s*```', ai_reply, re.DOTALL)
    if json_match:
        try:
            new_grid = json.loads(json_match.group(1))
            plan = load_plan()

            time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if symbol in new_grid:
                plan[symbol] = new_grid[symbol]
                plan[symbol]["update_time"] = time_str
                # 清除旧的 cooldown 和 pending_order（盘前重铸，盘中状态机重新开始）
                plan[symbol].pop("cooldown_until", None)
                plan[symbol].pop("pending_order", None)

            save_plan(plan)
            print(f"✅ {symbol} 网格已更新到 daily_trading_plan.json")

        except json.JSONDecodeError as e:
            print(f"❌ AI 返回的 JSON 格式损坏: {e}")
            tg_send(f"❌ {symbol} 盘前网格 JSON 解析失败: {e}")
    else:
        print(f"⚠️ AI 未返回 JSON 代码块")
        tg_send(f"⚠️ {symbol} 盘前谋划 AI 未返回网格 JSON")

    # 2. 推送 AI 分析报告
    prefix = f"💭 **盘前策略报告** ┃ **{symbol}**\n━━━━━━━━━━━━━━━━━━━━━\n"
    max_safe = 4000  # 内容最大字符数（含前缀不超过 4096）
    if len(prefix) + len(ai_reply) <= max_safe:
        tg_send(prefix + ai_reply)
    else:
        # 分段发送，每次确保 prefix + chunk 不超过 max_safe
        chunk_size = max_safe - len(prefix)
        chunks = [ai_reply[i:i+chunk_size] for i in range(0, len(ai_reply), chunk_size)]
        for i, chunk in enumerate(chunks):
            header = prefix if i == 0 else f"💭 **盘前策略报告** ┃ **{symbol}** (续 {i+1})\n━━━━━━━━━━━━━━━━━━━━━\n"
            tg_send(header + chunk)


# ==========================================
# 🎯 单标的完整处理流程
# ==========================================

def process_single_symbol(symbol: str, market: str, market_name: str):
    """对单个标的执行完整的盘前分析流程"""
    print(f"\n{'='*60}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🎯 开始分析: {symbol}")
    print(f"{'='*60}")

    try:
        # Step 1: 账户状态
        print(f"  📡 Step 1/8: 获取账户状态...")
        account_str = fetch_account_status(market)
        qty, cost = get_symbol_position(symbol)
        position_str = f"已持仓 {qty}股 (成本 ${cost:.2f})" if qty > 0 else "空仓"

        # Step 2: 市场温度
        print(f"  📡 Step 2/8: 获取市场温度...")
        temp_str = fetch_market_temperature(market)

        # Step 3: 标的基本信息
        print(f"  📡 Step 3/8: 获取标的基本信息...")
        static_str = fetch_static_info(symbol)

        # Step 4: 60日日K线
        print(f"  📡 Step 4/8: 获取60日日K线...")
        daily_kline_str = fetch_daily_kline(symbol)

        # Step 5: 3日10分钟K线
        print(f"  📡 Step 5/8: 获取短周期K线...")
        min_kline_str = fetch_min10_kline(symbol)

        # Step 5b: 美股当日盘前 5min K线（仅美股）
        premarket_kline_str = ""
        if market == "US":
            print(f"  📡 Step 5b: 获取美股盘前 5min K 线...")
            premarket_kline_str = fetch_premarket_kline_us(symbol)

        # Step 5c: 获取当前实时价格（美股用 extended_quote 获取盘前/盘后价）
        current_price = 0.0
        if market == "US":
            price_result = _logic_get_extended_quote(symbol)
        else:
            price_result = _logic_get_live_quote(symbol)
        if "error" not in price_result:
            current_price = price_result["price"]
            session_info = price_result.get("session", "regular")
            print(f"  📡 Step 5c: 当前价格 ${current_price:.2f} (session: {session_info})")
        else:
            print(f"  ⚠️ Step 5c: 实时价格获取失败: {price_result.get('error')}")

        # Step 6: 期权数据
        print(f"  📡 Step 6/8: 获取期权数据...")
        # 富途 API 支持港股和美股期权，统一调用；内部已有完整容错
        option_str = fetch_option_snapshot(symbol, current_price=current_price)

        # Step 7: 最新资讯
        print(f"  📡 Step 7/8: 获取最新资讯...")
        news_str = fetch_latest_news(symbol, mode="advanced")

        # Step 8: 组装消息
        print(f"  📝 组装 AI 投喂消息...")
        wake_msg = build_premarket_message(
            symbol=symbol,
            market=market,
            account_str=account_str,
            position_str=position_str,
            temp_str=temp_str,
            static_str=static_str,
            daily_kline_str=daily_kline_str,
            min_kline_str=min_kline_str,
            premarket_kline_str=premarket_kline_str,
            option_str=option_str,
            news_str=news_str,
            current_price=current_price,
            price_session=price_result.get("session", "") if current_price > 0 else "",
        )

        # Step 8.5: 缓存盘前所有数据，供盘中使用
        # 注意: 缓存的数据但作“盘前快照”战略参考，盘中实时数据将实斗岖新采集
        print(f"  📝 缓存盘前数据备用...")
        premarket_memo = {
            "daily_kline_str":     daily_kline_str,
            "min_kline_str":       min_kline_str,
            "premarket_kline_str": premarket_kline_str,
            "option_str":          option_str,
            "temp_str":            temp_str,
            "news_str":            news_str,
            "update_time":         datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        write_cache(f"premarket_memo_{symbol.replace('.', '_')}.json", premarket_memo)

        # Step 8+9: 推送 AI
        print(f"  🤖 推送给 AI 分析...")
        tg_send(f"⏳ 正在分析 ┃ **{symbol}** ┃ {market_name}")
        ai_reply = call_ai(wake_msg)

        if ai_reply:
            handle_ai_result(symbol, ai_reply)
        else:
            tg_send(f"❌ {symbol} 盘前谋划 AI 无响应")

        print(f"  ✅ {symbol} 分析完成")

    except Exception as e:
        print(f"  ❌ {symbol} 分析异常: {e}")
        tg_send(f"❌ 盘前谋划异常: {symbol} - {e}")


# ==========================================
# 🕐 主调度层
# ==========================================

def run_premarket_batch(market: str):
    """
    批量执行盘前谋划：
    - 先判断是否为交易日
    - 然后逐个标的分析，间隔5分钟
    """
    symbols = HK_SYMBOLS if market == "HK" else US_SYMBOLS
    market_name = "🇭🇰 港股" if market == "HK" else "🇺🇸 美股"

    print(f"\n{'#'*60}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🌅 盘前谋划启动: {market_name}")
    print(f"{'#'*60}")

    # Step 0: 开市判断
    if not is_trading_day(market):
        msg = f"📅 今日{market_name}不开市，跳过盘前谋划。"
        print(msg)
        tg_send(msg)
        return

    start_time = datetime.now()
    market_flag = "🇭🇰" if market == "HK" else "🇺🇸"
    tg_send(
        f"{market_flag} **盘前谋划启动** ┃ {market_name}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 标的: {', '.join(symbols)}\n"
        f"⏱️ 预计: ~{len(symbols) * 5} 分钟"
    )

    try:
        for i, symbol in enumerate(symbols):
            process_single_symbol(symbol, market, market_name)

            # Step 11: 标的间隔（最后一个不需要等）
            if i < len(symbols) - 1:
                print(f"\n⏳ 标的间隔冷却 {SYMBOL_INTERVAL // 60} 分钟...")
                time.sleep(SYMBOL_INTERVAL)

        elapsed = int((datetime.now() - start_time).total_seconds() / 60)
        tg_send(f"✅ **盘前谋划完成** ┃ {market_name} ┃ 共 {len(symbols)} 只 ┃ 耗时 {elapsed}min")
        print(f"\n✅ 盘前谋划批次完成: {market_name}")

    except Exception as e:
        print(f"❌ 批量盘前谋划任务遭遇异常: {e}")

    finally:
        # **断开底层长连接**：用完即弃，不浪费 24 小时待机的配额
        close_contexts()                   # 释放长桥 WebSocket 连接
        close_futu_context()               # 释放富途 OpenD 连接
        print(f"\n✅ 盘前谋划批次完成并回收连接: {market_name}")


def main():
    """主入口：注册定时任务并运行"""
    print("🌅 盘前谋划调度器启动")
    print(f"   港股: 每周一至周五 08:30")
    print(f"   美股: 每周一至周五 20:30")
    print(f"   标的: HK={HK_SYMBOLS}, US={US_SYMBOLS}")

    # 注册定时任务
    schedule.every().monday.at("08:30").do(run_premarket_batch, market="HK")
    schedule.every().tuesday.at("08:30").do(run_premarket_batch, market="HK")
    schedule.every().wednesday.at("08:30").do(run_premarket_batch, market="HK")
    schedule.every().thursday.at("08:30").do(run_premarket_batch, market="HK")
    schedule.every().friday.at("08:30").do(run_premarket_batch, market="HK")

    schedule.every().monday.at("20:00").do(run_premarket_batch, market="US")
    schedule.every().tuesday.at("20:00").do(run_premarket_batch, market="US")
    schedule.every().wednesday.at("20:00").do(run_premarket_batch, market="US")
    schedule.every().thursday.at("20:00").do(run_premarket_batch, market="US")
    schedule.every().friday.at("20:00").do(run_premarket_batch, market="US")

    # tg_send("🌅 盘前谋划调度器已上线，等待触发时间...")

    # 循环检查 schedule
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
