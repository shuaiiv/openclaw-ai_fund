# 文件名: longbridge_server.py (部署在 VPS 上 For-OpenClaw/longbridge/ 目录，供 OpenClaw 的 strategies 层调用)
import os
import sys
import json
import logging
from datetime import datetime, timedelta, date
import requests
import pandas as pd
from yahooquery import Ticker
import re
from decimal import Decimal
from longbridge.openapi import (
    TradeContext, 
    SubType, 
    PushQuote, 
    PushOrderChanged, 
    TopicType,
    OrderType, 
    OrderSide, 
    TimeInForceType
)
# 创建标准请求会话（不依赖 curl_cffi）
safe_session = requests.Session()
safe_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
})

from dotenv import load_dotenv, find_dotenv

# 2. ⚡️ 将 find_dotenv() 作为参数传给 load_dotenv()
load_dotenv(find_dotenv())




# ==========================================
# 📲 Telegram 推送
# ==========================================

# 替换为你实际的 TG Bot Token 和你的 Chat ID
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN_QUANT")
TG_CHAT_ID = os.getenv("TG_CHAT_ID") 

def send_tg_notification(text):
    """底层直连 TG 发送实时通知"""
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print(f"TG Push Failed: {e}")

# ==========================================
# 🔥 全局内存状态机 (接收 WebSocket 推送)
# ==========================================
_LIVE_CACHE = {
    "quotes": {},
    "orders": {}
}

def on_quote(symbol: str, event: PushQuote):
    """ 长桥底层回调：当订阅的股票有实时行情时触发 """
    _LIVE_CACHE["quotes"][symbol] = {
        "last_done": str(event.last_done),
        "timestamp": event.timestamp,
        "updated_at": "Just Now"
    }

def on_order_changed(order_event):
    """当订单状态发生变化时（长桥主动推送）"""
    _LIVE_CACHE["orders"][order_event.order_id] = {
        "symbol": order_event.symbol,
        "status": order_event.status,
        "executed_qty": str(order_event.executed_quantity)
    }
    
    status_str = str(order_event.status)
    
    # 将长桥的英文状态翻译成中文，方便阅读
    status_cn = status_str
    if "New" in status_str or "Submitted" in status_str:
        status_cn = "🟡 已挂单 (待成交)"
    elif "Filled" in status_str:
        status_cn = "🟢 已完全成交"
    elif "Canceled" in status_str:
        status_cn = "⚪️ 已撤单"
    elif "Rejected" in status_str:
        status_cn = "🔴 订单被拒"
        
    # 只要状态是上述几种，全部推送！
    if status_cn != status_str: 
        msg = (
            f"🔔 **【底层订单雷达】**\n"
            f"标的: {order_event.symbol}\n"
            f"状态: {status_cn}\n"
            f"成交数量: {order_event.executed_quantity}\n"
            f"订单价格: {order_event.price}\n"
            f"*(耗时 0.01 秒，由底层直推)*"
        )
        send_tg_notification(msg) # 调用你前面写的发送函数

# ==========================================
# 🔇 环境配置
# ==========================================
try:
    sys.stdout.reconfigure(encoding='utf-8')
except:
    pass

os.environ["LONGBRIDGE_LOG_LEVEL"] = "error"
logging.basicConfig(level=logging.CRITICAL, stream=sys.stderr)

from fastmcp import FastMCP
from longbridge.openapi import Config, QuoteContext, Period, AdjustType, CalcIndex, Market

mcp = FastMCP("longbridge-official")

# 全局缓存（供 get_full_analysis_report → save_analysis_to_file 流水线共享）
_DATA_CACHE = {}

# ==========================================
# 📐 模块级常量
# ==========================================

# K 线周期字符串 → SDK 枚举映射（全局唯一定义，避免各工具函数重复声明）
PERIOD_MAP = {
    "1min": Period.Min_1, "5min": Period.Min_5, "15min": Period.Min_15,
    "30min": Period.Min_30, "60min": Period.Min_60,
    "day": Period.Day, "week": Period.Week, "month": Period.Month, "year": Period.Year
}

# 市场字符串 → SDK 枚举映射
MARKET_MAP = {"US": Market.US, "HK": Market.HK}

# CalcIndex 全量指标字典（零 API 调用纯参考，供 AI 选择指标时查阅）
# 基于长桥官方文档 objects.md → CalcIndex 枚举定义
CALC_INDEX_CATALOG = [
    {"id": 1,  "name": "LastDone",           "description": "最新价",                       "scope": "全部证券"},
    {"id": 2,  "name": "ChangeVal",          "description": "涨跌额",                       "scope": "全部证券"},
    {"id": 3,  "name": "ChangeRate",         "description": "涨跌幅 (比率字段，不含%)",      "scope": "全部证券"},
    {"id": 4,  "name": "Volume",             "description": "成交量",                       "scope": "全部证券"},
    {"id": 5,  "name": "Turnover",           "description": "成交额",                       "scope": "全部证券"},
    {"id": 6,  "name": "YtdChangeRate",      "description": "年初至今涨跌幅",                "scope": "股票/指数"},
    {"id": 7,  "name": "TurnoverRate",       "description": "换手率",                       "scope": "股票/指数"},
    {"id": 8,  "name": "TotalMarketValue",   "description": "总市值",                       "scope": "股票/指数"},
    {"id": 9,  "name": "CapitalFlow",        "description": "资金流向",                     "scope": "股票/指数"},
    {"id": 10, "name": "Amplitude",          "description": "振幅",                         "scope": "股票/指数"},
    {"id": 11, "name": "VolumeRatio",        "description": "量比",                         "scope": "股票/指数"},
    {"id": 12, "name": "PeTtmRatio",         "description": "市盈率 (TTM)",                 "scope": "股票/指数"},
    {"id": 13, "name": "PbRatio",            "description": "市净率",                       "scope": "股票/指数"},
    {"id": 14, "name": "DividendRatioTtm",   "description": "股息率 (TTM)",                 "scope": "股票/指数"},
    {"id": 15, "name": "FiveDayChangeRate",  "description": "五日涨跌幅",                   "scope": "股票/指数"},
    {"id": 16, "name": "TenDayChangeRate",   "description": "十日涨跌幅",                   "scope": "股票/指数"},
    {"id": 17, "name": "HalfYearChangeRate", "description": "半年涨跌幅",                   "scope": "股票/指数"},
    {"id": 18, "name": "FiveMinutesChangeRate", "description": "五分钟涨跌幅",              "scope": "股票/指数"},
    {"id": 19, "name": "ExpiryDate",         "description": "到期日",                       "scope": "期权/窝轮"},
    {"id": 20, "name": "StrikePrice",        "description": "行权价",                       "scope": "期权/窝轮"},
    {"id": 21, "name": "UpperStrikePrice",   "description": "上限价",                       "scope": "仅窝轮"},
    {"id": 22, "name": "LowerStrikePrice",   "description": "下限价",                       "scope": "仅窝轮"},
    {"id": 23, "name": "OutstandingQty",     "description": "街货量",                       "scope": "仅窝轮"},
    {"id": 24, "name": "OutstandingRatio",   "description": "街货比",                       "scope": "仅窝轮"},
    {"id": 25, "name": "Premium",            "description": "溢价",                         "scope": "期权/窝轮"},
    {"id": 26, "name": "ItmOtm",             "description": "价内/价外",                    "scope": "仅窝轮"},
    {"id": 27, "name": "ImpliedVolatility",  "description": "隐含波动率",                   "scope": "期权/窝轮"},
    {"id": 28, "name": "WarrantDelta",       "description": "窝轮 Delta",                   "scope": "仅窝轮"},
    {"id": 29, "name": "CallPrice",          "description": "收回价",                       "scope": "仅窝轮"},
    {"id": 30, "name": "ToCallPrice",        "description": "距收回价 (%)",                 "scope": "仅窝轮"},
    {"id": 31, "name": "EffectiveLeverage",  "description": "有效杠杆",                     "scope": "仅窝轮"},
    {"id": 32, "name": "LeverageRatio",      "description": "杠杆比率",                     "scope": "仅窝轮"},
    {"id": 33, "name": "ConversionRatio",    "description": "换股比率",                     "scope": "仅窝轮"},
    {"id": 34, "name": "BalancePoint",       "description": "打和点",                       "scope": "仅窝轮"},
    {"id": 35, "name": "OpenInterest",       "description": "未平仓合约数",                 "scope": "仅期权"},
    {"id": 36, "name": "Delta",              "description": "Delta",                        "scope": "仅期权"},
    {"id": 37, "name": "Gamma",              "description": "Gamma",                        "scope": "仅期权"},
    {"id": 38, "name": "Theta",              "description": "Theta",                        "scope": "仅期权"},
    {"id": 39, "name": "Vega",               "description": "Vega",                         "scope": "仅期权"},
    {"id": 40, "name": "Rho",                "description": "Rho",                          "scope": "仅期权"},
]

