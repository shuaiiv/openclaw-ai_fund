"""
Notion 持仓价格同步器 (Notion Price Syncer)
=============================================
定时通过长桥 SDK 拉取指定标的的行情数据，自动更新 Notion 持仓表中的：
  - T-1 Closing Price：前一交易日收盘价（开盘前写入）
  - Current Price：盘中实时价格（每 5 分钟轮询，含盘前/盘后侦测）

调度规则：
  - 港股 (HK): Asia/Hong_Kong 时区，08:30 更新 T-1，09:25 创建/刷新当日盈亏快照，
              09:30-16:10 每 5 分钟更新实时价并刷新盈亏
  - 美股 (US): America/New_York 时区，04:00-15:55 每 5 分钟刷新当日盈亏；
              16:00 进入盘后时先切换 T-1 基准到当日常规盘收盘价；
              16:00-20:00 每 5 分钟更新延伸时段价并刷新下一交易日盈亏；
              20:05 用官方日 K close 修正 T-1 并刷新下一交易日盈亏

依赖环境变量: NOTION_TOKEN, DB_POS_HK, DB_POS_US,
              LONGBRIDGE_APP_KEY, LONGBRIDGE_APP_SECRET, LONGBRIDGE_ACCESS_TOKEN
可选环境变量: UPDATE_ONLY_POSITIVE_COUNT=true/false, POSITION_COUNT_PROPERTY=Count

架构说明:
  长桥 API 调用全部通过 longbridge/longbridge_server.py 中的封装函数完成，
  本文件不直接依赖 longbridge SDK。
"""

import os
import sys
import pytz
import time
import warnings
from datetime import datetime, time as datetime_time, timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from notion_client import Client
from dotenv import load_dotenv, find_dotenv

# ==========================================================
# 📦 路径设置：将 longbridge/ 加入 sys.path，以便寻址 longbridge_server
# 目录结构: for_openclaw/longbridge/, for_openclaw/notion/
# ==========================================================
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # for_openclaw/notion/
_ROOT_DIR   = os.path.dirname(_SCRIPT_DIR)                  # for_openclaw/
sys.path.insert(0, os.path.join(_ROOT_DIR, "longbridge"))  # 将 longbridge/ 加入搜索路径

# 延迟导入：等 dotenv 加载完毕再 import，避免长桥 SDK 在 .env 读取前初始化失败
load_dotenv(find_dotenv())

from longbridge_server import (          # noqa: E402
    _logic_get_live_quote,              # 获取实时行情；美股盘后初段可兜底取得常规盘 close
    _logic_get_extended_quote,          # 获取美股延伸时段最新价（含盘前/盘后/夜盘）
    _logic_get_history_kline,           # 获取历史日 K，用于官方收盘价口径
    _logic_get_trading_days,            # 判断是否为对应市场交易日
    Period,
)

from notion_database_manager import sync_daily_pnl_snapshot  # noqa: E402


# 🤫 屏蔽 tzlocal 的无关告警
warnings.filterwarnings("ignore", module="tzlocal")

# ===========================================================================
# ⚙️ 配置层（全部从环境变量读取）
# ===========================================================================
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DB_POS_HK    = os.getenv("DB_POS_HK")
DB_POS_US    = os.getenv("DB_POS_US")
POSITION_COUNT_PROPERTY = os.getenv("POSITION_COUNT_PROPERTY", "Count")
UPDATE_ONLY_POSITIVE_COUNT = os.getenv("UPDATE_ONLY_POSITIVE_COUNT", "true").strip().lower() not in {
    "0", "false", "no", "off"
}

notion = Client(auth=NOTION_TOKEN)


# ===========================================================================
# 🔧 工具函数
# ===========================================================================

def get_lb_code(raw_code: str, market: str) -> str:
    """
    将 Notion 存储格式的代码（如 HK.00700 / US.AAPL）
    转换为长桥标准格式（00700.HK / AAPL.US）。
    """
    if market == "HK":
        lb_code = raw_code.replace("HK.", "")
        return lb_code if lb_code.endswith(".HK") else f"{lb_code}.HK"
    else:
        lb_code = raw_code.replace("US.", "")
        return lb_code if lb_code.endswith(".US") else f"{lb_code}.US"


def _plain_text_property(row: dict, prop_name: str) -> str:
    """读取 Notion title/rich_text 属性的纯文本。"""
    prop = row.get("properties", {}).get(prop_name, {})
    prop_type = prop.get("type")
    if prop_type not in {"title", "rich_text"}:
        return ""
    return "".join(part.get("plain_text", "") for part in prop.get(prop_type, [])).strip()


