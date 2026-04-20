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
sys.path.insert(0, os.path.join(_ROOT_DIR, "telegram"))    # 将 telegram/ 加入搜索路径

# ==========================================
# 📦 从 longbridge_server 导入封装好的函数
# ==========================================
from longbridge_server import (
    _logic_get_trading_days,                  # Step 0: 交易日查询
    get_account_asset,                        # Step 1: 账户持仓与购买力
    _logic_get_market_temperature,            # Step 2: 当前市场温度
    _logic_get_market_temperature_history,    # Step 2: 历史市场温度历史
    _logic_get_static_info,                   # Step 3: 标的基本信息
    _logic_get_financial_indexes,             # Step 3: 估值指标 (支持自定义)
    _logic_get_history_kline,                 # Step 4+5: K 线数据
    close_contexts,                           # 释放 WebSocket 连接
)

# 期权模块使用富途 API
from futu_options_server import (
    _logic_get_expiry_dates,                  # Step 6: 期权到期日
    _logic_get_option_chain,                  # Step 6: 期权链
    _logic_get_option_snapshots,              # Step 6: 期权行情(含 YQ 降级)
    close_context as close_futu_context,      # 释放富途 OpenD 连接
)

# 导入 TG 发送工具
from tg_sender import send_message_async

# override=True 确保覆盖系统环境中可能存在的同名旧变量
load_dotenv(find_dotenv(), override=True)


# ==========================================
# ⚙️ 配置层
# ==========================================

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN_CLAW")
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID_ANALYSIS")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))   # OpenClaw/strategies/
_ROOT_DIR   = os.path.dirname(SCRIPT_DIR)                    # OpenClaw/
PLAN_FILE   = os.path.join(_ROOT_DIR, "data", "daily_trading_plan.json")
CACHE_DIR   = os.path.join(_ROOT_DIR, "data", "cache")
PROMPT_FILE = os.path.join(_ROOT_DIR, "prompts", "premarket_planner_prompt.md")

OPENCLAW_URL = "http://127.0.0.1:18789/v1/chat/completions"
OPENCLAW_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {os.getenv('OPENCLAW_GATEWAY_TOKEN', '')}",
    "x-openclaw-scopes": "operator.admin,operator.write",
}

# 标的列表
HK_SYMBOLS = ["0700.HK", "09988.HK", "01810.HK"]
US_SYMBOLS = ["AAPL.US", "NVDA.US", "TSLA.US", "AMD.US", "GOOGL.US", "NBIS.US", "GLD.US"]

# 标的间隔 (秒)
SYMBOL_INTERVAL = 300  # 5 分钟

# 加载 Prompt
with open(PROMPT_FILE, "r", encoding="utf-8") as f:
    PREMARKET_PROMPT = f.read()


# ==========================================
# 🛠️ 工具层
# ==========================================

def tg_send(text: str):
    """发送 Telegram 消息 (后台免阻塞)"""
    targets = [(TG_BOT_TOKEN, TG_CHANNEL_ID)] if TG_CHANNEL_ID else []
    send_message_async(text, targets=targets)


def ensure_cache_dir():
    """确保缓存目录存在"""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)