# CalcIndex 名称 → 枚举映射，用于 get_financial_indexes 的自定义指标支持
CALC_INDEX_NAME_MAP = {item["name"]: getattr(CalcIndex, item["name"], None) for item in CALC_INDEX_CATALOG}

# 文档中明确列出的 SecurityCalcIndex 响应对象字段名（拉取/calc-index.md）
# 直接按字段名读取，不需要通过 CalcIndex 枚举反查属性名
_CALC_RESPONSE_FIELDS = [
    "last_done", "change_val", "change_rate", "volume", "turnover",
    "ytd_change_rate", "turnover_rate", "total_market_value", "capital_flow",
    "amplitude", "volume_ratio", "pe_ttm_ratio", "pb_ratio", "dividend_ratio_ttm",
    "five_day_change_rate", "ten_day_change_rate", "half_year_change_rate",
    "five_minutes_change_rate", "expiry_date", "strike_price",
    "upper_strike_price", "lower_strike_price", "outstanding_qty",
    "outstanding_ratio", "premium", "itm_otm", "implied_volatility",
    "warrant_delta", "call_price", "to_call_price", "effective_leverage",
    "leverage_ratio", "conversion_ratio", "balance_point",
    "open_interest", "delta", "gamma", "theta", "vega", "rho",
]

# 默认估值指标集（无参调用 get_financial_indexes 时使用）
_DEFAULT_CALC_INDEXES = [
    CalcIndex.TotalMarketValue,
    CalcIndex.PeTtmRatio,
    CalcIndex.PbRatio,
    CalcIndex.DividendRatioTtm,
    CalcIndex.TurnoverRate,
    CalcIndex.VolumeRatio,
]

# ==========================================
# 1. 基础配置
# ==========================================
# ==========================================
APP_KEY      = os.getenv("LONGBRIDGE_APP_KEY")
APP_SECRET   = os.getenv("LONGBRIDGE_APP_SECRET")
ACCESS_TOKEN = os.getenv("LONGBRIDGE_ACCESS_TOKEN")

def get_lb_config():
    """
    创建长桥 Config 对象（新版 SDK API）。
    新版 SDK 使用工厂方法而非构造函数：
      - Config.from_apikey_env()                         # 直接读 LONGBRIDGE_* 环境变量
      - Config.from_apikey(app_key, app_secret, token)   # 传入显式凭证（当前用法）
    """
    if not all([APP_KEY, APP_SECRET, ACCESS_TOKEN]):
        raise ValueError(
            "长桥 API 凭证未配置，请检查 .env 中的 "
            "LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN"
        )
    return Config.from_apikey(APP_KEY, APP_SECRET, ACCESS_TOKEN)


# ==========================================
# 🔥 全局单例 Context 初始化 (用于维持 WebSocket 长连接)
# 这一步会在脚本启动时自动执行，并一直保持后台连接
# ==========================================
# 预赋值为 None，确保即使初始化失败 get_ctx() 也能给出有意义的错误，而非 NameError
quote_ctx = None
trade_ctx = None

# ==========================================
# 🔌 全局 Context 懒初始化（Lazy Init）
# ==========================================
# 不在模块加载时建立 WebSocket 长连接，避免 import 时不必要的网络操作和权限检查。
# 第一次调用 get_ctx() / get_trade_ctx() 时才真正连接，并将实例缓存供后续复用。
# ==========================================
quote_ctx  = None
trade_ctx  = None
_init_error: str = ""   # 记录初始化失败时的真实原因


def _init_contexts():
    """懒初始化：仅在首次调用时建立长连接，异常时记录真实错误信息。"""
    global quote_ctx, trade_ctx, _init_error
    if quote_ctx is not None:
        return  # 已初始化，直接返回

    import traceback
    try:
        cfg = get_lb_config()

        # 1. 行情 Context（绑定实时行情回调）
        quote_ctx = QuoteContext(cfg)
        quote_ctx.set_on_quote(on_quote)

        # 2. 交易 Context（绑定订单回调，订阅私人推送）
        trade_ctx = TradeContext(cfg)
        trade_ctx.set_on_order_changed(on_order_changed)
        trade_ctx.subscribe([TopicType.Private])

        _init_error = ""
        print("✅ LongBridge Contexts 初始化成功", flush=True)

    except Exception as e:
        _init_error = traceback.format_exc()
        quote_ctx  = None
        trade_ctx  = None
        # 直接 print，不依赖 logging 级别过滤
        print(f"❌ LongBridge Contexts 初始化失败:\n{_init_error}", flush=True)


def get_ctx() -> QuoteContext:
    """获取行情 Context（懒初始化，首次调用时建立连接）"""
    _init_contexts()
    if quote_ctx is None:
        raise RuntimeError(
            f"LongBridge QuoteContext 初始化失败，真实原因:\n{_init_error}"
        )
    return quote_ctx