def _number_property(row: dict, prop_name: str):
    """读取 Notion number 属性；兼容返回 number 的 formula。"""
    prop = row.get("properties", {}).get(prop_name, {})
    prop_type = prop.get("type")
    if prop_type == "number":
        return prop.get("number")
    if prop_type == "formula":
        formula = prop.get("formula", {})
        if formula.get("type") == "number":
            return formula.get("number")
    return None


def _should_update_price(row: dict) -> bool:
    """只允许持仓数量大于 0 的行更新价格。"""
    if not UPDATE_ONLY_POSITIVE_COUNT:
        return True

    count = _number_property(row, POSITION_COUNT_PROPERTY)
    raw_code = _plain_text_property(row, "Stock Code") or row.get("id", "unknown")
    if count is None:
        print(f"⚠️ {raw_code} 缺少 {POSITION_COUNT_PROPERTY} 持仓数量，跳过价格更新")
        return False

    try:
        if float(count) > 0:
            return True
    except (TypeError, ValueError):
        print(f"⚠️ {raw_code} 的 {POSITION_COUNT_PROPERTY}={count} 无法识别，跳过价格更新")
        return False

    print(f"⏭️ {raw_code} 持仓数量 {count} <= 0，跳过价格更新")
    return False


def _query_all_positions(ds_id: str) -> list:
    """读取 Notion 持仓表中所有行（翻页安全）"""
    results, has_more, cursor = [], True, None
    while has_more:
        resp = notion.data_sources.query(
            data_source_id=ds_id,
            **({"start_cursor": cursor} if cursor else {})
        )
        results.extend(resp.get("results", []))
        has_more = resp.get("has_more", False)
        cursor   = resp.get("next_cursor")
        if len(results) > 2000:
            break
    return results


def _is_trading_day(market: str) -> bool:
    """按对应市场本地日期判断今天是否交易日；异常时保守放行。"""
    try:
        result = _logic_get_trading_days(market)
        if "error" in result:
            print(f"⚠️ [{market}] 交易日查询失败，默认继续执行: {result['error']}")
            return True
        if not result.get("is_today_trading_day"):
            print(f"📅 [{market}] 今日非交易日，跳过本次价格/盈亏同步")
            return False
        return True
    except Exception as e:
        print(f"⚠️ [{market}] 交易日判断异常，默认继续执行: {e}")
        return True


# ===========================================================================
# 🗂️ 任务 A：更新 T-1 收盘价（用历史日 K 线末条的收盘价）
# ===========================================================================

def _date_key(value) -> str:
    return str(value or "")[:10]


def _next_weekday(date_value):
    next_day = date_value + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day


def _next_trading_day(market: str, date_value) -> str:
    """Return the next market trading day after date_value."""
    start = (date_value + timedelta(days=1)).isoformat()
    end = (date_value + timedelta(days=10)).isoformat()
    try:
        result = _logic_get_trading_days(market, start, end)
        if "error" not in result:
            for item in result.get("trade_days", []):
                item_date = item.get("date", "")
                if item_date > date_value.isoformat():
                    return item_date
        print(f"⚠️ [{market}] 下一交易日查询失败，退回工作日估算: {result.get('error', result)}")
    except Exception as e:
        print(f"⚠️ [{market}] 下一交易日查询异常，退回工作日估算: {e}")
    return _next_weekday(date_value).isoformat()


def _us_pnl_snapshot_date() -> str:
    """美股 16:00 ET 之后的盘后价格归入下一交易日快照。"""
    now_us = datetime.now(pytz.timezone("America/New_York"))
    if now_us.time() >= datetime_time(16, 0):
        return _next_trading_day("US", now_us.date())
    return now_us.date().isoformat()


def _pnl_snapshot_date_for_market(market: str) -> str | None:
    if market == "US":
        return _us_pnl_snapshot_date()
    return None


def _latest_daily_close(lb_code: str, target_date: str | None = None, allow_live_fallback: bool = False):
    """取最近一根历史日 K 的 close，保证和每日盈亏快照口径一致。"""
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=10)
    data = _logic_get_history_kline(lb_code, Period.Day, start_date, end_date)
    if data and isinstance(data[0], dict) and "error" in data[0]:
        if allow_live_fallback:
            result = _logic_get_live_quote(lb_code)
            if "error" not in result and result.get("price"):
                return float(result["price"]), f"{target_date or end_date.isoformat()} live_close_fallback"
        return None, data[0]["error"]

    daily_rows = [
        item for item in data
        if item.get("t") and item.get("c") is not None
    ]
    if not daily_rows:
        if allow_live_fallback:
            result = _logic_get_live_quote(lb_code)
            if "error" not in result and result.get("price"):
                return float(result["price"]), f"{target_date or end_date.isoformat()} live_close_fallback"
        return None, "历史日 K 为空"

    latest = daily_rows[-1]
    latest_date = _date_key(latest.get("t"))
    if allow_live_fallback and target_date and latest_date < target_date:
        result = _logic_get_live_quote(lb_code)
        if "error" not in result and result.get("price"):
            return float(result["price"]), f"{target_date} live_close_fallback"
    return float(latest["c"]), latest["t"]


