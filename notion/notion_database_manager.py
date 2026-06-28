from notion_client import Client
import csv
import os
import datetime
import logging
from collections import defaultdict

from dotenv import load_dotenv, find_dotenv

# 2. ⚡️ 将 find_dotenv() 作为参数传给 load_dotenv()
load_dotenv(find_dotenv())
notion = Client(auth=os.getenv("NOTION_TOKEN"))

# 你的这 4 个常量里，装的其实已经是 Data Source ID 了
DB_POS_HK = os.getenv("DB_POS_HK")
DB_TRANS_HK = os.getenv("DB_TRANS_HK")
DB_POS_US = os.getenv("DB_POS_US")
DB_TRANS_US = os.getenv("DB_TRANS_US")
DB_DAILY_PNL_HK = os.getenv("DB_DAILY_PNL_HK")
DB_DAILY_PNL_US = os.getenv("DB_DAILY_PNL_US")

DEFAULT_HK_PLATFORM = os.getenv("DEFAULT_HK_PLATFORM", "Trade25")
REALIZED_PNL_PROPERTY = "Realized P&L"
DAILY_PNL_TITLE_PROPERTY = os.getenv("DAILY_PNL_TITLE_PROPERTY", "Name")
DAILY_PNL_DATE_PROPERTY = os.getenv("DAILY_PNL_DATE_PROPERTY", "Date")
DAILY_PNL_MARKET_PROPERTY = os.getenv("DAILY_PNL_MARKET_PROPERTY", "Market")
DAILY_PNL_PLATFORM_PROPERTY = os.getenv("DAILY_PNL_PLATFORM_PROPERTY", "Platform")
SKIP_REALIZED_PNL_KEYS = {
    ("HK", "Futu", "HK.0700"),
    ("HK", "Futu", "HK.00700"),
    ("HK", "Futu", "HK.03690"),
}
EXCLUDE_DAILY_PNL_KEYS = {
    ("HK", "Futu", "HK.0700"),
}
MANUAL_COST_DAILY_PNL_KEYS = {
    ("HK", "Futu", "HK.00700"),
}
EXCLUDE_DAILY_PNL_TRADES = {
    ("HK", "Futu", "HK.03690", "2023-07-31", "Sell"),
    ("HK", "Futu", "HK.00700", "2025-02-07", "Sell"),
    ("HK", "Futu", "HK.00700", "2025-02-13", "Sell"),
    ("HK", "Futu", "HK.00700", "2025-07-23", "Sell"),
}

# ==========================================
# 肌肉功能 1：记录交易流水 (升级版：自带持仓同步)
# ==========================================
def _normalize_platform(market: str, platform: str | None = None) -> str:
    if market != "HK":
        return ""

    normalized = (platform or DEFAULT_HK_PLATFORM).strip()
    if normalized.lower() == "futu":
        return "Futu"
    if normalized.lower() == "trade25":
        return "Trade25"
    raise ValueError(f"未知港股平台: {platform!r}，目前支持 Futu / Trade25")


def _group_key(market: str, code: str, platform: str = "") -> tuple[str, str, str]:
    return market, _normalize_platform(market, platform), code


def _is_skipped_realized_pnl_key(market: str, code: str, platform: str = "") -> bool:
    return _group_key(market, code, platform) in SKIP_REALIZED_PNL_KEYS


def _is_excluded_daily_pnl_key(market: str, code: str, platform: str = "") -> bool:
    return _group_key(market, code, platform) in EXCLUDE_DAILY_PNL_KEYS


def _is_manual_daily_pnl_cost_key(market: str, code: str, platform: str = "") -> bool:
    return _group_key(market, code, platform) in MANUAL_COST_DAILY_PNL_KEYS


def _is_excluded_daily_pnl_trade(market: str, trade: dict) -> bool:
    key = (
        market,
        _normalize_platform(market, trade.get("Platform")) if market == "HK" else "",
        trade.get("Stock Code", ""),
        _date_key(trade.get("Date", "")),
        trade.get("Action", ""),
    )
    return key in EXCLUDE_DAILY_PNL_TRADES


