# 文件名: futu_options_server.py
"""
富途期权数据 MCP 服务
专注于期权链查询、期权快照(含希腊值/IV/OI)的获取。

三步工作流参照 longbridge_server.py:
  1. get_option_expiry_dates  — 获取所有到期日
  2. get_option_chain_by_date — 根据到期日获取期权合约列表
  3. get_option_market_data   — 批量获取合约实时行情(主通: 富途，降级: YahooQuery)
"""

import os
import sys
import json
import re
import logging
import atexit
from mcp.server.fastmcp import FastMCP
from futu import *

# ==========================================
# 🔇 环境配置
# ==========================================
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(level=logging.CRITICAL, stream=sys.stderr)

# ==========================================
# 初始化 FastMCP 服务
# ==========================================
mcp = FastMCP("Futu_Options_MCP")

# 从环境变量读取 OpenD 连接配置
OPEND_HOST = os.getenv("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("OPEND_PORT", 11111))

# ==========================================
# 🔌 全局单例 Context 懒初始化
# ==========================================
quote_ctx = None
_init_error: str = ""


def _init_context():
    """懒初始化：首次使用时才建立连接，异常时打印完整 traceback。"""
    global quote_ctx, _init_error
    if quote_ctx is not None:
        return
    import traceback
    try:
        quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
        _init_error = ""
    except Exception:
        _init_error = traceback.format_exc()
        quote_ctx = None
        print(f"❌ Futu OpenD 初始化失败:\n{_init_error}", flush=True)


def get_ctx():
    """获取行情 Context（懒初始化）"""
    _init_context()
    if quote_ctx is None:
        raise RuntimeError(f"OpenQuoteContext 初始化失败：\n{_init_error}")
    return quote_ctx


def close_context():
    """主动断开 OpenD 长连接"""
    global quote_ctx, _init_error
    if quote_ctx is not None:
        try:
            quote_ctx.close()
        except Exception:
            pass
        quote_ctx = None
    _init_error = ""
    print("🔌 [连接回收] Futu Options OpenD 会话已断开。", flush=True)


@atexit.register
def _cleanup_at_exit():
    close_context()


# ==========================================
# 🛠️ 工具函数
# ==========================================

def _df_to_records(df) -> list:
    """
    将 pandas DataFrame 转为 list[dict]，并将 NaN/NaT/特殊枚举值安全序列化为 Python 原生类型。
    """
    import math
    records = []

    # 用 json round-trip 处理不可直接序列化的类型
    raw = df.to_dict(orient="records")
    for row in raw:
        clean = {}
        for k, v in row.items():
            if v is None:
                clean[k] = None
            elif isinstance(v, float) and math.isnan(v):
                clean[k] = None
            elif isinstance(v, (int, float, bool, str)):
                clean[k] = v
            else:
                # 枚举/Timestamp 等 Futu 原生类型统一 str 化
                clean[k] = str(v)
        records.append(clean)
    return records


def _ok(data, ai_instruction: str = "") -> dict:
    result = {"data": data}
    if ai_instruction:
        result["_ai_instruction"] = ai_instruction
    return result


def _err(msg) -> dict:
    return {"error": str(msg)}


# ==========================================
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 逻辑层（_logic_*）：纯 Python，返回 list/dict
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _logic_get_expiry_dates(code: str) -> list | dict:
    """
    【主通道：富途官方】获取期权链所有到期日。
    返回 list[str]，按日期升序排列；失败返回 {"error": ...}。

    API: get_option_expiration_date(code)
    返回字段: strike_time (yyyy-MM-dd), option_expiry_date_distance, expiration_cycle
    """
    try:
        ctx = get_ctx()
        ret, data = ctx.get_option_expiration_date(code)
        if ret != RET_OK:
            return {"error": f"get_option_expiration_date 失败: {data}"}
        if data.empty:
            return []

        records = _df_to_records(data)
        # 过滤掉已过期 (distance < 0) 的条目，保留未来到期日
        future = [r for r in records if r.get("option_expiry_date_distance", 0) is not None
                  and r.get("option_expiry_date_distance", 0) >= 0]
        return future if future else records
    except Exception as e:
        return {"error": str(e)}


def _logic_get_option_chain(code: str, expiry_date: str,
                             option_type: str = "ALL",
                             option_cond_type: str = "ALL") -> list | dict:
    """
    【主通道：富途官方】获取指定到期日的期权链静态信息。
    返回 list[dict]，每条包含 code, name, option_type, strike_price, strike_time 等。

    API: get_option_chain(code, start=expiry_date, end=expiry_date, option_type=..., option_cond_type=...)
    注意: 富途此接口仅返回静态信息，动态行情需另拉快照。
    """
    try:
        ctx = get_ctx()
        # 兼容 YYYYMMDD 和 YYYY-MM-DD 两种格式
        clean_date = expiry_date.replace("-", "")
        fmt_date = f"{clean_date[:4]}-{clean_date[4:6]}-{clean_date[6:]}"

        # 解析 option_type 枚举
        ot_map = {"ALL": OptionType.ALL, "CALL": OptionType.CALL, "PUT": OptionType.PUT}
        ot_enum = ot_map.get(option_type.upper(), OptionType.ALL)

        # 解析 option_cond_type 枚举
        oct_map = {
            "ALL": OptionCondType.ALL,
            "WITHIN":  OptionCondType.WITHIN,  # 价内
            "OUTSIDE": OptionCondType.OUTSIDE, # 价外
        }
        oct_enum = oct_map.get(option_cond_type.upper(), OptionCondType.ALL)

        ret, data = ctx.get_option_chain(
            code, start=fmt_date, end=fmt_date,
            option_type=ot_enum, option_cond_type=oct_enum
        )
        if ret != RET_OK:
            return {"error": f"get_option_chain 失败: {data}"}
        if data.empty:
            return []

        records = _df_to_records(data)

        # 精简输出：只保留分析所需核心字段
        simplified = []
        for r in records:
            simplified.append({
                "futu_code":    r.get("code", ""),
                "name":         r.get("name", ""),
                "option_type":  str(r.get("option_type", "")),   # CALL / PUT
                "strike_price": r.get("strike_price"),
                "strike_time":  r.get("strike_time", fmt_date),
                "lot_size":     r.get("lot_size"),
                "suspension":   r.get("suspension", False),
                "expiration_cycle": str(r.get("expiration_cycle", "")),
                "option_standard_type": str(r.get("option_standard_type", "")),
            })
        return simplified
    except Exception as e:
        return {"error": str(e)}


def _logic_get_option_snapshots(code_list: list) -> list | dict:
    """
    【主通道：富途官方】批量获取期权合约实时快照（含 IV、OI、Greeks）。
    使用 get_market_snapshot，富途快照接口对期权合约同样适用，且无需订阅。

    返回字段: futu_code, last_price, volume, open_interest, implied_volatility,
              strike_price, option_type, expiry_date, delta, gamma, vega, theta, rho, _source
    """
    try:
        ctx = get_ctx()
        ret, data = ctx.get_market_snapshot(code_list)
        if ret != RET_OK:
            return {"error": f"get_market_snapshot 失败: {data}"}
        if data.empty:
            return []

        records = _df_to_records(data)
        result = []
        for r in records:
            # 只提取期权相关字段（option_valid == True 时才有合法数值）
            result.append({
                "futu_code":          r.get("code", ""),
                "name":               r.get("name", ""),
                "last_price":         r.get("last_price"),
                "volume":             r.get("volume"),
                "turnover":           r.get("turnover"),
                "bid_price":          r.get("bid_price"),
                "ask_price":          r.get("ask_price"),
                # 期权特有字段
                "option_type":        str(r.get("option_type", "")),
                "strike_price":       r.get("option_strike_price"),
                "expiry_date":        r.get("strike_time"),
                "option_contract_size": r.get("option_contract_size"),
                "open_interest":      r.get("option_open_interest"),
                "implied_volatility": r.get("option_implied_volatility"),
                "premium":            r.get("option_premium"),
                "delta":              r.get("option_delta"),
                "gamma":              r.get("option_gamma"),
                "vega":               r.get("option_vega"),
                "theta":              r.get("option_theta"),
                "rho":                r.get("option_rho"),
                "expiry_date_distance":        r.get("option_expiry_date_distance"),
                "option_area_type":            str(r.get("option_area_type", "")),
                "option_contract_multiplier":  r.get("option_contract_multiplier"),
                "_source": "Futu Real-time",
            })
        return result
    except Exception as e:
        error_msg = str(e)
        # 权限不足时（常见错误码 4106/4001 或消息含 "no quota"）降级到 YahooQuery
        if any(kw in error_msg.lower() for kw in ["4106", "4001", "no quota", "no permission", "auth"]):
            print(f"⚠️ 富途期权行情权限不足，降级至 YahooQuery: {error_msg}", flush=True)
            return _fallback_yahooquery_snapshots(code_list)
        return {"error": error_msg}


# ==========================================
# 🥷 YahooQuery 降级通道
# ==========================================

def _parse_futu_us_option_code(futu_code: str):
    """
    解析富途美股期权代码格式: US.AAPL260320C242500
    → underlying="AAPL", yymmdd="260320", opt_type="C", strike=242.5, market="US"
    富途行权价单位: 整数 × 1/1000 → 实际行权价 (如 242500 → 242.5)
    """
    # 匹配 US. 前缀
    match = re.match(r'^US\.([A-Z]+)(\d{6})([CP])(\d+)$', futu_code)
    if not match:
        return None
    underlying, yymmdd, opt_type, strike_raw = match.groups()
    yy, mm, dd = yymmdd[:2], yymmdd[2:4], yymmdd[4:]
    expiry_date = f"20{yy}-{mm}-{dd}"
    strike = float(strike_raw) / 1000.0
    return {
        "underlying": underlying,
        "expiry_date": expiry_date,
        "opt_type": opt_type,     # "C" or "P"
        "strike": strike,
        "market": "US",
    }


def _fallback_yahooquery_snapshots(futu_codes: list) -> list:
    """
    YahooQuery 降级：当富途期权行情权限不足时，拉取雅虎数据作为补全。
    目前仅支持美股 (US.*) 期权代码，港股 (HK.*) 无法降级。
    """
    try:
        from yahooquery import Ticker
        import pandas as pd

        if not futu_codes:
            return []

        # 按标的股分组，避免对同一标的多次调用雅虎
        underlying_map: dict[str, list] = {}    # underlying → list of futu_codes
        unparseable = []

        for fc in futu_codes:
            parsed = _parse_futu_us_option_code(fc)
            if parsed is None:
                unparseable.append(fc)
                continue
            sym = parsed["underlying"]
            if sym not in underlying_map:
                underlying_map[sym] = []
            underlying_map[sym].append((fc, parsed))

        if unparseable:
            print(f"⚠️ 无法解析的富途代码（非美股格式），跳过: {unparseable}", flush=True)

        result = []

        for underlying, code_parsed_pairs in underlying_map.items():
            try:
                ticker = Ticker(underlying)
                yq_options = ticker.option_chain
                if isinstance(yq_options, str) or (hasattr(yq_options, 'empty') and yq_options.empty):
                    result.extend(_make_empty_records(code_parsed_pairs, "YahooQuery 无数据"))
                    continue

                df = yq_options.reset_index()
                # 统一到期日格式为字符串前10位
                df["exp_str"] = df["expiration"].astype(str).str.slice(0, 10)

                for futu_code, parsed in code_parsed_pairs:
                    expiry_date = parsed["expiry_date"]
                    opt_type    = parsed["opt_type"]
                    strike      = parsed["strike"]
                    direction   = "Call" if opt_type == "C" else "Put"
                    yq_type     = "calls" if opt_type == "C" else "puts"

                    day_df = df[df["exp_str"] == expiry_date]
                    matched = day_df[
                        (day_df["optionType"] == yq_type) &
                        (day_df["strike"].round(2) == round(strike, 2))
                    ]

                    if matched.empty:
                        result.append({
                            "futu_code":    futu_code,
                            "name":         f"{underlying} {expiry_date} {strike:.2f} {direction}",
                            "last_price":   None,
                            "volume":       None,
                            "open_interest":None,
                            "implied_volatility": None,
                            "strike_price": strike,
                            "option_type":  direction,
                            "expiry_date":  expiry_date,
                            "delta":        None,
                            "gamma":        None,
                            "vega":         None,
                            "theta":        None,
                            "rho":          None,
                            "_source":      "YahooQuery Fallback (No Match)",
                        })
                        continue

                    row = matched.iloc[0]

                    def _safe_float(val, default=None):
                        import math
                        try:
                            v = float(val)
                            return default if math.isnan(v) else v
                        except Exception:
                            return default

                    result.append({
                        "futu_code":          futu_code,
                        "name":               f"{underlying} {expiry_date} {strike:.2f} {direction}",
                        "last_price":         _safe_float(row.get("lastPrice")),
                        "volume":             int(row.get("volume", 0)) if _safe_float(row.get("volume")) is not None else None,
                        "open_interest":      int(row.get("openInterest", 0)) if _safe_float(row.get("openInterest")) is not None else None,
                        "implied_volatility": _safe_float(row.get("impliedVolatility")),
                        "strike_price":       strike,
                        "option_type":        direction,
                        "expiry_date":        expiry_date,
                        "bid_price":          _safe_float(row.get("bid")),
                        "ask_price":          _safe_float(row.get("ask")),
                        # YahooQuery 暂不直接提供 Greeks（需二次计算），置 None
                        "delta":              None,
                        "gamma":              None,
                        "vega":               None,
                        "theta":              None,
                        "rho":                None,
                        "_source":            "YahooQuery Fallback",
                    })

            except Exception as e:
                result.extend(_make_empty_records(code_parsed_pairs, f"YahooQuery 错误: {e}"))

        return result

    except ImportError:
        return {"error": "yahooquery 未安装，请运行: pip install yahooquery"}
    except Exception as e:
        return {"error": f"YahooQuery 降级通道异常: {str(e)}"}


def _make_empty_records(code_parsed_pairs, reason: str) -> list:
    """为解析失败的代码生成占位记录"""
    records = []
    for futu_code, parsed in code_parsed_pairs:
        records.append({
            "futu_code":   futu_code,
            "last_price":  None,
            "volume":      None,
            "open_interest": None,
            "implied_volatility": None,
            "strike_price": parsed.get("strike"),
            "option_type":  "Call" if parsed.get("opt_type") == "C" else "Put",
            "expiry_date":  parsed.get("expiry_date"),
            "_source":      f"Error ({reason})",
        })
    return records


# ==========================================
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 接口层（MCP Tools）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@mcp.tool()
def get_option_expiry_dates(code: str) -> dict:
    """
    【第一步】获取指定股票的期权链所有到期日。

    参数:
    - code: 富途格式股票代码，如 "US.AAPL"、"HK.00700"

    返回 data 为到期日记录列表，每条包含:
    - strike_time: 到期日字符串 (YYYY-MM-DD)
    - option_expiry_date_distance: 距今天数 (正数为未来)
    - expiration_cycle: 交割周期 (WEEK/MONTH，港股指数期权/美股指数期权有值)

    进行期权分析的第一步，先获取所有可用到期日，再选择目标到期日进行后续查询。
    """
    try:
        data = _logic_get_expiry_dates(code)
        return _ok(
            data,
            "请列出最近 5 个未到期的到期日（按日期升序），询问用户想分析哪一天的期权链。"
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_option_chain_by_date(
    code: str,
    expiry_date: str,
    option_type: str = "ALL",
    option_cond_type: str = "ALL"
) -> dict:
    """
    【第二步】获取指定股票在某一到期日的期权合约列表（静态信息）。

    参数:
    - code: 富途格式标的代码，如 "US.AAPL"、"HK.00700"
    - expiry_date: 到期日，支持 "YYYY-MM-DD" 或 "YYYYMMDD" 格式
    - option_type: 过滤看涨/看跌，可选 "ALL"(默认)/"CALL"/"PUT"
    - option_cond_type: 过滤价内外，可选 "ALL"(默认)/"WITHIN"(价内)/"OUTSIDE"(价外)

    返回每份合约的 futu_code、option_type、strike_price 等静态字段。
    注意: 此接口不含实时行情(价格/IV/OI)，需进一步调用 get_option_market_data。
    """
    try:
        data = _logic_get_option_chain(code, expiry_date, option_type, option_cond_type)
        return _ok(
            data,
            (
                "请总结该到期日的行权价分布范围（最低/最高行权价）。"
                "找出当前股价附近的平值(ATM) Call 和 Put 合约代码，备用于下一步行情查询。"
                "无需列出所有合约，重点展示行权价分布和合约数量即可。"
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_option_market_data(futu_code_list: list[str]) -> dict:
    """
    【第三步】批量获取期权合约实时行情（含 IV、OI、Greeks）。

    参数:
    - futu_code_list: 富途期权代码列表，如 ["US.AAPL260117C230000", "US.AAPL260117P200000"]
                      代码来自 get_option_chain_by_date 的 futu_code 字段
                      支持最多 200 个合约同时查询（富途快照接口限制）

    智能路由:
    - 主通道: 富途官方 API (get_market_snapshot) → 包含完整 Greeks 和 IV
    - 降级通道: YahooQuery (仅美股，当富途返回权限错误时自动切换) → IV/OI 有值，Greeks 为空

    每条记录的 _source 字段标注数据来源。
    """
    try:
        if not futu_code_list:
            return _err("futu_code_list 不能为空")
        if len(futu_code_list) > 200:
            return _err("单次查询上限 200 个合约，请分批调用")

        data = _logic_get_option_snapshots(futu_code_list)

        # 汇总统计
        summary = {}
        if isinstance(data, list) and data:
            # OI 最高的合约（暗示关键行权价/最大痛点）
            with_oi = [r for r in data if r.get("open_interest") and r.get("open_interest", 0) > 0]
            if with_oi:
                max_oi_record = max(with_oi, key=lambda x: x["open_interest"])
                summary["max_oi"] = {
                    "futu_code":     max_oi_record.get("futu_code"),
                    "option_type":   max_oi_record.get("option_type"),
                    "strike_price":  max_oi_record.get("strike_price"),
                    "open_interest": max_oi_record.get("open_interest"),
                }
            sources = list({r.get("_source", "Unknown") for r in data})
            summary["data_sources"] = sources
            summary["total_contracts"] = len(data)

        return _ok(
            {"quotes": data, "summary": summary},
            (
                "请作为期权分析师，重点解读以下关键指标：\n"
                "1. **IV (隐含波动率)**：较高的 IV 意味着市场预期更大波动，期权定价更贵，卖方策略更有利。\n"
                "2. **OI (未平仓量)**：OI 最高的行权价通常是市场关注的关键支撑/阻力位（最大痛点）。\n"
                "3. **Greeks**: Delta 反映方向敏感性，Theta 反映时间损耗，Vega 反映波动率敏感性。\n"
                "4. 如果 _source 为 YahooQuery，说明富途权限不足，Greeks 数据不可用。\n"
                "请基于 summary 中的 max_oi 数据，指出最可能的关键压力/支撑位。"
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def get_option_full_analysis(
    code: str,
    expiry_date: str,
    option_type: str = "ALL",
    max_contracts: int = 20
) -> dict:
    """
    🔥 【聚合工具】一步完成期权链查询 + 实时行情拉取，适合快速分析单一到期日全链数据。

    参数:
    - code: 富途标的代码，如 "US.AAPL"
    - expiry_date: 目标到期日，格式 "YYYY-MM-DD" 或 "YYYYMMDD"
    - option_type: "ALL"(默认) / "CALL" / "PUT"
    - max_contracts: 最多拉取行情的合约数(默认20，最大200)。链条过长时自动截取中间平值附近合约。

    流程: 先拉静态链 → 选出目标合约代码 → 批量拉实时快照(含降级)
    """
    try:
        # Step 1: 获取期权链静态信息
        chain = _logic_get_option_chain(code, expiry_date, option_type)
        if isinstance(chain, dict) and "error" in chain:
            return chain  # 直接透传错误

        if not chain:
            return _err(f"在 {expiry_date} 找不到 {code} 的期权链数据")

        # Step 2: 筛选不停牌的合约，限制数量
        active = [c for c in chain if not c.get("suspension", False)]
        total_chain_count = len(active)

        if len(active) > max_contracts:
            # 按行权价排序，取中间段（平值附近）
            active_sorted = sorted(active, key=lambda x: x.get("strike_price") or 0)
            mid = len(active_sorted) // 2
            half = max_contracts // 2
            active = active_sorted[max(0, mid - half): mid + half]

        # Step 3: 提取代码，批量拉行情
        futu_codes = [c["futu_code"] for c in active if c.get("futu_code")]
        snapshots = _logic_get_option_snapshots(futu_codes) if futu_codes else []

        # Step 4: 将快照数据 key 化，merge 回静态链
        snap_map = {}
        if isinstance(snapshots, list):
            snap_map = {s["futu_code"]: s for s in snapshots}

        merged = []
        for c in active:
            fc = c.get("futu_code", "")
            snap = snap_map.get(fc, {})
            merged.append({**c, **snap})

        # Step 5: 计算 OI 分布（识别最大痛点行权价）
        call_oi: dict[float, int] = {}
        put_oi:  dict[float, int] = {}
        for item in merged:
            sp   = item.get("strike_price") or item.get("option_strike_price")
            oi   = item.get("open_interest") or 0
            otype = str(item.get("option_type", "")).upper()
            if sp is None or oi == 0:
                continue
            if "CALL" in otype:
                call_oi[sp] = call_oi.get(sp, 0) + oi
            elif "PUT" in otype:
                put_oi[sp]  = put_oi.get(sp, 0) + oi

        max_pain_strike = None
        if call_oi or put_oi:
            all_strikes = set(list(call_oi.keys()) + list(put_oi.keys()))
            min_pain = float("inf")
            for sp in all_strikes:
                total_pain = sum(max(0, sp - k) * v for k, v in call_oi.items()) + \
                             sum(max(0, k - sp) * v for k, v in put_oi.items())
                if total_pain < min_pain:
                    min_pain = total_pain
                    max_pain_strike = sp

        data_sources = list({item.get("_source", "Unknown") for item in merged})

        return _ok(
            {
                "code":             code,
                "expiry_date":      expiry_date,
                "option_type":      option_type,
                "total_chain_count":total_chain_count,
                "fetched_count":    len(merged),
                "max_pain_strike":  max_pain_strike,
                "data_sources":     data_sources,
                "chain":            merged,
            },
            (
                "请扮演期权分析师，基于以下数据生成一份简洁的期权市场结构分析报告：\n"
                "1. **最大痛点 (Max Pain)**: max_pain_strike 是期权到期时，标的股价最可能收盘的行权价（使最多期权买家亏损）。\n"
                "2. **Call OI vs Put OI**: 分析 Call 和 Put 的未平仓量分布，识别关键支撑（高 Put OI）和阻力（高 Call OI）行权价。\n"
                "3. **IV 水平**: 比较不同行权价的隐含波动率，识别 IV 偏斜（Skew）方向（Put Skew 或 Call Skew）。\n"
                "4. **数据来源 (data_sources)**: 如含 YahooQuery，说明富途权限受限，Greeks 不可用，请在报告中注明。\n"
                "5. **操作建议**: 基于最大痛点和 OI 分布给出买方/卖方策略建议。"
            )
        )
    except Exception as e:
        return _err(e)


# ==========================================
# 生命周期管理工具
# ==========================================

@mcp.tool()
def close_connections() -> dict:
    """
    🔌【连接回收】主动断开 Futu OpenD 会话，释放连接配额。
    在长时间空闲或分析结束后调用。
    """
    try:
        close_context()
        return {"status": "SUCCESS", "message": "Futu Options OpenD 会话已断开。"}
    except Exception as e:
        return _err(e)


if __name__ == "__main__":
    mcp.run()