def update_t1_price(market: str, ds_id: str, allow_live_fallback: bool = False):
    """
    更新 T-1 收盘价。

    使用历史日 K 最新一根 close，而不是 quote.last_done。quote 在收盘后/盘后
    可能和官方日 K close 存在微小差异；每日盈亏快照也使用这个口径。
    """
    print(f"\n🌅 [{market}] 更新 T-1 收盘价...")
    if not _is_trading_day(market):
        return

    try:
        rows = _query_all_positions(ds_id)

        for row in rows:
            if not _should_update_price(row):
                continue

            page_id  = row["id"]
            raw_code = _plain_text_property(row, "Stock Code")
            if not raw_code:
                print(f"⚠️ {page_id} 缺少 Stock Code，跳过")
                continue
            lb_code  = get_lb_code(raw_code, market)

            try:
                target_date = datetime.now(pytz.timezone("America/New_York")).date().isoformat() if market == "US" else None
                t1_price, price_date = _latest_daily_close(lb_code, target_date, allow_live_fallback)
                if not t1_price:
                    print(f"⚠️ {raw_code} 历史日 K 获取失败: {price_date}")
                    continue

                notion.pages.update(
                    page_id=page_id,
                    properties={"T-1 Closing Price": {"number": t1_price}}
                )
                print(f"✅ {raw_code} ({lb_code}) T-1 收盘价: {t1_price:.3f} [{price_date}]")

            except Exception as e:
                print(f"❌ {raw_code} T-1 更新失败: {e}")

            time.sleep(0.5)  # 配合 Notion API 限流

    except Exception as e:
        print(f"❌ [{market}] T-1 整体任务崩溃: {e}")



# ===========================================================================
# 📡 任务 B：更新实时 Current Price
# ===========================================================================

def update_current_price(market: str, ds_id: str, phase: str = "current"):
    """
    盘中每 5 分钟触发：
      - 港股: 调用 _logic_get_live_quote 获取常规盘实时价
      - 美股: 调用 _logic_get_extended_quote 在盘前/盘中/盘后/夜盘中自动选最新价
    """
    print(f"\n⚡ [{market}] 更新价格并刷新每日盈亏... phase={phase}")
    if not _is_trading_day(market):
        return

    try:
        rows = _query_all_positions(ds_id)

        for row in rows:
            if not _should_update_price(row):
                continue

            page_id  = row["id"]
            raw_code = _plain_text_property(row, "Stock Code")
            if not raw_code:
                print(f"⚠️ {page_id} 缺少 Stock Code，跳过")
                continue
            lb_code  = get_lb_code(raw_code, market)

            try:
                if market == "HK":
                    # ── 港股：常规盘实时价即可 ──────────────────────────────
                    result = _logic_get_live_quote(lb_code)
                    if "error" in result:
                        print(f"⚠️ {raw_code} 实时行情失败: {result['error']}")
                        continue
                    current_price = result["price"]
                    session_tag   = "regular"

                else:
                    # ── 美股：延伸时段智能选最新价 ─────────────────────────
                    result = _logic_get_extended_quote(lb_code)
                    if "error" in result:
                        print(f"⚠️ {raw_code} 延伸行情失败: {result['error']}")
                        continue
                    current_price = result["price"]
                    session_tag   = result.get("session", "")

                notion.pages.update(
                    page_id=page_id,
                    properties={"Current Price": {"number": current_price}}
                )
                print(f"✅ {raw_code} ({lb_code}) 实时价: {current_price:.3f}  [{session_tag}]")

            except Exception as e:
                print(f"❌ {raw_code} 实时价更新失败: {e}")

            time.sleep(0.5)

        snapshot_date = _pnl_snapshot_date_for_market(market)
        pnl_result = sync_daily_pnl_snapshot(market=market, snapshot_date=snapshot_date)
        if pnl_result["status"] in {"success", "warning"}:
            print(f"📈 [{market}] 每日盈亏快照已同步: {pnl_result['msg']}")
        else:
            print(f"⚠️ [{market}] 每日盈亏快照同步失败: {pnl_result['msg']}")

    except Exception as e:
        print(f"❌ [{market}] 实时价整体任务崩溃: {e}")


def update_us_afterhours_open():
    """美股盘后开始：先切换 T-1 基准到当日收盘，再写入下一交易日快照。"""
    if not _is_trading_day("US"):
        return
    update_t1_price("US", DB_POS_US, True)
    update_current_price("US", DB_POS_US, "afterhours_open")