def record_transaction(
    market: str,
    action: str,
    name: str,
    code: str,
    date: str,
    amount: int,
    price: float,
    fee: float,
    platform: str | None = None,
):
    ds_id = DB_TRANS_HK if market == "HK" else DB_TRANS_US
    try:
        platform = _normalize_platform(market, platform)
        is_futu_zero_allotment = market == "HK" and platform == "Futu" and action == "Buy" and int(amount) == 0
        if action not in {"Buy", "Sell"}:
            return {"status": "error", "msg": "❌ 未知的交易动作"}
        if int(amount) <= 0 and not is_futu_zero_allotment:
            return {"status": "error", "msg": "❌ 交易数量必须大于 0"}
        if float(price) < 0 or float(fee) < 0:
            return {"status": "error", "msg": "❌ 价格和手续费不能为负数"}

        if action == "Sell":
            current_count, _ = _rebuild_position_from_transactions(market, code, platform)
            if int(amount) > current_count:
                return {
                    "status": "error",
                    "msg": f"❌ 卖出数量超过持仓：当前 {code} {platform or ''} 只有 {current_count} 股，不能卖出 {amount} 股"
                }

        # 1. 先写流水账
        logging.info(f"[流水] 开始写入: {market} {platform} {action} {code} x{amount} @{price}, fee={fee}")
        properties = {
            "Stock Name": {"title": [{"text": {"content": name}}]},
            "Stock Code": {"rich_text": [{"text": {"content": code}}]},
            "Action": {"select": {"name": action}},
            "Date": {"date": {"start": date}},
            "Price": {"number": float(price)},
            "Count": {"number": int(amount)},
            "Trade Fee": {"number": float(fee)}
        }
        if market == "HK":
            properties["Platform"] = {"select": {"name": platform}}

        trans_resp = notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": ds_id},
            properties=properties
        )
        logging.info(f"[流水] 写入成功, page_id={trans_resp.get('id')}")

        if is_futu_zero_allotment:
            pnl_result = sync_realized_pnl(market, code=code, platform=platform)
            daily_pnl_result = sync_daily_pnl_snapshot(market=market, snapshot_date=date, platform=platform)
            pnl_msg = pnl_result.get("msg", "")
            daily_pnl_msg = daily_pnl_result.get("msg", "")
            if pnl_result["status"] in {"success", "warning"} and daily_pnl_result["status"] in {"success", "warning"}:
                return {"status": "success", "msg": f"✅ 打新未中流水记录成功，已计入已实现盈亏和每日盈亏！({pnl_msg}；{daily_pnl_msg})"}
            return {"status": "warning", "msg": f"⚠️ 打新未中流水已记录，但盈亏同步失败: Realized={pnl_msg}; Daily={daily_pnl_msg}"}
        
        # 2. 🚀 流水写入成功后，自动触发持仓更新逻辑！
        pos_result = update_position(market, name, code, action, amount, price, fee, platform)
        pnl_result = sync_realized_pnl(market, code=code, platform=platform)
        daily_pnl_result = sync_daily_pnl_snapshot(market=market, snapshot_date=date, platform=platform)
        
        # 3. 将两者的结果打包返回给大模型
        if pos_result["status"] != "success":
            return {"status": "warning", "msg": f"⚠️ 流水记录成功，但持仓同步失败: {pos_result['msg']}"}

        pnl_msg = pnl_result.get("msg", "")
        daily_pnl_msg = daily_pnl_result.get("msg", "")
        if pnl_result["status"] == "success" and daily_pnl_result["status"] == "success":
            return {"status": "success", "msg": f"✅ 流水记录成功，且已同步持仓、已实现盈亏与每日盈亏！({pos_result['msg']}；{pnl_msg}；{daily_pnl_msg})"}
        return {"status": "warning", "msg": f"⚠️ 流水和持仓已同步，但部分盈亏同步未完全成功: Realized={pnl_msg}; Daily={daily_pnl_msg}"}
            
    except Exception as e:
        logging.error(f"[流水] 写入失败: {str(e)}")
        return {"status": "error", "msg": f"❌ 流水记录失败: {str(e)}"}

# ==========================================
# 肌肉功能 2：更新持仓数据
# ==========================================
def _prop_number(props: dict, name: str) -> float:
    data = props.get(name, {})
    if data.get("type") == "number":
        return float(data.get("number") or 0)
    if data.get("type") == "formula":
        formula = data.get("formula", {})
        if formula.get("type") == "number":
            return float(formula.get("number") or 0)
    return 0.0


def _prop_text(props: dict, name: str) -> str:
    data = props.get(name, {})
    p_type = data.get("type")
    if p_type not in {"title", "rich_text"}:
        return ""
    return "".join(part.get("plain_text", "") for part in data.get(p_type, [])).strip()


def _prop_select(props: dict, name: str) -> str:
    data = props.get(name, {})
    selected = data.get("select") if data.get("type") == "select" else None
    return selected.get("name", "") if selected else ""


def _prop_date(props: dict, name: str) -> str:
    data = props.get(name, {})
    value = data.get("date") if data.get("type") == "date" else None
    return value.get("start", "") if value else ""


def _prop_created_time(props: dict, name: str) -> str:
    data = props.get(name, {})
    return data.get("created_time", "") if data.get("type") == "created_time" else ""


def _date_key(raw_date: str) -> str:
    """Return YYYY-MM-DD from a Notion date/datetime string."""
    return (raw_date or "")[:10]


def _query_all_rows(ds_id: str, limit: int = 5000) -> list:
    results, has_more, cursor = [], True, None

    while has_more:
        resp = notion.data_sources.query(
            data_source_id=ds_id,
            **({"start_cursor": cursor} if cursor else {})
        )
        results.extend(resp.get("results", []))
        has_more = resp.get("has_more", False)
        cursor = resp.get("next_cursor")
        if len(results) > limit:
            break

    return results