def get_trade_ctx() -> TradeContext:
    """获取交易 Context（懒初始化，首次调用时建立连接）"""
    _init_contexts()
    if trade_ctx is None:
        raise RuntimeError(
            f"LongBridge TradeContext 初始化失败，真实原因:\n{_init_error}"
        )
    return trade_ctx


# ==========================================
# 2. 核心逻辑层 (纯 Python 函数，只返回 Dict/List)
# ==========================================

def _logic_get_live_quote(symbol: str):
    try:
        ctx = get_ctx()
        q_list = ctx.quote([symbol])
        if not q_list: return {"error": f"No quote for {symbol}"}
        q = q_list[0]
        return {
            "symbol": symbol,
            "price": float(q.last_done),
            "open": float(q.open),
            "high": float(q.high),
            "low": float(q.low),
            "vol": int(q.volume),
            "timestamp": str(q.timestamp),
            "change_rate": f"{(float(q.last_done)-float(q.prev_close))/float(q.prev_close)*100:+.2f}%" if float(q.prev_close) > 0 else "N/A"
        }
    except Exception as e:
        return {"error": str(e)}


def _logic_get_extended_quote(symbol: str) -> dict:
    """
    获取含盘前/盘后/夜盘的最新价格（主要用于美股延伸时段）。

    通过对比各时段的 timestamp，选出绝对最新的成交价，规避
    「last_done 停留在常规盘收盘价」的问题。

    返回字段:
        price      : 时间戳最新的那个时段成交价（已做兜底：停牌时退回 prev_close）
        session    : 来源时段名称（"regular" / "pre_market" / "post_market" / "over_night"）
        prev_close : 昨日收盘价
        error      : 异常时返回错误描述
    """
    def _ts(dt_obj) -> float:
        """将 datetime 或数值统一转为浮点时间戳"""
        return dt_obj.timestamp() if hasattr(dt_obj, "timestamp") else float(dt_obj)

    try:
        ctx = get_ctx()
        q_list = ctx.quote([symbol])
        if not q_list:
            return {"error": f"No quote for {symbol}"}
        q = q_list[0]

        prev_close = float(q.prev_close) if q.prev_close else 0.0

        # 收集各时段 (price, timestamp, session_name)
        candidates: list[tuple[float, float, str]] = []

        if q.last_done and float(q.last_done) > 0 and q.timestamp:
            candidates.append((float(q.last_done), _ts(q.timestamp), "regular"))

        if getattr(q, "pre_market_quote", None):
            pmq = q.pre_market_quote
            if pmq and float(pmq.last_done) > 0 and pmq.timestamp:
                candidates.append((float(pmq.last_done), _ts(pmq.timestamp), "pre_market"))

        if getattr(q, "post_market_quote", None):
            poq = q.post_market_quote
            if poq and float(poq.last_done) > 0 and poq.timestamp:
                candidates.append((float(poq.last_done), _ts(poq.timestamp), "post_market"))

        if getattr(q, "over_night_quote", None):
            onq = q.over_night_quote
            if onq and float(onq.last_done) > 0 and onq.timestamp:
                candidates.append((float(onq.last_done), _ts(onq.timestamp), "over_night"))

        if candidates:
            # 按时间戳升序取最后一个（最新）
            candidates.sort(key=lambda x: x[1])
            best_price, _, best_session = candidates[-1]
        else:
            # 全部为 0（停牌）→ 退回昨日收盘价
            best_price  = prev_close
            best_session = "prev_close_fallback"

        return {
            "symbol":     symbol,
            "price":      best_price,
            "session":    best_session,
            "prev_close": prev_close,
        }
    except Exception as e:
        return {"error": str(e)}


# SDK 枚举类型转换工具：将 Rust native 类型转为字符串，避免 JSON 序列化失败
def _sdk_val(v):
    """如果 v 是基础 Python 类型（str/int/float/bool/None）则直接返回，否则转为 str"""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _logic_get_static_info(symbol: str):
    try:
        ctx = get_ctx()
        i_list = ctx.static_info([symbol])
        if not i_list: return {}
        i = i_list[0]
        total_shares = getattr(i, 'total_shares', 'N/A')
        return {
            "name":         getattr(i, 'name_cn', ''),
            "board":        _sdk_val(getattr(i, 'board', '')),    # SecurityBoard 枚举 → str
            "currency":     getattr(i, 'currency', ''),
            "lot_size":     getattr(i, 'lot_size', 0),
            "total_shares": total_shares,
        }
    except Exception as e:
        return {"error": str(e)}

def _logic_get_financial_indexes(symbol: str, index_names: list[str] = None):
    """
    获取标的的计算指标（PE/PB/市值/股息率/换手率/量比）。
    字段名来自官方文档: 拉取/calc-index.md。
    """
    try:
        ctx = get_ctx()
        if index_names:
            target_indexes = [
                v for n in index_names
                if (v := CALC_INDEX_NAME_MAP.get(n)) is not None
            ]
            if not target_indexes:
                return {"error": "没有找到有效的指标名称"}
        else:
            target_indexes = _DEFAULT_CALC_INDEXES

        idxs = ctx.calc_indexes([symbol], target_indexes)
        if not idxs:
            return {}
        idx = idxs[0]

        # 文档已明确列出 SecurityCalcIndex 的所有字段名，直接读取即可
        # 不需要任何 CalcIndex 枚举反查（避开不可 hash/无 .name 问题）
        result = {}
        for field in _CALC_RESPONSE_FIELDS:
            val = getattr(idx, field, None)
            if val is not None:
                result[field] = str(val)
        return result
    except Exception as e:
        return {"error": f"Finance API Error: {str(e)}"}

def _logic_get_capital_distribution(symbol: str) -> dict:
    """
    获取标的当日资金流向分布（大/中/小单三档）。

    根据官方文档 capital-distribution.md，字段包含 large/medium/small 三级分类。
    """
    try:
        ctx = get_ctx()
        d = ctx.capital_distribution(symbol)
        return {
            "in_large":   float(d.capital_in.large),
            "in_medium":  float(d.capital_in.medium),
            "in_small":   float(d.capital_in.small),
            "out_large":  float(d.capital_out.large),
            "out_medium": float(d.capital_out.medium),
            "out_small":  float(d.capital_out.small),
        }
    except Exception as e:
        return {"error": str(e)}

def _logic_get_market_temperature(market: str):
    """获取指定市场的实时温度指数。"""
    try:
        ctx = get_ctx()
        m = MARKET_MAP.get(market.upper(), Market.HK)
        t = ctx.market_temperature(m)
        return {
            "temp": t.temperature,
            "desc": t.description,
            "val": t.valuation,
            "sent": t.sentiment
        }
    except Exception as e:
        logging.warning(f"Market temperature fetch failed: {e}")
        return {"error": f"Temp fetch failed: {str(e)}"}