def update_us_t1_and_next_pnl():
    """美股盘后结束后：用官方日 K close 修正 T-1，并刷新下一交易日快照。"""
    if not _is_trading_day("US"):
        return
    update_t1_price("US", DB_POS_US)
    snapshot_date = _us_pnl_snapshot_date()
    pnl_result = sync_daily_pnl_snapshot(market="US", snapshot_date=snapshot_date)
    if pnl_result["status"] in {"success", "warning"}:
        print(f"📈 [US] 官方收盘价修正后，每日盈亏快照已同步: {pnl_result['msg']}")
    else:
        print(f"⚠️ [US] 官方收盘价修正后，每日盈亏快照同步失败: {pnl_result['msg']}")


# ===========================================================================
# 🕐 主调度器
# ===========================================================================

if __name__ == "__main__":
    job_defaults = {
        "misfire_grace_time": 120,  # 允许任务最多迟到 2 分钟，绝不漏单
        "coalesce": True,           # 任务积压时合并为一次执行，防止雪崩
    }
    scheduler = BlockingScheduler(job_defaults=job_defaults)
    tz_hk = pytz.timezone("Asia/Hong_Kong")
    tz_us = pytz.timezone("America/New_York")

    # ──────────────────────────────────────────────────────────────
    # 🇭🇰 港股调度 (Asia/Hong_Kong)
    # ──────────────────────────────────────────────────────────────
    # a. 开盘前 (08:30)：更新 T-1 收盘价
    scheduler.add_job(
        update_t1_price, "cron",
        day_of_week="mon-fri", hour=8, minute=30,
        timezone=tz_hk, args=["HK", DB_POS_HK]
    )
    # b. 开盘前 5 分钟 (09:25)：创建/刷新当日盈亏快照；若集合竞价价可得则写入
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=9, minute=25,
        timezone=tz_hk, args=["HK", DB_POS_HK, "open"], max_instances=1
    )
    # c. 盘中 (09:30-16:00)：每 5 分钟更新实时价
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=9, minute="30-59/5",
        timezone=tz_hk, args=["HK", DB_POS_HK, "intraday"], max_instances=1
    )
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour="10-15", minute="*/5",
        timezone=tz_hk, args=["HK", DB_POS_HK, "intraday"], max_instances=1
    )
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=16, minute=0,
        timezone=tz_hk, args=["HK", DB_POS_HK, "intraday"], max_instances=1
    )
    # d. 收盘后 10 分钟 (16:10)：写入最终收盘价并刷新当日盈亏
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=16, minute=10,
        timezone=tz_hk, args=["HK", DB_POS_HK, "close"]
    )

    # ──────────────────────────────────────────────────────────────
    # 🇺🇸 美股调度 (America/New_York)
    # ──────────────────────────────────────────────────────────────
    # a. 盘前+盘中 (04:00-15:55)：每 5 分钟刷新当日快照。
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour="4-15", minute="*/5",
        timezone=tz_us, args=["US", DB_POS_US, "us_regular_window"], max_instances=1
    )
    # b. 盘后开始 (16:00)：先把 T-1 切到当日常规盘收盘价，再创建/刷新下一交易日快照。
    scheduler.add_job(
        update_us_afterhours_open, "cron",
        day_of_week="mon-fri", hour=16, minute=0,
        timezone=tz_us, max_instances=1
    )
    # c. 盘后 (16:05-20:00)：每 5 分钟刷新下一交易日快照。
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=16, minute="5-59/5",
        timezone=tz_us, args=["US", DB_POS_US, "us_afterhours"], max_instances=1
    )
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour="17-19", minute="*/5",
        timezone=tz_us, args=["US", DB_POS_US, "us_afterhours"], max_instances=1
    )
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=20, minute=0,
        timezone=tz_us, args=["US", DB_POS_US, "us_afterhours"], max_instances=1
    )
    # d. 盘后结束后 5 分钟 (20:05)：用官方日 K close 修正 T-1，并刷新下一交易日快照。
    scheduler.add_job(
        update_us_t1_and_next_pnl, "cron",
        day_of_week="mon-fri", hour=20, minute=5,
        timezone=tz_us, max_instances=1
    )

    print("🚀 Notion 价格同步调度器已启动（含盘前/盘后延伸时段侦测）")
    print(f"   港股: 08:30 更新 T-1，09:25 建立/刷新当日快照，09:30-16:10 每 5 分钟实时价 [HK]")
    print(f"   美股: 04:00-15:55 刷新当日快照；16:00-20:00 盘后价写入下一交易日；20:05 官方收盘价修正 [US]")

    # 💡 调试时取消注释，立即跑一次：
    # update_t1_price("US", DB_POS_US)
    # update_current_price("US", DB_POS_US)

    scheduler.start()