def _query_transactions_for_code(market: str, code: str, platform: str | None = None) -> list:
    ds_id = DB_TRANS_HK if market == "HK" else DB_TRANS_US
    results, has_more, cursor = [], True, None
    filters = [{"property": "Stock Code", "rich_text": {"equals": code}}]
    if market == "HK":
        filters.append({"property": "Platform", "select": {"equals": _normalize_platform(market, platform)}})
    query_filter = filters[0] if len(filters) == 1 else {"and": filters}

    while has_more:
        resp = notion.data_sources.query(
            data_source_id=ds_id,
            filter=query_filter,
            **({"start_cursor": cursor} if cursor else {})
        )
        results.extend(resp.get("results", []))
        has_more = resp.get("has_more", False)
        cursor = resp.get("next_cursor")
        if len(results) > 5000:
            break

    return results


def _trade_from_row(market: str, row: dict) -> dict | None:
    props = row.get("properties", {})
    action = _prop_select(props, "Action")
    count = int(_prop_number(props, "Count"))
    platform = _normalize_platform(market, _prop_select(props, "Platform")) if market == "HK" else ""
    is_futu_zero_allotment = market == "HK" and platform == "Futu" and action == "Buy" and count == 0
    if action not in {"Buy", "Sell"} or count < 0:
        return None
    if count == 0 and not is_futu_zero_allotment:
        return None

    return {
        "Notion Page ID": row.get("id", ""),
        "Created time": _prop_created_time(props, "Created time") or row.get("created_time", ""),
        "Date": _prop_date(props, "Date"),
        "Action": action,
        "Count": count,
        "Price": _prop_number(props, "Price"),
        "Trade Fee": _prop_number(props, "Trade Fee"),
        "Stock Name": _prop_text(props, "Stock Name"),
        "Stock Code": _prop_text(props, "Stock Code"),
        "Platform": platform,
    }


def _rebuild_lots_and_realized_pnl(trades: list, code: str) -> tuple[int, float, float]:
    trades.sort(key=lambda x: (x["Date"], x["Created time"], x["Notion Page ID"]))

    lots = []
    realized_pnl = 0.0
    total_buy_count = 0
    total_buy_cost = 0.0
    for trade in trades:
        if trade["Action"] == "Buy":
            if trade["Count"] == 0:
                realized_pnl -= trade["Trade Fee"]
                continue

            unit_cost = ((trade["Count"] * trade["Price"]) + trade["Trade Fee"]) / trade["Count"]
            lots.append({"Count": trade["Count"], "Unit Price": unit_cost})
            total_buy_count += trade["Count"]
            total_buy_cost += trade["Count"] * unit_cost
            continue

        sell_count = trade["Count"]
        held_count = sum(lot["Count"] for lot in lots)
        if sell_count > held_count:
            raise ValueError(
                f"{code} 在 {trade['Date']} 卖出 {sell_count} 股，但此前可用持仓只有 {held_count} 股"
            )

        cost_basis = 0.0
        lots.sort(key=lambda x: x["Unit Price"])
        while sell_count > 0:
            lot = lots[0]
            used = min(sell_count, lot["Count"])
            cost_basis += used * lot["Unit Price"]
            lot["Count"] -= used
            sell_count -= used

            if lot["Count"] == 0:
                lots.pop(0)

        realized_pnl += (trade["Count"] * trade["Price"]) - trade["Trade Fee"] - cost_basis

    new_count = int(sum(lot["Count"] for lot in lots))
    total_cost = sum(lot["Count"] * lot["Unit Price"] for lot in lots)
    new_unit_price = total_cost / new_count if new_count > 0 else total_buy_cost / total_buy_count if total_buy_count > 0 else 0
    return new_count, new_unit_price, realized_pnl


def _rebuild_position_from_transactions(market: str, code: str, platform: str | None = None) -> tuple[int, float]:
    """
    Conservative cost model:
    - Buy creates a lot with fee included in unit cost.
    - Sell removes shares from the lowest-cost lots first.

    The remaining lots therefore keep a deliberately higher cost basis, which
    is useful as a risk-control / psychology cost instead of broker accounting.
    """
    rows = _query_transactions_for_code(market, code, platform)
    trades = []

    for row in rows:
        trade = _trade_from_row(market, row)
        if trade:
            trades.append(trade)

    new_count, new_unit_price, _ = _rebuild_lots_and_realized_pnl(trades, code)
    return new_count, new_unit_price


