"""
Notion 持仓价格同步器 (Notion Price Syncer)
=============================================
定时通过长桥 SDK 拉取指定标的的行情数据，自动更新 Notion 持仓表中的：
  - T-1 Closing Price：前一交易日收盘价（开盘前写入）
  - Current Price：盘中实时价格（每 5 分钟轮询，含盘前/盘后侦测）

调度规则：
  - 港股 (HK): Asia/Hong_Kong 时区，09:00 更新 T-1，09:30-16:10 每 5 分钟更新实时价
  - 美股 (US): America/New_York 时区，16:05 更新 T-1，04:00-20:00 每 5 分钟更新实时价

依赖环境变量: NOTION_TOKEN, DB_POS_HK, DB_POS_US,
              LONGBRIDGE_APP_KEY, LONGBRIDGE_APP_SECRET, LONGBRIDGE_ACCESS_TOKEN

架构说明:
  长桥 API 调用全部通过 longbridge/longbridge_server.py 中的封装函数完成，
  本文件不直接依赖 longbridge SDK。
"""

import os
import sys
import pytz
import time
import warnings
from datetime import datetime, timedelta
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
    _logic_get_history_kline,            # 获取历史 K 线 → 用于提取 T-1 收盘价
    _logic_get_live_quote,              # 获取港股实时价格（常规盘）
    _logic_get_extended_quote,          # 获取美股延伸时段最新价（含盘前/盘后/夜盘）
)
from longbridge.openapi import Period    # noqa: E402  仅用于 Period.Day 枚举常量

# 🤫 屏蔽 tzlocal 的无关告警
warnings.filterwarnings("ignore", module="tzlocal")

# ===========================================================================
# ⚙️ 配置层（全部从环境变量读取）
# ===========================================================================
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DB_POS_HK    = os.getenv("DB_POS_HK")
DB_POS_US    = os.getenv("DB_POS_US")

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


# ===========================================================================
# 🗂️ 任务 A：更新 T-1 收盘价（用历史日K线末条的收盘价）
# ===========================================================================

def update_t1_price(market: str, ds_id: str):
    """
    开盘前触发：通过 _logic_get_history_kline 取近 7 日日K线，
    提取最后一条的收盘价写入 Notion 持仓表的 T-1 Closing Price 字段。
    """
    print(f"\n🌅 [{market}] 更新 T-1 收盘价...")
    try:
        rows = _query_all_positions(ds_id)
        end_date   = datetime.now().date()
        start_date = end_date - timedelta(days=7)

        for row in rows:
            page_id  = row["id"]
            raw_code = row["properties"]["Stock Code"]["rich_text"][0]["plain_text"]
            lb_code  = get_lb_code(raw_code, market)

            try:
                klines = _logic_get_history_kline(lb_code, Period.Day, start_date, end_date)
                # 检查返回值是否有效（_logic_get_history_kline 失败时返回 [{"error": ...}]）
                if not klines or "error" in klines[0]:
                    print(f"⚠️ {raw_code} K 线拉取失败: {klines}")
                    continue

                t1_price = float(klines[-1]["c"])   # 最后一条的收盘价
                notion.pages.update(
                    page_id=page_id,
                    properties={"T-1 Closing Price": {"number": t1_price}}
                )
                print(f"✅ {raw_code} ({lb_code}) T-1 价格: {t1_price:.3f}")

            except Exception as e:
                print(f"❌ {raw_code} T-1 更新失败: {e}")

            time.sleep(0.5)  # 配合 Notion API 限流

    except Exception as e:
        print(f"❌ [{market}] T-1 整体任务崩溃: {e}")


# ===========================================================================
# 📡 任务 B：更新实时 Current Price
# ===========================================================================

def update_current_price(market: str, ds_id: str):
    """
    盘中每 5 分钟触发：
      - 港股: 调用 _logic_get_live_quote 获取常规盘实时价
      - 美股: 调用 _logic_get_extended_quote 在盘前/盘中/盘后/夜盘中自动选最新价
    """
    print(f"\n⚡ [{market}] 更新实时价格...")
    try:
        rows = _query_all_positions(ds_id)

        for row in rows:
            page_id  = row["id"]
            raw_code = row["properties"]["Stock Code"]["rich_text"][0]["plain_text"]
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

    except Exception as e:
        print(f"❌ [{market}] 实时价整体任务崩溃: {e}")


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
    # a. 开盘前 30 分钟 (09:00)：更新 T-1 收盘价
    scheduler.add_job(
        update_t1_price, "cron",
        day_of_week="mon-fri", hour=8, minute=30,
        timezone=tz_hk, args=["HK", DB_POS_HK]
    )
    # b. 盘中 (09:30-16:00)：每 5 分钟更新实时价
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=9, minute="30-59/5",
        timezone=tz_hk, args=["HK", DB_POS_HK], max_instances=1
    )
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour="10-15", minute="*/5",
        timezone=tz_hk, args=["HK", DB_POS_HK], max_instances=1
    )
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=16, minute=0,
        timezone=tz_hk, args=["HK", DB_POS_HK], max_instances=1
    )
    # c. 收盘后 10 分钟 (16:10)：写入最终收盘价
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=16, minute=10,
        timezone=tz_hk, args=["HK", DB_POS_HK]
    )

    # ──────────────────────────────────────────────────────────────
    # 🇺🇸 美股调度 (America/New_York)
    # ──────────────────────────────────────────────────────────────
    # a. 常规盘结束后 5 分钟 (16:05)：更新 T-1 收盘价
    scheduler.add_job(
        update_t1_price, "cron",
        day_of_week="mon-fri", hour=16, minute=5,
        timezone=tz_us, args=["US", DB_POS_US]
    )
    # b. 盘前+盘中+盘后 (04:00-20:00)：每 5 分钟更新延伸时段最新价
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour="4-19", minute="*/5",
        timezone=tz_us, args=["US", DB_POS_US], max_instances=1
    )
    scheduler.add_job(
        update_current_price, "cron",
        day_of_week="mon-fri", hour=20, minute=0,
        timezone=tz_us, args=["US", DB_POS_US], max_instances=1
    )

    print("🚀 Notion 价格同步调度器已启动（含盘前/盘后延伸时段侦测）")
    print(f"   港股: 09:00 更新 T-1，09:30-16:10 每 5 分钟实时价 [HK]")
    print(f"   美股: 16:05 更新 T-1，04:00-20:00 每 5 分钟延伸价 [US]")

    # 💡 调试时取消注释，立即跑一次：
    update_t1_price("US", DB_POS_US)
    # update_current_price("US", DB_POS_US)

    scheduler.start()