# 🔥 重构后的通用 K 线获取逻辑
def _logic_get_history_kline(symbol: str, period: Period, start_date: date, end_date: date):
    """
    通用 K 线获取函数 (V6.6 支持自动翻页突破 1000 根限制)
    """
    try:
        ctx = get_ctx()
        # 只有分钟线容易超限 (1000根)，日线一年才250根
        is_minute = period in [Period.Min_1, Period.Min_5, Period.Min_10, Period.Min_15, Period.Min_30, Period.Min_60]
        
        all_data = []
        current_end = end_date
        
        while current_end >= start_date:
            raw_k = ctx.history_candlesticks_by_date(symbol, period, AdjustType.ForwardAdjust, start_date, current_end)
            if not raw_k:
                break
                
            # 格式化这批数据
            chunk = []
            for k in raw_k:
                t_str = k.timestamp.strftime("%Y-%m-%d %H:%M") if is_minute else k.timestamp.strftime("%Y-%m-%d")
                chunk.append({
                    "t": t_str, "o": float(k.open), "h": float(k.high), "l": float(k.low), "c": float(k.close), "v": int(k.volume)
                })
                
            all_data = chunk + all_data # 拼接到前面
            
            # 🔥 如果拉满了 1000 根，说明前面还有数据，需要更新 current_end 往前继续拉
            if len(raw_k) == 1000 and is_minute:
                oldest_time = raw_k[0].timestamp
                # 把结束日期设为当前这批最老的一天的前一天
                current_end = oldest_time.date() - timedelta(days=1)
            else:
                break # 没到 1000 根说明拉完了
                
        # 按时间戳去重并排序 (防止拼接处重复)
        unique_data = {item['t']: item for item in all_data}
        sorted_data = list(unique_data.values())
        sorted_data.sort(key=lambda x: x['t'])
        
        return sorted_data
    except Exception as e:
        return [{"error": str(e)}]

# ==========================================
# 🥇 核心逻辑层：期权数据 (长桥主导 + YahooQuery 容灾降级)
# ==========================================

# 【主通道：长桥官方】获取期权链的所有到期日
def _logic_get_option_expiry_dates(symbol: str):
    try:
        ctx = get_ctx()
        dates = ctx.option_chain_expiry_date_list(symbol)
        return list(dates) if dates else []
    except Exception as e:
        return {"error": f"Failed to get expiry dates: {str(e)}"}



def _logic_get_option_chain_by_date(symbol: str, expiry_date: str):
    """【主通道：长桥官方】获取指定到期日的期权合约列表"""
    try:
        ctx = get_ctx()
        clean_date = expiry_date.replace("-", "")
        target_date = datetime.strptime(clean_date, "%Y%m%d").date()
        
        chain = ctx.option_chain_info_by_date(symbol, target_date)
        if not chain: return []
        
        result = []
        for o in chain:
            result.append({
                "strike_price": float(o.price),
                "call_symbol": o.call_symbol,
                "put_symbol": o.put_symbol,
                "is_standard": o.standard
            })
        return result
    except Exception as e:
        return {"error": f"Failed to get option chain: {str(e)}"}

def _logic_get_option_quotes(symbols: list):
    """
    【智能路由网关】获取深度行情
    尝试走长桥官方，报错则无缝降级到 YahooQuery 通道
    """
    try:
        ctx = get_ctx()
        quotes = ctx.option_quote(symbols)
        if not quotes: return []
        
        result = []
        for q in quotes:
            # 文档确认 option_extend 是正确的嵌套对象 (option-quote.md)
            # 子字段: implied_volatility, open_interest, expiry_date,
            #         strike_price, direction (C/P), contract_type 等
            ext = getattr(q, 'option_extend', None)
            iv  = str(getattr(ext, 'implied_volatility', 'N/A')) if ext else 'N/A'
            oi  = int(getattr(ext, 'open_interest',      0))     if ext else 'N/A'
            sp  = str(getattr(ext, 'strike_price',       'N/A')) if ext else 'N/A'
            exp = str(getattr(ext, 'expiry_date',        'N/A')) if ext else 'N/A'
            direction = getattr(ext, 'direction', '')             if ext else ''
            result.append({
                "symbol":             q.symbol,
                "last_done":          float(q.last_done),
                "volume":             int(q.volume),
                "open_interest":      oi,
                "implied_volatility": float(iv) if iv != 'N/A' else None,
                "strike_price":       sp,
                "direction":          "Call" if direction == "C" else "Put",
                "expiry_date":        exp,
                "_source":            "Longbridge Real-time"
            })
        return result
    except Exception as e:
        error_msg = str(e)
        # 触发容灾：如果没有行情权限 (301604)
        if "301604" in error_msg or "no quote access" in error_msg.lower():
            return _fallback_yahooquery_option_quotes(symbols)
        else:
            return {"error": f"Failed to get option quotes: {error_msg}"}

# ==========================================
# 🥷 隐秘翻译器 (YahooQuery 容灾实现)
# ==========================================

def _fallback_yahooquery_option_quotes(symbols: list):
    # 解析长桥代码 -> YahooQuery 拉链 -> 过滤提取补全
    try:
        if not symbols: return []
        
        # 1. 拆解第一个长桥代码 (如 AAPL260320C242500.US)
        first_symbol = symbols[0]
        match = re.match(r'^([A-Z]+)(\d{6})([CP])(\d+)\.US$', first_symbol)
        if not match: return {"error": f"Unparseable symbol: {first_symbol}"}
            
        underlying, date_str, _, _ = match.groups()
        # 转换长桥日期 260320 -> 雅虎 2026-03-20
        target_date = f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:]}"
        
        # 2. 召唤 YahooQuery
        t = Ticker(underlying)
        options = t.option_chain
        if isinstance(options, str) or options.empty:
            return []
            
        df = options.reset_index()
        df['exp_str'] = df['expiration'].astype(str).str.slice(0, 10)
        day_df = df[df['exp_str'] == target_date]
        
        # 3. 精准匹配并伪装返回
        result = []
        for sym in symbols:
            smatch = re.match(r'^([A-Z]+)(\d{6})([CP])(\d+)\.US$', sym)
            if not smatch: continue
            
            _, _, opt_type, strike_str = smatch.groups()
            direction = "Call" if opt_type == "C" else "Put"
            yq_opt_type = 'calls' if direction == "Call" else 'puts'
            
            # 长桥的 strike 乘了 1000，如 242500 -> 242.5
            strike_price = float(strike_str) / 1000.0
            
            # 在 YQ 的 DataFrame 里寻找行权价和方向一致的合约
            matched_row = day_df[(day_df['optionType'] == yq_opt_type) & (day_df['strike'].round(2) == round(strike_price, 2))]
            
            if not matched_row.empty:
                row = matched_row.iloc[0]
                result.append({
                    "symbol": sym, # 继续用长桥的马甲
                    "last_done": float(row.get('lastPrice', 0)),
                    "volume": int(row.get('volume', 0) if pd.notna(row.get('volume')) else 0),
                    "open_interest": int(row.get('openInterest', 0) if pd.notna(row.get('openInterest')) else 0),
                    "implied_volatility": float(row.get('impliedVolatility', 0) if pd.notna(row.get('impliedVolatility')) else 0),
                    "strike_price": str(strike_price),
                    "direction": direction,
                    "expiry_date": date_str,
                    "_source": "YahooQuery Fallback" # 标记来源
                })
        return result
    except Exception as e:
        return {"error": f"YahooQuery Fallback Error: {str(e)}"}