def update_position(
    market: str,
    name: str,
    code: str,
    action: str,
    amount: int,
    price: float,
    fee: float,
    platform: str | None = None,
):
    ds_id = DB_POS_HK if market == "HK" else DB_POS_US
    try:
        platform = _normalize_platform(market, platform)
        if action not in {"Buy", "Sell"}:
            return {"status": "error", "msg": "未知的交易动作"}

        new_count, new_unit_price = _rebuild_position_from_transactions(market, code, platform)

        # 🎯 直接拿你的 ID 去查
        filters = [{"property": "Stock Code", "rich_text": {"equals": code}}]
        if market == "HK":
            filters.append({"property": "Platform", "select": {"equals": platform}})
        query_filter = filters[0] if len(filters) == 1 else {"and": filters}
        query = notion.data_sources.query(
            data_source_id=ds_id,
            filter=query_filter
        )
        
        if query['results']:
            page_id = query['results'][0]['id']
            notion.pages.update(
                page_id=page_id,
                properties={
                    "Count": {"number": new_count},
                    "Unit Price": {"number": round(new_unit_price, 4)}
                }
            )
            return {"status": "success", "msg": f"已更新持仓: {code} 数量 {new_count}, 保守成本 {new_unit_price:.2f}"}
        else:
            if action == "Sell" or new_count <= 0:
                return {"status": "error", "msg": "没有持仓无法卖出！"}
            logging.info(f"[持仓] 新建仓位: {code}")
            properties = {
                "Stock Name": {"title": [{"text": {"content": name}}]},
                "Stock Code": {"rich_text": [{"text": {"content": code}}]},
                "Count": {"number": new_count},
                "Unit Price": {"number": round(new_unit_price, 4)}
            }
            if market == "HK":
                properties["Platform"] = {"select": {"name": platform}}
            notion.pages.create(
                parent={"type": "data_source_id", "data_source_id": ds_id},
                properties=properties
            )
            return {"status": "success", "msg": f"已新建仓位: {code} 数量 {new_count}, 保守成本 {new_unit_price:.2f}"}
    except Exception as e:
        logging.error(f"[持仓] 更新失败: {str(e)}")
        return {"status": "error", "msg": str(e)}


def _query_positions(market: str) -> list:
    ds_id = DB_POS_HK if market == "HK" else DB_POS_US
    return _query_all_rows(ds_id)


def _position_key(market: str, row: dict) -> tuple[str, str, str]:
    props = row.get("properties", {})
    return _group_key(
        market,
        _prop_text(props, "Stock Code"),
        _prop_select(props, "Platform") if market == "HK" else "",
    )


def _create_position_for_realized_pnl(market: str, name: str, code: str, platform: str, count: int, unit_price: float, realized_pnl: float):
    ds_id = DB_POS_HK if market == "HK" else DB_POS_US
    properties = {
        "Stock Name": {"title": [{"text": {"content": name or code}}]},
        "Stock Code": {"rich_text": [{"text": {"content": code}}]},
        "Count": {"number": int(count)},
        "Unit Price": {"number": round(unit_price, 4)},
        REALIZED_PNL_PROPERTY: {"number": round(realized_pnl, 4)},
    }
    if market == "HK":
        properties["Platform"] = {"select": {"name": platform}}

    notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": ds_id},
        properties=properties,
    )


def sync_realized_pnl(market: str | None = None, code: str | None = None, platform: str | None = None):
    """
    Rebuild realized P&L from transaction history and write it to Position rows.

    HK positions are keyed by (Stock Code, Platform). US positions are keyed by
    Stock Code only. HK Futu HK.0700 / HK.00700 are intentionally skipped.
    """
    try:
        markets = [market] if market else ["HK", "US"]
        updated = 0
        created = 0
        skipped = 0
        errors = []

        for current_market in markets:
            platform_filter = _normalize_platform(current_market, platform) if platform and current_market == "HK" else None
            trans_ds_id = DB_TRANS_HK if current_market == "HK" else DB_TRANS_US
            transactions = _query_all_rows(trans_ds_id)
            grouped_trades = defaultdict(list)

            for row in transactions:
                trade = _trade_from_row(current_market, row)
                if not trade:
                    continue
                if code and trade["Stock Code"] != code:
                    continue
                if platform_filter and trade["Platform"] != platform_filter:
                    continue

                key = _group_key(current_market, trade["Stock Code"], trade["Platform"])
                if _is_skipped_realized_pnl_key(current_market, trade["Stock Code"], trade["Platform"]):
                    skipped += 1
                    continue
                grouped_trades[key].append(trade)

            positions_by_key = defaultdict(list)
            for pos_row in _query_positions(current_market):
                positions_by_key[_position_key(current_market, pos_row)].append(pos_row)

            for key, trades in grouped_trades.items():
                _, group_platform, group_code = key
                name = next((trade["Stock Name"] for trade in reversed(trades) if trade.get("Stock Name")), group_code)

                try:
                    new_count, new_unit_price, realized_pnl = _rebuild_lots_and_realized_pnl(trades, group_code)
                except Exception as e:
                    errors.append(f"{group_code} {group_platform or ''}: {e}")
                    continue

                update_props = {
                    REALIZED_PNL_PROPERTY: {"number": round(realized_pnl, 4)},
                    "Count": {"number": new_count},
                    "Unit Price": {"number": round(new_unit_price, 4)},
                }

                position_rows = positions_by_key.get(key, [])
                if position_rows:
                    for row in position_rows:
                        notion.pages.update(page_id=row["id"], properties=update_props)
                        updated += 1
                else:
                    _create_position_for_realized_pnl(
                        current_market,
                        name,
                        group_code,
                        group_platform,
                        new_count,
                        new_unit_price,
                        realized_pnl,
                    )
                    created += 1

        status = "success" if not errors else "warning"
        msg = f"已更新 {updated} 行，已新建 {created} 行，已跳过 {skipped} 笔特殊流水"
        if errors:
            msg += "；异常: " + " | ".join(errors[:5])
        return {"status": status, "msg": msg, "updated": updated, "created": created, "skipped": skipped, "errors": errors}
    except Exception as e:
        logging.error(f"[Realized P&L] 同步失败: {str(e)}")
        return {"status": "error", "msg": str(e)}