def read_cache(filename: str) -> dict | list | None:
    """读取本地 JSON 缓存文件"""
    path = os.path.join(CACHE_DIR, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def write_cache(filename: str, data):
    """写入本地 JSON 缓存文件"""
    ensure_cache_dir()
    path = os.path.join(CACHE_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_plan() -> dict:
    """读取当前交易计划 JSON"""
    if os.path.exists(PLAN_FILE):
        with open(PLAN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_plan(plan: dict):
    """保存交易计划 JSON"""
    with open(PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=4)


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

    # 美股只保留常规盘中数据：09:30-16:00 美东时间
    if market == "US":
        import pytz
        et = pytz.timezone("America/New_York")
        filtered = []
        for k in cached:
            try:
                t = datetime.strptime(k["t"], "%Y-%m-%d %H:%M")
                # K 线时间戳转为美东时间
                t_et = pytz.utc.localize(t).astimezone(et)
                h, m = t_et.hour, t_et.minute
                # 筛选 09:30-15:59（常规盘）
                if (h == 9 and m >= 30) or (10 <= h <= 14) or (h == 15):
                    filtered.append(k)
            except Exception:
                filtered.append(k)  # 解析失败时保留
        display_data = filtered
    else:
        display_data = cached

    # 格式化输出
    lines = [f"📈 近3个交易日10分钟K线 ({len(display_data)}条):"]
    lines.append("时间 | 开 | 高 | 低 | 收 | 成交量")
    for k in display_data[-120:]:  # 10分钟线 3 天内最多 120 条
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

def lb_to_futu(symbol: str) -> str:
    """
    将长桥格式代码转换为富途格式代码。
    长桥: 'AAPL.US' / '0700.HK' / '9988.HK'
    富途: 'US.AAPL' / 'HK.00700' / 'HK.09988'

    注意: 富途港股代码固定 5 位，需补零；美股代码直接拼接。
    """
    parts = symbol.rsplit(".", 1)
    if len(parts) != 2:
        return symbol  # 无法识别时原样返回
    ticker, market = parts[0], parts[1].upper()
    if market == "HK":
        ticker = ticker.lstrip("0").zfill(5)   # 去掉多余前导零后补足为 5 位
    return f"{market}.{ticker}"


# ==========================================
# 📡 Step 6: 期权信息
# ==========================================

def fetch_option_data(symbol: str, current_price: float = 0) -> str:
    """
    获取标的的期权信息：本周末/两周后/四周后三个到期日的 ATM 合约深度数据。
    输入 symbol 使用长桥格式，内部自动转换为富途格式。
    """
    try:
        futu_code = lb_to_futu(symbol)  # e.g. "AAPL.US" -> "US.AAPL"
        opt_dates_raw = _logic_get_expiry_dates(futu_code)

        if isinstance(opt_dates_raw, dict) and "error" in opt_dates_raw:
            return f"期权到期日获取失败: {opt_dates_raw['error']}"
        if not isinstance(opt_dates_raw, list) or not opt_dates_raw:
            return "该标的暂无期权数据。"

        today = datetime.now().date()
        # opt_dates_raw 每条包含 strike_time 字段
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

        # 计算目标到期日
        # 今天 days_to_friday：到本周五的天数（最少 1 天）
        days_to_friday = (4 - today.weekday()) % 7 or 7
        this_friday    = today + timedelta(days=days_to_friday)

        # 自适应中期到期日：
        #   如果距本周五 <=3 天（周三/周四/周五），则选“下下周末”，避免前两个日期太贴近
        #   否则（周一/周二）选“下周末”
        next_weeks = 2 if days_to_friday <= 3 else 1
        middle_friday  = today + timedelta(days=days_to_friday + next_weeks * 7)
        four_weeks_out = today + timedelta(days=28)

        def pick_closest(dates, target):
            return min(dates, key=lambda d: abs((d - target).days))

        # 三个目标到期日
        target_dates = [
            (pick_closest(future_dates, this_friday),    "本周末"),
            (pick_closest(future_dates, middle_friday),  "下周末" if next_weeks == 1 else "下下周末"),
            (pick_closest(future_dates, four_weeks_out), "四周后"),
        ]
        seen = set()
        unique_targets = []
        for d, label in target_dates:
            if d not in seen:
                seen.add(d)
                unique_targets.append((d, label))

        # 如果没有现价，先获取
        if current_price <= 0:
            from longbridge_server import _logic_get_live_quote
            quote = _logic_get_live_quote(symbol)
            current_price = quote.get("price", 0)

        # 收集 ATM 合约的 futu_code
        target_symbols: list[str] = []       # futu 期权代码
        date_labels: dict[str, str] = {}     # futu_code -> 标签

        for exp_date, label in unique_targets:
            exp_str = exp_date.strftime("%Y-%m-%d")
            chain = _logic_get_option_chain(futu_code, exp_str)
            if not isinstance(chain, list) or not chain:
                continue

            # 富途链每条有 option_type(“CALL”/“PUT”) + strike_price + futu_code
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

        # 批量查行情（富途主通 → YahooQuery 自动降级）
        opt_quotes = _logic_get_option_snapshots(target_symbols)
        if not isinstance(opt_quotes, list) or not opt_quotes:
            return "期权行情请求返回空。"

        lines = ["🛡️ 期权深度分析 (ATM IV/OI):　[🔗富途行情 | YQ 降级支持]　"]
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

        if len(lines) == 1:
            return "期权行情匹配为空。"

        return "\n".join(lines)

    except Exception as e:
        return f"期权数据暂不可用: {e}"



# ==========================================
# 📡 Step 7: Tavily 资讯
# ==========================================

def fetch_latest_news(symbol: str) -> str:
    """通过 Tavily 拉取最新资讯（盘前用 advanced 模式）"""
    if not TAVILY_API_KEY:
        return "未配置 Tavily API，暂无资讯。"
    try:
        # 提取标的名称用于搜索
        ticker = symbol.split(".")[0]
        res = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": f"{ticker} stock latest news analysis earnings outlook",
                "search_depth": "advanced",
                "include_answer": True,
                "max_results": 5,
            },
            timeout=15,
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
) -> str:
    """将所有盘面数据组装成发给 AI 的完整盘前分析提示词"""

    # 仅美股且有盘前K线数据时插入独立段落
    premarket_section = (
        f"{'='*50}\n"
        f"🌅 **五b、当日美股盘前 5min K 线**\n"
        f"{premarket_kline_str}\n\n"
    ) if premarket_kline_str else ""

    return (
        f"📋 **【盘前谋划数据投喂】** {symbol} ({market}股)\n"
        f"⏰ 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

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
    """将唤醒消息发送给本地 OpenClaw，返回 AI 回复文本"""
    try:
        res = requests.post(
            OPENCLAW_URL,
            headers=OPENCLAW_HEADERS,
            json={
                "model": "openclaw/default",
                "messages": [
                    {"role": "system", "content": PREMARKET_PROMPT},
                    {"role": "user",   "content": wake_msg},
                ],
            },
            timeout=300,  # 盘前分析数据量大，给更多时间
        )
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"]
        tg_send(f"❌ 盘前谋划 AI 调用失败: HTTP {res.status_code}")
        return None
    except Exception as e:
        tg_send(f"❌ 盘前谋划 AI 超时或异常: {e}")
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
    prefix = f"💭 **【盘前策略报告】** {symbol}\n"
    max_safe = 4000  # 内容最大字符数（含前缀不超过 4096）
    if len(prefix) + len(ai_reply) <= max_safe:
        tg_send(prefix + ai_reply)
    else:
        # 分段发送，每次确保 prefix + chunk 不超过 max_safe
        chunk_size = max_safe - len(prefix)
        chunks = [ai_reply[i:i+chunk_size] for i in range(0, len(ai_reply), chunk_size)]
        for i, chunk in enumerate(chunks):
            header = prefix if i == 0 else f"💭 **【盘前策略报告】** {symbol} (续 {i+1})\n"
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
        print(f"  📡 Step 1/7: 获取账户状态...")
        account_str = fetch_account_status(market)
        qty, cost = get_symbol_position(symbol)
        position_str = f"已持仓 {qty}股 (成本 ${cost:.2f})" if qty > 0 else "空仓"

        # Step 2: 市场温度
        print(f"  📡 Step 2/7: 获取市场温度...")
        temp_str = fetch_market_temperature(market)

        # Step 3: 标的基本信息
        print(f"  📡 Step 3/7: 获取标的基本信息...")
        static_str = fetch_static_info(symbol)

        # Step 4: 60日日K线
        print(f"  📡 Step 4/7: 获取60日日K线...")
        daily_kline_str = fetch_daily_kline(symbol)

        # Step 5: 3日10分钟K线
        print(f"  📡 Step 5/7: 获取短周期K线...")
        min_kline_str = fetch_min10_kline(symbol)

        # Step 5b: 美股当日盘前 5min K线（仅美股）
        premarket_kline_str = ""
        if market == "US":
            print(f"  📡 Step 5b: 获取美股盘前 5min K 线...")
            premarket_kline_str = fetch_premarket_kline_us(symbol)

        # Step 6: 期权数据
        print(f"  📡 Step 6/8: 获取期权数据...")
        # 富途 API 支持港股和美股期权，统一调用；内部已有完整容错
        option_str = fetch_option_data(symbol)

        # Step 7: 最新资讯
        print(f"  📡 Step 7/8: 获取最新资讯...")
        news_str = fetch_latest_news(symbol)

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
        tg_send(f"⏳ **【盘前谋划】** {market_name} · 正在分析 {symbol}...")
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
        f"{market_flag} **【盘前谋划启动】** {market_name} | {start_time.strftime('%H:%M')}"
        f"\n标的列表: {', '.join(symbols)}"
        f"\n预计耗时: ~{len(symbols) * 5} 分钟 (每标的间隔5分钟)"
    )

    try:
        for i, symbol in enumerate(symbols):
            process_single_symbol(symbol, market, market_name)

            # Step 11: 标的间隔（最后一个不需要等）
            if i < len(symbols) - 1:
                print(f"\n⏳ 标的间隔冷却 {SYMBOL_INTERVAL // 60} 分钟...")
                time.sleep(SYMBOL_INTERVAL)

        elapsed = int((datetime.now() - start_time).total_seconds() / 60)
        tg_send(f"✅ **【盘前谋划完成】** {market_name} │ 共 {len(symbols)} 只标的 │ 耗时 {elapsed} 分钟")
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

    schedule.every().monday.at("20:30").do(run_premarket_batch, market="US")
    schedule.every().tuesday.at("20:30").do(run_premarket_batch, market="US")
    schedule.every().wednesday.at("20:30").do(run_premarket_batch, market="US")
    schedule.every().thursday.at("20:30").do(run_premarket_batch, market="US")
    schedule.every().friday.at("20:30").do(run_premarket_batch, market="US")

    # tg_send("🌅 盘前谋划调度器已上线，等待触发时间...")

    # 循环检查 schedule
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