# ==========================================
# 🛑 订单状态管理
# ==========================================

# 撤销订单
def cancel_order_by_id(order_id: str):
    """
    撤销指定订单
    """
    try:
        trade_ctx = get_trade_ctx()
        trade_ctx.cancel_order(order_id)
        return {"status": "SUCCESS"}
    except Exception as e:
        print(f"⚠️ 撤单异常: {e}")
        return {"error": str(e)}

def _logic_get_market_temperature_history(market: str = "HK", days: int = 30):
    try:
        ctx = get_ctx()
        m_map = {"US": Market.US, "HK": Market.HK}
        target = m_map.get(market.upper(), Market.HK)
        end = datetime.now().date()
        start = (datetime.now() - timedelta(days=days)).date()
        resp = ctx.history_market_temperature(target, start, end)
        res = []
        if hasattr(resp, 'records'):
            for item in resp.records:
                res.append({
                    "date": item.timestamp.strftime("%Y-%m-%d"),
                    "temp": item.temperature,
                })
        return res
    except Exception as e:
        return {"error": str(e)}

# 查询订单状态
def get_order_status_by_id(order_id: str):
    """
    极速查询特定订单的状态。
    返回示例: "NewStatus", "SubmittedStatus", "FilledStatus", "CanceledStatus" 等
    """
    try:
        trade_ctx = get_trade_ctx()
        # 优先在今日订单中寻找 (速度最快)
        today_orders = trade_ctx.today_orders()
        if today_orders:
            for order in today_orders:
                if order.order_id == order_id:
                    return str(order.status).replace('OrderStatus.', '')
        
        # 如果今日订单里没有，去近 7 天历史订单里找（加日期范围防止全量拉取）
        history_orders = trade_ctx.history_orders(
            start_at=datetime.now() - timedelta(days=7),
            end_at=datetime.now()
        )
        if history_orders:
            for order in history_orders:
                if order.order_id == order_id:
                    return str(order.status).replace('OrderStatus.', '')
                    
        return "Unknown"
    except Exception as e:
        print(f"⚠️ 查询订单状态异常: {e}")
        return "Error"

# ==========================================
# 3. 接口层 (MCP Tools - 纯数据模式)
# ==========================================

@mcp.tool()
def get_live_quote(symbol: str):
    """获取实时行情 (返回 JSON 数据)。"""
    data = _logic_get_live_quote(symbol)
    return {
        "data": data,
        "_ai_instruction": "请将此行情数据格式化为易读的卡片，包含价格、涨跌幅(用红/绿色区分)、成交量。"
    }

@mcp.tool()
def get_static_info(symbol: str):
    """获取基础档案 (返回 JSON 数据)。"""
    data = _logic_get_static_info(symbol)
    return {
        "data": data,
        "_ai_instruction": "请展示该股票的基础信息，如名称、板块、每手股数、总股本等。"
    }

@mcp.tool()
def get_financial_indexes(symbol: str, indexes: list[str] = None):
    """
    获取指定标的的计算指标（如市盈率、市值等）。
    - indexes: 可选，指标名称列表，如 ["PeTtmRatio", "PbRatio", "TotalMarketValue"]。
              不传则使用默认的 6 个核心估值指标。
              可通过 get_calc_index_dictionary 查看全部可用指标。
    """
    data = _logic_get_financial_indexes(symbol, indexes)
    return {
        "data": data,
        "_ai_instruction": "请分析该股票的估值状态(PE/PB)及其他已获取的指标，并与行业平均水平进行简单的对比(如果知道的话)。"
    }

@mcp.tool()
def get_capital_distribution(symbol: str):
    """获取资金分布 (返回 JSON 数据)。"""
    data = _logic_get_capital_distribution(symbol)
    return {
        "data": data,
        "_ai_instruction": "请计算主力净流入(大单入-大单出)，并用 Emoji (🟢/🔴) 标示资金情绪是流入还是流出。"
    }

@mcp.tool()
def get_market_temperature(market: str = "HK"):
    """获取市场温度 (返回 JSON 数据)。"""
    data = _logic_get_market_temperature(market)
    return {
        "data": data,
        "_ai_instruction": "请生成一个【市场温度仪表盘】。使用 Emoji (🌡️/❄️/🔥) 形象化展示温度，并对情绪和估值进行简短点评。"
    }

@mcp.tool()
def get_market_temperature_history(market: str = "HK", days: int = 30):
    """获取历史市场温度 (返回 JSON 数据)。"""
    try:
        ctx = get_ctx()
        m_map = {"US": Market.US, "HK": Market.HK}
        target = m_map.get(market.upper(), Market.HK)
        end = datetime.now().date()
        start = (datetime.now() - timedelta(days=days)).date()
        resp = ctx.history_market_temperature(target, start, end)
        res = []
        if hasattr(resp, 'records'):
            for item in resp.records:
                res.append({
                    "date": item.timestamp.strftime("%Y-%m-%d"),
                    "temp": item.temperature
                })
        return {
            "history_data": res,
            "_ai_instruction": "请根据历史数据绘制趋势描述，或者如果支持的话，尝试用 ASCII 图表展示温度变化。"
        }
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

# 🔥 恢复并重构：历史 K 线查询工具
@mcp.tool()
def get_history_candlesticks(symbol: str, period: str = "day", start: str = None, end: str = None):
    """
    获取指定时间范围的历史 K 线数据。
    【⚠️ 极其重要的使用规范】：
    1. 你必须克制地获取数据，避免 Token 超载！
    2. 对于日K线 (day)，如果为了分析短期支撑阻力，请务必传入近 1 到 3 个月的 start 和 end 日期。
    3. 对于分钟K线 (1min~60min)，数据量极大！你必须严格传入当天的日期，或最多近 3 天的 start 和 end 日期，否则会导致系统崩溃！

    参数:
    - symbol: 股票代码 (e.g., "0700.HK")
    - period: 周期，可选值 "1min", "5min", "15min", "30min", "60min", "day" (日K), "week" (周K), "month" (月K), "year" (年K)。默认为 "day"。
    - start: 开始日期 (格式 "YYYY-MM-DD")。
    - end: 结束日期 (格式 "YYYY-MM-DD")。
    """
    try:
        target_period = PERIOD_MAP.get(period.lower(), Period.Day)

        # 2. 参数处理：日期
        if not end:
            end_d = datetime.now().date()
        else:
            end_d = datetime.strptime(end, "%Y-%m-%d").date()
            
        if not start:
            # 🚨 核心改动：如果 AI 偷懒没传 start，默认只给它近 30 天的，而不是 365 天！
            # 如果是分钟线，默认只给近 3 天！
            days_to_subtract = 3 if "min" in period.lower() else 30
            start_d = end_d - timedelta(days=days_to_subtract)
        else:
            start_d = datetime.strptime(start, "%Y-%m-%d").date()

        # 3. 调用统一的逻辑层函数
        data = _logic_get_history_kline(symbol, target_period, start_d, end_d)
        
        # 4. 后处理：计算极值，方便 AI 回答
        high_val = -float('inf')
        low_val = float('inf')
        high_date = ""
        low_date = ""
        
        for item in data:
            if "error" in item: continue
            if item['h'] > high_val:
                high_val = item['h']
                high_date = item['t']
            if item['l'] < low_val:
                low_val = item['l']
                low_date = item['t']

        return {
            "symbol": symbol,
            "period": period,
            "range": f"{start_d} to {end_d}",
            "summary": {
                "highest": {"price": high_val, "date": high_date} if high_val != -float('inf') else None,
                "lowest": {"price": low_val, "date": low_date} if low_val != float('inf') else None,
                "count": len(data)
            },
            "k_line_data": data, 
            "_ai_instruction": f"请根据 K 线数据回答用户问题。如需分析趋势，请参考 summary 中的最高/最低点。如果数据量较大，无需列出所有数据，只需总结关键走势。"
        }

    except Exception as e:
        return {"error": str(e)}