# ==========================================
# 肌肉功能 3：每日盈亏快照
# ==========================================
def _daily_pnl_ds_id(market: str, platform: str | None = None) -> str | None:
    if market == "HK":
        return DB_DAILY_PNL_HK
    if market == "US":
        return DB_DAILY_PNL_US
    raise ValueError(f"未知市场: {market!r}")


def _daily_pnl_platform_label(market: str, platform: str | None = None) -> str:
    if market == "HK":
        return _normalize_platform(market, platform)
    if market == "US":
        return "IBKR"
    raise ValueError(f"未知市场: {market!r}")


def _today_for_market(market: str) -> str:
    tz_name = "Asia/Hong_Kong" if market == "HK" else "America/New_York"
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo(tz_name)).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def _snapshot_time_for_market(market: str) -> str:
    tz_name = "Asia/Hong_Kong" if market == "HK" else "America/New_York"
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo(tz_name)).isoformat()
    except Exception:
        return datetime.datetime.now().isoformat()


def _safe_pct(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _daily_pnl_platforms(market: str, platform: str | None = None) -> list[str | None]:
    if market == "HK":
        return [_normalize_platform(market, platform)] if platform else ["Trade25", "Futu"]
    return [None]


def _calculate_realized_pnl_for_date(market: str, snapshot_date: str, platform: str | None = None) -> tuple[float, int, list[str]]:
    """
    Calculate realized P&L generated by trades whose Date equals snapshot_date.

    The lot engine matches the existing conservative model: sells consume the
    lowest-cost lots first, and buy fees are included in unit cost.
    """
    trans_ds_id = DB_TRANS_HK if market == "HK" else DB_TRANS_US
    platform_filter = _normalize_platform(market, platform) if market == "HK" else ""
    grouped_trades = defaultdict(list)
    errors = []

    for row in _query_all_rows(trans_ds_id):
        trade = _trade_from_row(market, row)
        if not trade:
            continue
        if _is_excluded_daily_pnl_trade(market, trade):
            continue
        if _date_key(trade["Date"]) > snapshot_date:
            continue
        if market == "HK" and trade["Platform"] != platform_filter:
            continue
        key = _group_key(market, trade["Stock Code"], trade["Platform"])
        if _is_excluded_daily_pnl_key(market, trade["Stock Code"], trade["Platform"]):
            continue
        if _is_manual_daily_pnl_cost_key(market, trade["Stock Code"], trade["Platform"]):
            continue
        grouped_trades[key].append(trade)

    realized_pnl = 0.0
    realized_trade_count = 0

    for key, trades in grouped_trades.items():
        _, group_platform, group_code = key
        lots = []
        trades.sort(key=lambda x: (x["Date"], x["Created time"], x["Notion Page ID"]))

        try:
            for trade in trades:
                trade_date = _date_key(trade["Date"])
                if trade["Action"] == "Buy":
                    if trade["Count"] == 0:
                        if trade_date == snapshot_date:
                            realized_pnl -= trade["Trade Fee"]
                            realized_trade_count += 1
                        continue

                    unit_cost = ((trade["Count"] * trade["Price"]) + trade["Trade Fee"]) / trade["Count"]
                    lots.append({"Count": trade["Count"], "Unit Price": unit_cost})
                    continue

                sell_count = trade["Count"]
                held_count = sum(lot["Count"] for lot in lots)
                if sell_count > held_count:
                    raise ValueError(
                        f"{group_code} 在 {trade['Date']} 卖出 {sell_count} 股，但此前可用持仓只有 {held_count} 股"
                    )

                cost_basis = 0.0
                lots.sort(key=lambda x: x["Unit Price"])
                while sell_count > 0:
                    lot = lots[0]
                    used = min(sell_count, lot["Count"])
                    cost_basis += used * lot["Unit Price"]
                    lot["Count"] -= used
                    sell_count -= used

                    if lot["Count"] == 0:
                        lots.pop(0)

                if trade_date == snapshot_date:
                    realized_pnl += (trade["Count"] * trade["Price"]) - trade["Trade Fee"] - cost_basis
                    realized_trade_count += 1
        except Exception as e:
            errors.append(f"{group_code} {group_platform or ''}: {e}")

    return realized_pnl, realized_trade_count, errors


def _calculate_cumulative_realized_pnl_until(market: str, snapshot_date: str, platform: str | None = None) -> tuple[float, float, list[str]]:
    trans_ds_id = DB_TRANS_HK if market == "HK" else DB_TRANS_US
    platform_filter = _normalize_platform(market, platform) if market == "HK" else ""
    grouped_trades = defaultdict(list)
    errors = []

    for row in _query_all_rows(trans_ds_id):
        trade = _trade_from_row(market, row)
        if not trade:
            continue
        if _is_excluded_daily_pnl_trade(market, trade):
            continue
        if _date_key(trade["Date"]) > snapshot_date:
            continue
        if market == "HK" and trade["Platform"] != platform_filter:
            continue
        if _is_excluded_daily_pnl_key(market, trade["Stock Code"], trade["Platform"]):
            continue
        if _is_manual_daily_pnl_cost_key(market, trade["Stock Code"], trade["Platform"]):
            continue
        key = _group_key(market, trade["Stock Code"], trade["Platform"])
        grouped_trades[key].append(trade)

    cumulative_realized_pnl = 0.0
    cumulative_cost_basis = 0.0

    for key, trades in grouped_trades.items():
        _, group_platform, group_code = key
        lots = []
        trades.sort(key=lambda x: (x["Date"], x["Created time"], x["Notion Page ID"]))

        try:
            for trade in trades:
                if trade["Action"] == "Buy":
                    if trade["Count"] == 0:
                        cumulative_realized_pnl -= trade["Trade Fee"]
                        continue

                    unit_cost = ((trade["Count"] * trade["Price"]) + trade["Trade Fee"]) / trade["Count"]
                    cumulative_cost_basis += trade["Count"] * unit_cost
                    lots.append({"Count": trade["Count"], "Unit Price": unit_cost})
                    continue

                sell_count = trade["Count"]
                held_count = sum(lot["Count"] for lot in lots)
                if sell_count > held_count:
                    raise ValueError(
                        f"{group_code} 在 {trade['Date']} 卖出 {sell_count} 股，但此前可用持仓只有 {held_count} 股"
                    )

                cost_basis = 0.0
                lots.sort(key=lambda x: x["Unit Price"])
                while sell_count > 0:
                    lot = lots[0]
                    used = min(sell_count, lot["Count"])
                    cost_basis += used * lot["Unit Price"]
                    lot["Count"] -= used
                    sell_count -= used

                    if lot["Count"] == 0:
                        lots.pop(0)

                cumulative_realized_pnl += (trade["Count"] * trade["Price"]) - trade["Trade Fee"] - cost_basis
        except Exception as e:
            errors.append(f"{group_code} {group_platform or ''}: {e}")

    return cumulative_realized_pnl, cumulative_cost_basis, errors


def _calculate_unrealized_pnl_snapshot(market: str, platform: str | None = None) -> dict:
    """
    Calculate current-day unrealized P&L from position rows.

    Formula:
      unrealized = (Current Price - T-1 Closing Price) * Count
      denominator = T-1 Closing Price * Count
    """
    unrealized_pnl = 0.0
    gross_exposure = 0.0
    market_value = 0.0
    open_cost_basis = 0.0
    position_count = 0
    skipped = []
    manual_open_cost_basis = 0.0
    platform_filter = _normalize_platform(market, platform) if market == "HK" else ""

    for row in _query_positions(market):
        props = row.get("properties", {})
        code = _prop_text(props, "Stock Code") or row.get("id", "")
        row_platform = _prop_select(props, "Platform") if market == "HK" else ""
        if market == "HK" and row_platform != platform_filter:
            continue
        if _is_excluded_daily_pnl_key(market, code, row_platform):
            continue
        count = _prop_number(props, "Count")
        if count <= 0:
            continue

        current_price = _prop_number(props, "Current Price")
        t1_price = _prop_number(props, "T-1 Closing Price")
        unit_price = _prop_number(props, "Unit Price")
        if current_price <= 0 or t1_price <= 0:
            skipped.append(code)
            continue

        unrealized_pnl += (current_price - t1_price) * count
        gross_exposure += t1_price * count
        market_value += current_price * count
        position_open_cost = unit_price * count
        open_cost_basis += position_open_cost
        if _is_manual_daily_pnl_cost_key(market, code, row_platform):
            manual_open_cost_basis += position_open_cost
        position_count += 1

    return {
        "unrealized_pnl": unrealized_pnl,
        "gross_exposure": gross_exposure,
        "market_value": market_value,
        "open_cost_basis": open_cost_basis,
        "cumulative_unrealized_pnl": market_value - open_cost_basis,
        "position_count": position_count,
        "manual_open_cost_basis": manual_open_cost_basis,
        "skipped": skipped,
    }


def _query_daily_pnl_row(ds_id: str, snapshot_date: str, market: str, platform: str | None = None) -> dict | None:
    query_filter = {"property": DAILY_PNL_DATE_PROPERTY, "date": {"equals": snapshot_date}}
    if market == "HK" and DB_DAILY_PNL_HK and ds_id == DB_DAILY_PNL_HK:
        query_filter = {
            "and": [
                query_filter,
                {"property": DAILY_PNL_MARKET_PROPERTY, "select": {"equals": market}},
                {"property": DAILY_PNL_PLATFORM_PROPERTY, "select": {"equals": _daily_pnl_platform_label(market, platform)}},
            ]
        }

    resp = notion.data_sources.query(data_source_id=ds_id, filter=query_filter, page_size=1)
    results = resp.get("results", [])
    return results[0] if results else None


def _daily_pnl_properties(market: str, snapshot_date: str, snapshot: dict, platform: str | None = None) -> dict:
    platform_label = _daily_pnl_platform_label(market, platform)
    title = f"{snapshot_date} {market} {platform_label}"
    properties = {
        DAILY_PNL_TITLE_PROPERTY: {"title": [{"text": {"content": title}}]},
        DAILY_PNL_DATE_PROPERTY: {"date": {"start": snapshot_date}},
        DAILY_PNL_MARKET_PROPERTY: {"select": {"name": market}},
        "D Rlzd": {"number": round(snapshot["realized_pnl"], 4)},
        "Cum Rlzd": {"number": round(snapshot.get("cumulative_realized_pnl", 0), 4)},
        "Open Cost": {"number": round(snapshot.get("open_cost_basis", 0), 4)},
        "Cum Cost": {"number": round(snapshot.get("cumulative_cost_basis", 0), 4)},
        "T-1 MV": {"number": round(snapshot["gross_exposure"], 4)},
        "Mkt Value": {"number": round(snapshot["market_value"], 4)},
        "Pos Cnt": {"number": snapshot["position_count"]},
        "Trade Cnt": {"number": snapshot["realized_trade_count"]},
        "Snapshot": {"date": {"start": snapshot["snapshot_time"]}},
        "Notes": {"rich_text": [{"text": {"content": snapshot["notes"][:1900]}}]},
    }
    if market == "HK" and DB_DAILY_PNL_HK:
        properties[DAILY_PNL_PLATFORM_PROPERTY] = {"select": {"name": platform_label}}
    return properties


def upsert_daily_pnl_snapshot(market: str, snapshot_date: str, snapshot: dict, platform: str | None = None) -> str:
    """Create or update one daily P&L row. Returns created/updated."""
    ds_id = _daily_pnl_ds_id(market, platform)
    if not ds_id:
        suffix = f"_{platform.upper()}" if platform else ""
        raise ValueError(f"缺少 DB_DAILY_PNL_{market}{suffix}")

    properties = _daily_pnl_properties(market, snapshot_date, snapshot, platform)
    existing = _query_daily_pnl_row(ds_id, snapshot_date, market, platform)

    if existing:
        notion.pages.update(page_id=existing["id"], properties=properties)
        return "updated"

    notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": ds_id},
        properties=properties,
    )
    return "created"


