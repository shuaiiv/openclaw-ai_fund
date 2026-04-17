#!/usr/bin/env python3
"""
StockScope - 本地行情情报采集器
=====================================
一键抓取指定标的的全维度行情快照，输出为 Markdown 报告，供 Gemini-CLI 直接读取分析。

用法:
    python fetch.py AAPL.US
    python fetch.py AAPL.US 700.HK        # 同时采集多个标的

输出位置: stockscope/output/<SYMBOL>_<date>.md
缓存目录: stockscope/cache/
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta, date

import pytz
from dotenv import load_dotenv, find_dotenv

# -----------------------------------------------------------------
# 路径设置：复用 mcp/longbridge_server.py 中的全部逻辑函数
# -----------------------------------------------------------------
_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR    = os.path.dirname(_SCRIPT_DIR)
_LONGBRIDGE_DIR = os.path.join(_ROOT_DIR, "longbridge")
_FUTU_DIR       = os.path.join(_ROOT_DIR, "futu")
sys.path.insert(0, _LONGBRIDGE_DIR)
sys.path.insert(0, _FUTU_DIR)

load_dotenv(find_dotenv())

# 延迟导入：等 dotenv 加载完再 import，避免 SDK 在 .env 读取前初始化失败
from longbridge_server import (          # noqa: E402
    _logic_get_live_quote,
    _logic_get_static_info,
    _logic_get_financial_indexes,
    _logic_get_market_temperature,
    _logic_get_capital_distribution,
    _logic_get_history_kline,
    get_trading_days,
    close_contexts,
    MARKET_MAP,
)
from longbridge.openapi import Period, Market   # noqa: E402

# 期权模块使用富途 API
from futu_options_server import (          # noqa: E402
    _logic_get_expiry_dates,
    _logic_get_option_chain,
    _logic_get_option_snapshots,
    close_context as close_futu_context,   # 显式回收 Futu OpenD 连接
)

# -----------------------------------------------------------------
# 目录常量
# -----------------------------------------------------------------
CACHE_DIR  = os.path.join(_ROOT_DIR, "data", "cache")
OUTPUT_DIR = os.path.join(_ROOT_DIR, "data")
os.makedirs(CACHE_DIR,  exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ===========================================================================
# 🗄️  缓存工具 (增量更新，复用 premarket_planner 策略)
# ===========================================================================

def _cache_path(symbol: str, tag: str) -> str:
    safe = symbol.replace(".", "_")
    return os.path.join(CACHE_DIR, f"{safe}_{tag}.json")


def _read_cache(path: str):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _write_cache(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_last_n_trading_days(market: str, n: int) -> tuple[date, date]:
    """
    通过官方交易日历 API 获取最近 n 个交易日的 (start_date, end_date)。
    避免连续假期/展期/攼标日导致日历日计算失误。
    """
    today = datetime.now().date()
    start_d = today - timedelta(days=30)  # 向前 30 天必能覆盖 n 个交易日
    try:
        resp = get_trading_days(market, str(start_d), str(today))
        trading_list = resp.get("trading_days", [])
        if trading_list:
            days = sorted(
                [d["date"] for d in trading_list],
                reverse=True  # 降序，最新在前
            )
            recent = [datetime.strptime(d, "%Y-%m-%d").date() for d in days[:n]]
            if recent:
                return min(recent), max(recent)
    except Exception as e:
        print(f"  ⚠️ 交易日查询失败({market})，退化为日历日: {e}")
    # 降级备份
    return today - timedelta(days=n * 2), today


# ===========================================================================
# 📡  数据采集层（每个函数只做一件事，失败时静默降级）
# ===========================================================================

def collect_live_quote(symbol: str) -> dict:
    """实时行情（价格 / 涨跌 / 量）"""
    result = _logic_get_live_quote(symbol)
    return result


def collect_static_info(symbol: str) -> dict:
    """
    基本静态信息（名称 / 板块 / 货币 / 手数 / 总股本）—— 永久缓存。

    名称、货币、手数等字段极少变更，命中缓存即直接返回。
    如需强制刷新，手动删除 cache/xxx_static.json 即可。
    """
    cache_file = _cache_path(symbol, "static")
    cached     = _read_cache(cache_file)
    if cached:
        return cached

    data = _logic_get_static_info(symbol)
    if data and "error" not in data:
        _write_cache(cache_file, data)
    return data


def collect_financial_indexes(symbol: str) -> dict:
    """估值指标（PE/PB/市值/股息率/换手率/量比）—— 每日缓存"""
    cache_file = _cache_path(symbol, "financials")
    cached_raw = _read_cache(cache_file)

    today_str = datetime.now().strftime("%Y-%m-%d")
    if cached_raw and cached_raw.get("_date") == today_str:
        return cached_raw

    data = _logic_get_financial_indexes(symbol)
    if data and "error" not in data:
        data["_date"] = today_str
        _write_cache(cache_file, data)
    return data


def collect_market_temperature(market: str) -> dict:
    """当前市场温度 + 最近 5 个交易日历史温度"""
    result = {"current": {}, "history": []}

    try:
        result["current"] = _logic_get_market_temperature(market)
    except Exception as e:
        result["current"] = {"error": str(e)}

    # 历史温度：获取最近 5 个交易日的温度
    try:
        ctx_mod = sys.modules.get("longbridge_server")
        if ctx_mod and hasattr(ctx_mod, "get_ctx"):
            ctx = ctx_mod.get_ctx()
            m   = MARKET_MAP.get(market.upper(), Market.HK)
            # 拉足够多天确保能覆盖 5 个交易日
            start_td, end_td = _get_last_n_trading_days(market, 5)
            resp = ctx.history_market_temperature(m, start_td, end_td)
            if hasattr(resp, "records") and resp.records:
                for item in resp.records:
                    result["history"].append({
                        "date": item.timestamp.strftime("%Y-%m-%d"),
                        "temp": item.temperature,
                    })
    except Exception as e:
        result["history_error"] = str(e)

    return result


def collect_daily_kline(symbol: str, days: int = 60) -> list:
    """近 N 日日线 K 线 —— 增量缓存（只在缺口时补拉）"""
    cache_file = _cache_path(symbol, "kline_daily")
    cached     = _read_cache(cache_file) or []

    today   = datetime.now().date()
    need_fetch = True

    if cached and isinstance(cached, list) and len(cached) > 0:
        latest_str = cached[-1].get("t", "")[:10]
        try:
            latest_date = datetime.strptime(latest_str, "%Y-%m-%d").date()
            if (today - latest_date).days <= 1 and len(cached) >= 30:
                need_fetch = False
        except (ValueError, IndexError):
            pass

    if need_fetch:
        start_d = today - timedelta(days=days + 10)
        data    = _logic_get_history_kline(symbol, Period.Day, start_d, today)
        if data and "error" not in data[0]:
            cached = data
            _write_cache(cache_file, data)

    return cached


def collect_minute_kline(symbol: str, period: Period, tag: str, fetch_days: int = 3) -> list:
    """分钟 K 线（10 分 / 5 分）—— 当日缓存"""
    cache_file = _cache_path(symbol, f"kline_{tag}")
    cached     = _read_cache(cache_file) or []

    today     = datetime.now().date()
    need_fetch = True

    if cached and isinstance(cached, list) and len(cached) > 0:
        latest_str = cached[-1].get("t", "")[:10]
        try:
            latest_date = datetime.strptime(latest_str, "%Y-%m-%d").date()
            if (today - latest_date).days == 0 and len(cached) >= 10:
                need_fetch = False
        except (ValueError, IndexError):
            pass

    if need_fetch:
        if period == Period.Min_10:
            # 10分钟 K 线：用交易日历获取真实交易日范围
            market = "US" if symbol.endswith(".US") else "HK"
            start_d, end_d = _get_last_n_trading_days(market, fetch_days)
        else:
            # 其他周期（如 5分钟）保持原逻辑
            market = "US" if symbol.endswith(".US") else "HK"
            end_d = today
            start_d = today - timedelta(days=fetch_days)
        data = _logic_get_history_kline(symbol, period, start_d, end_d)
        if data and isinstance(data, list) and "error" not in data[0]:
            cached = data
            _write_cache(cache_file, data)

    # 美股10分钟 K 线只保留常规盘中：09:30-16:00 美东时间
    if period == Period.Min_10 and symbol.endswith(".US"):
        et = pytz.timezone("America/New_York")
        filtered = []
        for k in cached:
            try:
                t = datetime.strptime(k["t"], "%Y-%m-%d %H:%M")
                t_et = pytz.utc.localize(t).astimezone(et)
                h, m = t_et.hour, t_et.minute
                if (h == 9 and m >= 30) or (10 <= h <= 14) or (h == 15):
                    filtered.append(k)
            except Exception:
                filtered.append(k)
        return filtered

    return cached


def collect_capital_flow(symbol: str) -> dict:
    """当日资金流向分布（大 / 中 / 小单三档）"""
    return _logic_get_capital_distribution(symbol)


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


def collect_option_snapshot(symbol: str, current_price: float) -> list:
    """
    期权 ATM 探针：本周末 + 两周后两个到期日的 Call/Put IV/OI。
    使用富途 API（主通道）+ YahooQuery（降级）。
    输入 symbol 使用长桥格式，内部自动转换为富途格式。
    """
    results = []
    try:
        futu_code = lb_to_futu(symbol)  # e.g. "AAPL.US" -> "US.AAPL"
        opt_dates_raw = _logic_get_expiry_dates(futu_code)

        if isinstance(opt_dates_raw, dict) and "error" in opt_dates_raw:
            return [{"error": opt_dates_raw["error"]}]
        if not isinstance(opt_dates_raw, list) or not opt_dates_raw:
            return [{"error": "no expiry dates"}]

        today = datetime.now().date()
        future_dates = []
        for r in opt_dates_raw:
            date_str = r.get("strike_time", "")
            try:
                d = datetime.strptime(str(date_str), "%Y-%m-%d").date()
                if d > today:
                    future_dates.append(d)
            except (ValueError, TypeError):
                pass
        future_dates = sorted(future_dates)
        if not future_dates:
            return [{"error": "no future expiry dates"}]

        days_to_friday  = (4 - today.weekday()) % 7 or 7
        this_friday     = today + timedelta(days=days_to_friday)
        two_weeks_out   = today + timedelta(days=14)

        def pick_closest(dates, target):
            return min(dates, key=lambda d: abs((d - target).days))

        near_date = pick_closest(future_dates, this_friday)
        far_date  = pick_closest(future_dates, two_weeks_out)

        targets = [(near_date, "本周末")]
        if far_date != near_date:
            targets.append((far_date, "两周后"))

        target_symbols: list[str] = []
        date_labels: dict[str, str] = {}

        for exp_date, label in targets:
            exp_str = exp_date.strftime("%Y-%m-%d")
            chain   = _logic_get_option_chain(futu_code, exp_str)
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
            return [{"error": "no ATM contracts found"}]

        opt_quotes = _logic_get_option_snapshots(target_symbols)
        if not isinstance(opt_quotes, list):
            return [{"error": str(opt_quotes)}]

        for q in opt_quotes:
            if "error" in q:
                continue
            fc = q.get("futu_code", "")
            try:
                iv_val = q.get("implied_volatility")
                iv_str = f"{float(iv_val):.1f}%" if iv_val is not None else "N/A"
            except (TypeError, ValueError):
                iv_str = "N/A"
            results.append({
                "label":         date_labels.get(fc, fc),
                "iv":            iv_str,
                "open_interest": q.get("open_interest", "N/A"),
                "volume":        q.get("volume", "N/A"),
                "last":          q.get("last_price", "N/A"),
                "source":        q.get("_source", ""),
            })

    except Exception as e:
        results.append({"error": str(e)})

    return results


# ===========================================================================
# 📝  报告渲染（输出 Markdown，Gemini-CLI 友好）
# ===========================================================================

def _market_of(symbol: str) -> str:
    """从代码后缀推断市场"""
    return "US" if symbol.upper().endswith(".US") else "HK"


def render_report(
    symbol:      str,
    quote:       dict,
    static:      dict,
    financials:  dict,
    temperature: dict,
    daily_k:     list,
    min10_k:     list,
    min5_k:      list,
    cap_flow:    dict,
    options:     list,
) -> str:
    """将所有数据维度拼合成 Markdown 报告字符串"""

    now_str    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    price      = quote.get("price", "N/A")
    change_pct = quote.get("change_rate", "N/A")

    lines = [
        f"# 📊 StockScope 行情快照 — {symbol}",
        f"> 生成时间: {now_str}",
        "",
        "---",
        "",

        # ── 基本面 ──────────────────────────────
        "## 🏷️ 基本信息",
        f"- **标的**: `{symbol}`  名称: {static.get('name', 'N/A')}",
        f"- **当前价格**: `{price}`  涨跌幅: {change_pct}",
        f"- **开盘**: {quote.get('open', 'N/A')}  最高: {quote.get('high', 'N/A')}  最低: {quote.get('low', 'N/A')}",
        f"- **成交量**: {quote.get('vol', 'N/A')}",
        f"- **货币**: {static.get('currency', 'N/A')}  手数: {static.get('lot_size', 'N/A')}  总股本: {static.get('total_shares', 'N/A')}",
        "",

        # ── 估值指标 ──────────────────────────────
        "## 📈 估值指标",
    ]

    if financials and "error" not in financials:
        fi_labels = {
            "total_market_value":  "总市值",
            "pe_ttm_ratio":        "PE (TTM)",
            "pb_ratio":            "PB",
            "dividend_ratio_ttm":  "股息率 (TTM)",
            "turnover_rate":       "换手率",
            "volume_ratio":        "量比",
        }
        for key, label in fi_labels.items():
            val = financials.get(key, "N/A")
            if val != "N/A":
                lines.append(f"- **{label}**: {val}")
    else:
        lines.append(f"- ⚠️ 估值指标获取失败: {financials.get('error', 'Unknown')}")
    lines.append("")

    # ── 市场温度 ──────────────────────────────
    market = _market_of(symbol)
    cur_t  = temperature.get("current", {})
    lines += [
        f"## 🌡️ 市场温度 ({market})",
        f"- **当前温度**: {cur_t.get('temp', 'N/A')} | {cur_t.get('desc', '')}",
        f"- **估值**: {cur_t.get('val', 'N/A')}  情绪: {cur_t.get('sent', 'N/A')}",
    ]
    hist = temperature.get("history", [])
    if hist:
        lines.append("- **近 5 日趋势**:")
        for h in hist:
            lines.append(f"  - {h['date']}: {h['temp']}")
    lines.append("")

    # ── 资金流向 ──────────────────────────────
    lines.append("## 💰 当日资金流向分布")
    if "error" not in cap_flow:
        net_l = cap_flow.get("in_large",  0) - cap_flow.get("out_large",  0)
        net_m = cap_flow.get("in_medium", 0) - cap_flow.get("out_medium", 0)
        net_s = cap_flow.get("in_small",  0) - cap_flow.get("out_small",  0)
        net_t = net_l + net_m + net_s
        sentiment = "🟢 主力净流入" if net_l > 0 else "🔴 主力净流出"
        lines += [
            f"- {sentiment} | 总净流: `{net_t:+.0f}`",
            f"  - 🟡 大单: 流入 {cap_flow.get('in_large',0):.0f} / 流出 {cap_flow.get('out_large',0):.0f} = 净 {net_l:+.0f}",
            f"  - 🟠 中单: 流入 {cap_flow.get('in_medium',0):.0f} / 流出 {cap_flow.get('out_medium',0):.0f} = 净 {net_m:+.0f}",
            f"  - ⚪️ 小单: 流入 {cap_flow.get('in_small',0):.0f} / 流出 {cap_flow.get('out_small',0):.0f} = 净 {net_s:+.0f}",
        ]
    else:
        lines.append(f"- ⚠️ 资金流向获取失败: {cap_flow.get('error')}")
    lines.append("")

    # ── 期权快照 ──────────────────────────────
    lines.append("## 🛡️ 期权 ATM 探针 (IV / OI)")
    if options:
        for o in options:
            if "error" in o:
                lines.append(f"- ⚠️ {o['error']}")
            else:
                src = f" [{o['source']}]" if o.get("source") else ""
                lines.append(
                    f"- **{o['label']}** | IV={o['iv']} | OI={o['open_interest']} | "
                    f"Vol={o['volume']} | Last={o['last']}{src}"
                )
    else:
        lines.append("- ⚠️ 期权数据为空（该标的暂无期权数据或富途 API 权限不足）")
    lines.append("")

    # ── 日线 K 线 ──────────────────────────────
    lines.append("## 📉 近 60 日日线 K 线")
    lines.append("```")
    lines.append("日期       |  开盘  |  最高  |  最低  |  收盘  |  成交量")
    if daily_k:
        for k in daily_k[-60:]:
            lines.append(
                f"{k['t']} | {k['o']:>6} | {k['h']:>6} | "
                f"{k['l']:>6} | {k['c']:>6} | {k['v']}"
            )
    else:
        lines.append("暂无数据")
    lines.append("```")
    lines.append("")

    # ── 10 分钟 K 线 ──────────────────────────────
    lines.append("## 📊 近 3 日 10 分钟 K 线")
    lines.append("```")
    lines.append("时间              |  开盘  |  最高  |  最低  |  收盘  |  成交量")
    if min10_k:
        for k in min10_k[-120:]:
            lines.append(
                f"{k['t']} | {k['o']:>6} | {k['h']:>6} | "
                f"{k['l']:>6} | {k['c']:>6} | {k['v']}"
            )
    else:
        lines.append("暂无数据")
    lines.append("```")
    lines.append("")

    # ── 5 分钟 K 线（今日盘中）──────────────────────
    lines.append("## ⚡️ 今日 5 分钟盘中 K 线")
    lines.append("```")
    lines.append("时间              |  开盘  |  最高  |  最低  |  收盘  |  成交量")
    if min5_k:
        for k in min5_k:
            lines.append(
                f"{k['t']} | {k['o']:>6} | {k['h']:>6} | "
                f"{k['l']:>6} | {k['c']:>6} | {k['v']}"
            )
    else:
        lines.append("暂无数据（非交易时段）")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


# ===========================================================================
# 🚀  主函数
# ===========================================================================

def fetch_symbol(symbol: str, skip_account: bool = False) -> str:
    """逐步采集全维度数据，返回完整报告字符串"""
    market = _market_of(symbol)
    print(f"\n{'='*55}")
    print(f"  StockScope 正在采集: {symbol}  [{market}]")
    print(f"{'='*55}")

    # Step 1: 实时行情
    print("  [1/7] 实时行情...")
    quote = collect_live_quote(symbol)
    current_price = quote.get("price", 0.0)

    # Step 2: 静态信息 + 估值指标
    print("  [2/7] 基本信息 & 估值指标...")
    static     = collect_static_info(symbol)
    financials = collect_financial_indexes(symbol)

    # Step 3: 市场温度
    print("  [3/7] 市场温度...")
    temperature = collect_market_temperature(market)

    # Step 4: 资金流向
    print("  [4/7] 资金流向...")
    cap_flow = collect_capital_flow(symbol)

    # Step 5: K 线
    print("  [5/7] 日线 K 线 (60日)...")
    daily_k = collect_daily_kline(symbol, days=60)

    print("  [6/7] 分钟 K 线 (10min + 5min)...")
    min10_k = collect_minute_kline(symbol, Period.Min_10, "10min", fetch_days=3)
    today   = datetime.now().date()
    min5_k  = collect_minute_kline(symbol, Period.Min_5, "5min_today", fetch_days=1)
    # 只保留今天的 5 分钟数据
    today_str = today.strftime("%Y-%m-%d")
    min5_k    = [k for k in min5_k if k.get("t", "").startswith(today_str)]

    # Step 6: 期权快照
    # 富途 API 同时支持美股和港股期权，统一尝试；内部已有容错
    print("  [7/7] 期权 ATM 探针...")
    options = collect_option_snapshot(symbol, current_price)

    # 渲染报告
    report = render_report(
        symbol      = symbol,
        quote       = quote,
        static      = static,
        financials  = financials,
        temperature = temperature,
        daily_k     = daily_k,
        min10_k     = min10_k,
        min5_k      = min5_k,
        cap_flow    = cap_flow,
        options     = options,
    )

    # 写入输出文件
    date_str   = today.strftime("%Y%m%d")
    safe_sym   = symbol.replace(".", "_")
    out_file   = os.path.join(OUTPUT_DIR, f"{safe_sym}_{date_str}.md")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n  ✅ 报告已保存: {out_file}")
    return out_file


def main():
    parser = argparse.ArgumentParser(
        description="StockScope — 本地行情情报采集器，输出 Markdown 供 Gemini-CLI 分析"
    )
    parser.add_argument(
        "symbols",
        nargs="+",
        metavar="SYMBOL",
        help="标的代码，如 AAPL.US 或 700.HK（可多个）"
    )
    args = parser.parse_args()

    output_files = []
    try:
        for sym in args.symbols:
            try:
                out = fetch_symbol(sym.upper())
                output_files.append(out)
            except Exception as e:
                print(f"\n  ❌ {sym} 采集失败: {e}")
    finally:
        close_contexts()       # 释放长桥 WebSocket 连接
        close_futu_context()   # 释放富途 OpenD 连接（其后台线程是非 daemon，必须显式关闭）

    print("\n" + "="*55)
    print("  📁 全部报告：")
    for f in output_files:
        print(f"     {f}")
    print("="*55)
    os._exit(0)  # 强制退出：绕过非 daemon 线程的等待，确保进程真正终止


if __name__ == "__main__":
    main()