# 📈 期权分析工具箱

@mcp.tool()
def get_option_expiry_dates(symbol: str):
    """
    获取指定股票（如 'AAPL.US'）的期权链所有到期日。
    进行期权分析的第一步，先拿到哪些日期可以交易。
    """
    data = _logic_get_option_expiry_dates(symbol)
    return {
        "data": data,
        "_ai_instruction": "请列出最近的几个关键到期日，并询问用户想查看哪一天的期权链。"
    }

@mcp.tool()
def get_option_chain_by_date(symbol: str, expiry_date: str):
    """
    获取指定股票在特定到期日（如 '20240119' 或 '2024-01-19'）的期权合约列表（包含行权价和对应的 Call/Put 代码）。
    """
    data = _logic_get_option_chain_by_date(symbol, expiry_date)
    return {
        "data": data,
        "symbol": symbol,
        "expiry_date": expiry_date,
        "_ai_instruction": "请总结当前到期日的行权价分布范围。无需列出所有合约代码，提取出平值（当前股价附近）的 Call 和 Put 合约代码备用即可。"
    }

@mcp.tool()
def get_option_market_data(symbols: list):
    """
    批量获取期权合约的深度行情（支持最多传入500个合约代码）。
    包含关键数据：最新价、成交量、未平仓合约数(Open Interest)、隐含波动率(IV)。
    """
    data = _logic_get_option_quotes(symbols)
    return {
        "data": data,
        "_ai_instruction": "请作为期权分析师，重点解读这些合约的 IV（隐含波动率）和 OI（未平仓量）。OI 最高的行权价通常暗示着强支撑或强阻力。"
    }

# ==========================================
# 📡 实时订阅引擎 (Push)
# ==========================================
@mcp.tool()
def subscribe_market_data(symbols: list[str]):
    """ 订阅指定股票的实时行情并立刻获取一次快照 """
    try:
        quote_ctx.subscribe(symbols, [SubType.Quote])
        return {"status": "SUCCESS", "message": f"Subscribed to {symbols}"}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def get_live_snapshot(symbol: str = None):
    """
    【AI 决策核心】读取实时行情与订单快照。
    采用“WebSocket极速内存为主，API主动拉取为辅”的双引擎自愈机制。
    """
    result = {
        "source": "WebSocket Cache",
        "quotes": {},
        "orders": {}
    }
    
    # ==========================================
    # 1. 行情兜底 (Quote Fallback)
    # ==========================================
    if symbol:
        cached_quote = _LIVE_CACHE["quotes"].get(symbol)
        if cached_quote:
            result["quotes"][symbol] = cached_quote
        else:
            result["quotes"][symbol] = _logic_get_live_quote(symbol) # 使用主动拉取
            result["source"] = "API Pull (Fallback)"
    else:
        result["quotes"] = _LIVE_CACHE["quotes"]
        
    # ==========================================
    # 2. 订单兜底 (Order Fallback - 解决模拟仓延迟)
    # ==========================================
    try:
        # 直接调用 trade_ctx 主动拉取今日订单
        # 只要挂了单，服务器就一定有记录，绝不会为空
        orders_resp = trade_ctx.today_orders()
        active_orders = {}
        
        if orders_resp:
            for order in orders_resp:
                active_orders[order.order_id] = {
                    "symbol": order.symbol,
                    "side": str(order.side),
                    "status": str(order.status).replace("OrderStatus.", ""),  # 将 Rust 枚举转为字符串
                    "executed_qty": str(order.executed_quantity),
                    "price": str(order.price)
                }
        
        # 强制合并到结果中返回
        result["orders"] = active_orders
        
    except Exception as e:
        # 如果拉取失败，退回使用 WebSocket 缓存
        result["orders"] = _LIVE_CACHE["orders"] 
        result["order_error"] = str(e)
        
    return result