def sync_daily_pnl_snapshot(market: str | None = None, snapshot_date: str | None = None, platform: str | None = None):
    """
    Upsert daily P&L snapshots for HK/US.

    Required Notion tables:
      DB_DAILY_PNL_HK and DB_DAILY_PNL_US. HK rows are separated by Platform.
    """
    try:
        markets = [market] if market else ["HK", "US"]
        updated = 0
        created = 0
        missing = []
        warnings = []

        for current_market in markets:
            current_date = snapshot_date or _today_for_market(current_market)
            for current_platform in _daily_pnl_platforms(current_market, platform):
                ds_id = _daily_pnl_ds_id(current_market, current_platform)
                if not ds_id:
                    missing.append(f"DB_DAILY_PNL_{current_market}")
                    continue

                realized_pnl, realized_trade_count, realized_errors = _calculate_realized_pnl_for_date(
                    current_market,
                    current_date,
                    current_platform,
                )
                cumulative_realized_pnl, cumulative_cost_basis, cumulative_errors = _calculate_cumulative_realized_pnl_until(
                    current_market,
                    current_date,
                    current_platform,
                )
                unrealized = _calculate_unrealized_pnl_snapshot(current_market, current_platform)
                cumulative_cost_basis += unrealized.get("manual_open_cost_basis", 0.0)

                gross_exposure = unrealized["gross_exposure"]
                unrealized_pnl = unrealized["unrealized_pnl"]
                total_pnl = realized_pnl + unrealized_pnl
                cumulative_unrealized_pnl = unrealized["cumulative_unrealized_pnl"]
                cumulative_total_pnl = cumulative_realized_pnl + cumulative_unrealized_pnl

                note_parts = []
                if unrealized["skipped"]:
                    note_parts.append("Skipped positions missing Current Price or T-1 Closing Price: " + ", ".join(unrealized["skipped"][:20]))
                if realized_errors:
                    note_parts.append("Realized P&L errors: " + " | ".join(realized_errors[:5]))
                if cumulative_errors:
                    note_parts.append("Cumulative P&L errors: " + " | ".join(cumulative_errors[:5]))
                notes = "；".join(note_parts)

                snapshot = {
                    "realized_pnl": realized_pnl,
                    "realized_pnl_pct": _safe_pct(realized_pnl, gross_exposure),
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_pct": _safe_pct(unrealized_pnl, gross_exposure),
                    "total_pnl": total_pnl,
                    "total_pnl_pct": _safe_pct(total_pnl, gross_exposure),
                    "cumulative_realized_pnl": cumulative_realized_pnl,
                    "cumulative_realized_pnl_pct": _safe_pct(cumulative_realized_pnl, cumulative_cost_basis),
                    "cumulative_unrealized_pnl": cumulative_unrealized_pnl,
                    "cumulative_unrealized_pnl_pct": _safe_pct(cumulative_unrealized_pnl, cumulative_cost_basis),
                    "cumulative_total_pnl": cumulative_total_pnl,
                    "cumulative_total_pnl_pct": _safe_pct(cumulative_total_pnl, cumulative_cost_basis),
                    "open_cost_basis": unrealized["open_cost_basis"],
                    "cumulative_cost_basis": cumulative_cost_basis,
                    "gross_exposure": gross_exposure,
                    "market_value": unrealized["market_value"],
                    "position_count": unrealized["position_count"],
                    "realized_trade_count": realized_trade_count,
                    "snapshot_time": _snapshot_time_for_market(current_market),
                    "notes": notes,
                }
                result = upsert_daily_pnl_snapshot(current_market, current_date, snapshot, current_platform)
                if result == "updated":
                    updated += 1
                else:
                    created += 1

                if notes:
                    platform_label = f" {current_platform}" if current_platform else ""
                    warnings.append(f"{current_market}{platform_label}: {notes}")

        if missing:
            return {
                "status": "warning",
                "msg": "缺少每日盈亏表环境变量: " + ", ".join(missing),
                "updated": updated,
                "created": created,
                "warnings": warnings,
            }

        status = "success" if not warnings else "warning"
        msg = f"已更新 {updated} 行，已新建 {created} 行"
        if warnings:
            msg += "；提示: " + " | ".join(warnings[:3])
        return {"status": status, "msg": msg, "updated": updated, "created": created, "warnings": warnings}
    except Exception as e:
        logging.error(f"[Daily P&L] 同步失败: {str(e)}")
        return {"status": "error", "msg": str(e)}