# ==========================================
# ⚔️ 交易执行引擎 (Trade)
# ==========================================
@mcp.tool()
def get_account_asset():
    """ 
    【AI 自我感知】
    交易前查验账户可用购买力(buy_power)与持仓。
    """
    try:
        # 1. 安全获取资金余额
        balance_resp = trade_ctx.account_balance()
        
        # 探测结构：可能是原生 list，也可能包裹在 response.list 或 response.channels 中
        if hasattr(balance_resp, 'list'):
            b_list = balance_resp.list
        elif hasattr(balance_resp, 'channels'):
            b_list = balance_resp.channels
        else:
            b_list = balance_resp
            
        buy_power = str(b_list[0].buy_power) if b_list and len(b_list) > 0 else "0"

        # 2. 安全获取股票持仓
        pos_resp = trade_ctx.stock_positions()
        
        # 探测结构：解决 'StockPositionsResponse' object is not iterable 报错
        if hasattr(pos_resp, 'list'):
            p_list = pos_resp.list
        elif hasattr(pos_resp, 'channels'):
            p_list = pos_resp.channels
        else:
            p_list = pos_resp

        positions = []
        if p_list:
            for channel_data in p_list:
                # 兼容 SDK 不同版本中的字段命名差异 (stock_info vs positions)
                stock_list = getattr(channel_data, 'stock_info', getattr(channel_data, 'positions', []))
                
                for p in stock_list:
                    positions.append({
                        "symbol": p.symbol, 
                        "qty": str(p.quantity),
                        # 兜底：如果新版没有 available_quantity，则使用总 quantity
                        "available_qty": str(getattr(p, 'available_quantity', p.quantity)),
                        "cost_price": str(getattr(p, 'cost_price', 0.0)),
                        "currency": str(getattr(p, 'currency', 'USD')),
                        "market": str(getattr(p, 'market', 'US'))
                    })
                    
        return {
            "buy_power": buy_power, 
            "positions": positions
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc() # 在终端打印完整堆栈，方便未来排错
        return {"error": f"Failed to get account asset: {str(e)}"}


@mcp.tool()
def submit_trade_order(symbol: str, side: str, quantity: int, price: float, reason: str):
    """
    【核心交易执行工具】向交易所下达限价买卖单。
    
    参数说明:
    - symbol: 股票代码，如 AAPL.US
    - side: 买卖方向，"Buy" 或 "Sell"
    - quantity: 交易股数
    - price: 限价单的触发价格
    - reason: 🧠 你的核心交易理由（必填！必须详细描述你为什么在这个时刻下达这个订单，结合K线、期权和新闻的分析）。
    """
    try:
        # 这里保留你原本调用长桥 SDK 下单的代码
        trade_ctx = get_trade_ctx()
        order_side = OrderSide.Buy if side.lower() == "buy" else OrderSide.Sell
        resp = trade_ctx.submit_order(
            symbol=symbol,
            order_type=OrderType.LO,
            side=order_side,
            submitted_quantity=Decimal(str(quantity)),
            submitted_price=Decimal(str(price)),
            time_in_force=TimeInForceType.Day,
            remark="AI Autopilot"
        )
        order_id = resp.order_id
        
        # ⚡️ 核心联动：订单提交成功后，立刻将 AI 的“理由”推送到你的 TG！
        msg = (
            f"🤖 **【AI 狙击手：实时交易报告】**\n"
            f"🎯 标的: {symbol}\n"
            f"⚡️ 动作: {side}\n"
            f"📦 数量: {quantity} 股\n"
            f"💰 价格: ${price}\n"
            f"🆔 订单ID: {order_id}\n\n"
            f"🧠 **AI 核心决策理由:**\n{reason}"
        )
        send_tg_notification(msg)
        
        return {"status": "SUCCESS", "order_id": order_id, "message": "Order submitted and TG notification sent."}
        
    except Exception as e:
        # 如果下单失败，也要发个 TG 告诉我为什么失败
        send_tg_notification(f"❌ **下单失败警报**\n标的: {symbol}\n原因: {str(e)}")
        import traceback
        traceback.print_exc()
        return {"error": f"Failed to submit order: {str(e)}"}

@mcp.tool()
def cancel_trade_order(order_id: str):
    """ 撤销未成交的订单 """
    try:
        trade_ctx = get_trade_ctx()
        trade_ctx.cancel_order(order_id)
        return {"status": "SUCCESS", "message": f"Requested cancellation for {order_id}"}
    except Exception as e:
        return {"error": str(e)}

# ⚡️ 新增工具：供 AI 盘前保存网格计划
@mcp.tool()
def save_trading_plan(plan_data: dict):
    """盘前保存你的网格计划 (包含 price, condition(<=或>=), action, reason)"""
    try:
        # plan_data = json.loads(plan_json_str)
        existing_data = {}
        if os.path.exists(PLAN_FILE):
            with open(PLAN_FILE, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for symbol, data in plan_data.items():
            # 自动为每个标的补全更新时间
            data["update_time"] = current_time
            existing_data[symbol] = data
            
        with open(PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, ensure_ascii=False, indent=4)
        return "✅ 作战计划已写入底层雷达！Python 哨兵已接管 24 小时高频盯盘。"
    except Exception as e:
        return f"❌ JSON 保存失败: {str(e)}"

# ==========================================
# 4. 超级聚合工具 V6.3 (复用统一逻辑)
# ==========================================

@mcp.tool()
def get_full_analysis_report(symbol: str):
    """
    🔥 获取【全维度数据包】(Data Bundle)。
    返回包含行情、资金、K线、估值的完整 JSON 数据包。
    """
    global _DATA_CACHE
    try:
        # 1. 搬运数据
        quote = _logic_get_live_quote(symbol)
        static = _logic_get_static_info(symbol)
        fin = _logic_get_financial_indexes(symbol)
        cap = _logic_get_capital_distribution(symbol)
        market = "US" if symbol.endswith(".US") else "HK"
        temp = _logic_get_market_temperature(market)
        
        # 2. 🔥 调用统一逻辑层获取 K 线 (最近 7 天，5分钟线)
        end_d = datetime.now().date()
        start_d = (datetime.now() - timedelta(days=7)).date()
        k_data = _logic_get_history_kline(symbol, Period.Min_5, start_d, end_d)

        # 3. 存入缓存
        _DATA_CACHE = {
            "symbol": symbol,
            "k_data": k_data,
            "quote": quote,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        # 4. 组装数据包
        data_bundle = {
            "snapshot": {
                "symbol": symbol,
                "fetched_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "quote": quote,
                "valuation": fin,
                "market_temp": temp,
                "static": static
            },
            "capital_flow": {
                "raw_data": cap,
                "note": "Please calculate Net Flow = In - Out"
            },
            "technical": {
                "k_line_count": len(k_data),
                "k_line_note": "Full 5-min data provided below"
            }
        }

        # 5. 导演指令
        return {
            "data_bundle": data_bundle,
            "raw_k_lines": k_data,
            "_ai_instruction": """
                请扮演【首席金融分析师】，基于提供的 `data_bundle` 和 `raw_k_lines` 生成一份专业的 Markdown 深度研报。

                要求排版如下：
                1. 🚀 **标题**: 包含股票名称和生成时间。
                2. 📝 **核心摘要**: 包含最新价、主力净流入（需计算）、估值状态。
                3. 💰 **资金博弈**: 详细分析主力与散户的流向对比。
                4. 📈 **技术面深度复盘**: 
                - 你**必须**读取 `raw_k_lines` 中的完整数据。
                - 分析最近 5 个交易日的支撑位、压力位、成交量异动。
                - 给出未来 24 小时的走势预判。
                5. ⚠️ **操作建议**: 给出多空策略。

                ❗ **重要存档指令**: 
                如果用户要求【保存文件】，请调用 `save_analysis_to_file` 工具，只传文本即可。
            """
        }

    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return f"❌ Data Fetch Error: {str(e)}"

# ==========================================
# 5. 保存工具 V6.3 (兼容缓存)
# ==========================================

@mcp.tool()
def save_analysis_to_file(analysis_content: str, filename_prefix: str = "Analysis"):
    """
    💾【保存工具】将分析内容保存到本地文件。
    AI 只需要传入 analysis_content (分析文本)，不需要传入原始数据 (raw data)。
    """
    global _DATA_CACHE
    try:
        # === 👇 目标目录 👇 ===
        save_dir = "/home/claw/my_ai_fund/longbridge_export_data"
        # ====================
        
        if save_dir and not os.path.exists(save_dir):
            os.makedirs(save_dir)
            
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_prefix = filename_prefix.replace(":", "_").replace("/", "_").replace(" ", "_")
        filename = f"{safe_prefix}_{timestamp}.md"
        full_path = os.path.join(save_dir, filename) if save_dir else filename
        
        final_content = analysis_content + "\n\n"
        final_content += "---\n"
        final_content += "### 📎 完整原始数据附件 (Auto-Appended from Cache)\n"
        
        if _DATA_CACHE:
            final_content += f"**标的**: {_DATA_CACHE.get('symbol', 'N/A')}\n"
            final_content += f"**获取时间**: {_DATA_CACHE.get('timestamp', 'N/A')}\n\n"
            final_content += "**5分钟 K线数据 (Full Source):**\n"
            final_content += "```json\n"
            final_content += json.dumps(_DATA_CACHE.get('k_data', []), ensure_ascii=False)
            final_content += "\n```\n"
        else:
            final_content += "⚠️ 缓存中未找到数据 (请先运行 get_full_analysis_report)。\n"

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(final_content)
            
        return f"✅ 文件已保存: `{os.path.abspath(full_path)}` (含缓存数据)"
    except Exception as e:
        return f"❌ 保存失败: {str(e)}"


# ==========================================
# 7. 量化数据无损导出工具 (Direct to Disk)
# ==========================================

@mcp.tool()
def export_kline_data_to_json(symbol: str, period: str = "5min", start: str = None, end: str = None):
    """
    🔥 量化专用：获取历史 K 线数据，并直接无损保存到本地 JSON 文件中。
    不会将庞大的原始数据返回给 AI，从而彻底避免大模型输出截断问题。
    """
    try:
        target_period = PERIOD_MAP.get(period.lower(), Period.Min_5)

        # 2. 日期处理
        if not end:
            end_d = datetime.now().date()
        else:
            end_d = datetime.strptime(end, "%Y-%m-%d").date()
            
        if not start:
            start_d = end_d - timedelta(days=30)
        else:
            start_d = datetime.strptime(start, "%Y-%m-%d").date()

        # 3. 调用底层逻辑拉取完整数据 (会自动触发我们上一版的翻页逻辑)
        data = _logic_get_history_kline(symbol, target_period, start_d, end_d)
        
        if not data or "error" in data[0]:
            return {"error": data[0].get("error", "Unknown error fetching data")}

        # 4. 🔥 核心：Python 直接写硬盘，不经过大模型
        # ====================
        # 您可以修改成您想要的绝对路径
        save_dir = "/home/claw/my_ai_fund/longbridge_export_data" 
        # ====================
        
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_symbol = symbol.replace(".", "_")
        filename = f"{safe_symbol}_{period}_{start_d}_to_{end_d}_{timestamp}.json"
        full_path = os.path.join(save_dir, filename)

        with open(full_path, "w", encoding="utf-8") as f:
            # indent=2 可以让导出的 JSON 文件排版很漂亮，方便您自己点开看
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 5. 只返回“体检报告”给 AI，绝对不返回原数据
        return {
            "status": "SUCCESS",
            "message": "Data exported to local disk successfully.",
            "file_path": full_path,
            "symbol": symbol,
            "period": period,
            "total_records": len(data),  # 告诉 AI 一共拉到了多少条
            "date_range": f"{start_d} to {end_d}",
            "_ai_instruction": "请告诉用户数据已经无损导出完毕，并向用户展示文件保存的具体路径和总记录数(total_records)。不要尝试展示具体的 K 线数据。"
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

# ==========================================
# 📅 市场交易日查询
# ==========================================

def _logic_get_trading_days(market: str = "HK", start: str = None, end: str = None) -> dict:
    """
    查询指定市场的交易日历（含半日市标注）。

    注意：官方 API 限制区间不超过 31 天，其中超出部分会被自动裁剪至当月第一天。
    """
    try:
        ctx = get_ctx()
        m = MARKET_MAP.get(market.upper(), Market.HK)

        # end_d 也需要根据市场时区计算，否则港股深夜可能判断误差一天
        tz_us = pytz.timezone("America/New_York")
        local_today = datetime.now(tz_us).date() if market.upper() == "US" else datetime.now().date()

        if not end:
            end_d = local_today
        else:
            end_d = datetime.strptime(end, "%Y-%m-%d").date()

        if not start:
            start_d = end_d.replace(day=1)
        else:
            start_d = datetime.strptime(start, "%Y-%m-%d").date()

        # 安全防护：官方 API 限制区间不超过 31 天
        if (end_d - start_d).days > 31:
            start_d = end_d.replace(day=1)

        resp = ctx.trading_days(m, start_d, end_d)

        trade_days = []
        half_days = set()
        if hasattr(resp, 'half_trade_day'):
            for d in resp.half_trade_day:
                half_days.add(str(d))
        if hasattr(resp, 'trade_day'):
            for d in resp.trade_day:
                ds = str(d)
                trade_days.append({"date": ds, "is_half_day": ds in half_days})

        is_today_trading = str(local_today) in {str(d) for d in (resp.trade_day if hasattr(resp, 'trade_day') else [])}

        return {
            "market": market.upper(),
            "range_start": str(start_d),
            "range_end": str(end_d),
            "is_today_trading_day": is_today_trading,
            "trade_days": trade_days
        }
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def get_trading_days(market: str = "HK", start: str = None, end: str = None):
    """
    查询指定市场的交易日历（含半日市标注）。
    
    参数:
    - market: 市场代码，可选 "HK", "US", "CN", "SG"。默认 "HK"。
    - start: 开始日期 (格式 "YYYY-MM-DD")。默认当月第一天。
    - end: 结束日期 (格式 "YYYY-MM-DD")。默认当天。

    ℹ️ 限制：日期区间不能超过一个月，只支持最近一年的数据。
    """
    data = _logic_get_trading_days(market, start, end)
    if "error" in data:
        return {"error": f"Failed to get trading days: {data['error']}"}

    return {
        "market": data["market"],
        "range": f"{data['range_start']} to {data['range_end']}",
        "is_today_trading_day": data["is_today_trading_day"],
        "total_trading_days": len(data["trade_days"]),
        "trading_days": data["trade_days"],
        "_ai_instruction": "请清晰展示交易日历，特别标注半日市，并告知今天是否为交易日。"
    }



# ==========================================
# 📖 计算指标字典
# ==========================================

@mcp.tool()
def get_calc_index_dictionary():
    """
    返回长桥 CalcIndex 全量指标字典（零 API 调用，纯本地数据）。
    
    每个条目包含: id, name, description(中文含义), scope(适用证券类型)。
    AI 可先调用此工具了解可用指标，再通过 get_financial_indexes 的 indexes 参数按需查询。
    """
    return {
        "total_count": len(CALC_INDEX_CATALOG),
        "indexes": CALC_INDEX_CATALOG,
        "usage_hint": "将所需指标的 name 字段作为列表传给 get_financial_indexes(symbol, indexes=[...]) 即可查询",
        "_ai_instruction": "请列出全部指标（按 scope 分组）并说明用法。"
    }


if __name__ == "__main__":
    mcp.run()