# ==========================================
# 翻页增强版：真正意义上的“全量” CSV 导出
# ==========================================
def export_data_to_file(market: str, table_type: str):
    # 导出目录：脚本同级的 exported_data/ 文件夹（VPS 上相对路径，避免硬编码）
    SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exported_data")
    ds_id = DB_POS_HK if table_type == "Position" else DB_TRANS_HK
    if market == "US":
        ds_id = DB_POS_US if table_type == "Position" else DB_TRANS_US
    
    try:
        if not os.path.exists(SAVE_DIR):
            os.makedirs(SAVE_DIR)

        # 1. 🚀 核心分页抓取
        all_notion_results = []
        has_more = True
        start_cursor = None

        while has_more:
            response = notion.data_sources.query(
                data_source_id=ds_id,
                start_cursor=start_cursor
            )
            all_notion_results.extend(response.get('results', []))
            has_more = response.get('has_more', False)
            start_cursor = response.get('next_cursor')
            if len(all_notion_results) > 5000: break # 安全阈值

        if not all_notion_results:
            return {"status": "error", "msg": "表格中没有数据"}
        
        # 2. 📝 数据解析
        all_rows = []
        headers = set()
        BLACK_LIST = ["notion_page_id", "Notion_Page_ID"]

        for row in all_notion_results:
            clean_row = {}
            props = row['properties']
            
            for field_name, content in props.items():
                if field_name in BLACK_LIST: continue
                
                headers.add(field_name)
                p_type = content.get('type')
                val = ""

                # --- 核心取值逻辑 (严禁省略) ---
                if p_type == 'title':
                    val = content['title'][0]['plain_text'] if content['title'] else ""
                elif p_type == 'rich_text':
                    val = content['rich_text'][0]['plain_text'] if content['rich_text'] else ""
                elif p_type == 'number':
                    val = content.get('number', 0)
                elif p_type == 'select':
                    val = content['select']['name'] if content['select'] else ""
                elif p_type == 'date':
                    val = content['date']['start'] if content.get('date') else ""
                elif p_type == 'formula':
                    f_data = content.get('formula', {})
                    f_type = f_data.get('type')
                    if f_type == 'number':
                        val = f_data.get('number', 0)
                    elif f_type == 'string':
                        val = f_data.get('string', "")
                    elif f_type == 'date':
                        val = f_data.get('date', {}).get('start', "")
                
                clean_row[field_name] = val
            
            all_rows.append(clean_row)

        # 3. 💾 写入 CSV
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{market}_{table_type}_{timestamp}.csv"
        full_path = os.path.join(SAVE_DIR, filename)
        
        with open(full_path, "w", encoding="utf-8-sig", newline='') as f:
            # 自动提取所有发现的表头并排序
            sorted_headers = sorted(list(headers))
            writer = csv.DictWriter(f, fieldnames=sorted_headers)
            writer.writeheader()
            writer.writerows(all_rows)
            
        return {
            "status": "success", 
            "msg": full_path,
            "count": len(all_rows)
        }
    except Exception as e:
        return {"status": "error", "msg": str(e)}
