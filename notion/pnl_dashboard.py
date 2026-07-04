"""
Notion 每日盈亏看板
====================

单文件 HTTP 服务，读取 Notion 每日盈亏表并用 ECharts 渲染双 Y 轴折线图。

运行：
  python notion/pnl_dashboard.py

环境变量：
  NOTION_TOKEN
  DB_DAILY_PNL_HK
  DB_DAILY_PNL_US
  PNL_DASHBOARD_HOST=127.0.0.1
  PNL_DASHBOARD_PORT=8765
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv, find_dotenv
from notion_client import Client

# Import shared helpers from notion_database_manager to avoid duplicate definitions
from notion_database_manager import (
    _daily_pnl_ds_id,
    _daily_pnl_platform_label,
    _normalize_platform,
    _safe_pct,
)

load_dotenv(find_dotenv())

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DB_DAILY_PNL_HK = os.getenv("DB_DAILY_PNL_HK")
DB_DAILY_PNL_US = os.getenv("DB_DAILY_PNL_US")
HOST = os.getenv("PNL_DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("PNL_DASHBOARD_PORT", "8765"))

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>每日盈亏</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --text: #202124;
      --muted: #70757a;
      --line: #deded8;
      --accent: #176f6b;
      --danger: #c94747;
      --blue: #3f6fb5;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .shell {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 18px 10px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.86);
      backdrop-filter: blur(10px);
      position: sticky;
      top: 0;
      z-index: 2;
    }

    h1 {
      font-size: 18px;
      line-height: 1.2;
      margin: 0;
      font-weight: 680;
    }

    .toolbar {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }

    .segmented {
      display: inline-flex;
      align-items: center;
      padding: 2px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f0f0ec;
    }

    button {
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      font-size: 13px;
      line-height: 28px;
      min-width: 42px;
      height: 28px;
      padding: 0 10px;
      cursor: pointer;
    }

    button.active {
      background: var(--panel);
      color: var(--text);
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
    }

    .custom-range {
      display: none;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }

    .custom-range.active {
      display: inline-flex;
    }

    input[type="date"] {
      height: 32px;
      min-width: 136px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
      padding: 0 8px;
      font: inherit;
      font-size: 13px;
    }

    main {
      padding: 14px 18px 18px;
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 12px;
      min-height: 0;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 8px;
    }

    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      min-height: 72px;
    }

    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }

    .metric strong {
      display: block;
      font-size: 19px;
      line-height: 1.2;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .metric small {
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 11px;
      font-variant-numeric: tabular-nums;
    }

    .chart-wrap {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-height: 420px;
      height: calc((100vh - 214px) * 0.6);
      padding: 8px;
    }

    .bar-chart-wrap {
      min-height: 300px;
      height: calc((100vh - 214px) * 0.4);
    }

    #chart,
    #dailyChart {
      width: 100%;
      height: 100%;
    }

    .error {
      display: none;
      padding: 12px;
      color: #8d2f2f;
      background: #fff2f0;
      border: 1px solid #ffd8d2;
      border-radius: 8px;
      font-size: 13px;
    }

    .pos { color: var(--accent); }
    .neg { color: var(--danger); }

    @media (max-width: 900px) {
      header {
        align-items: flex-start;
        flex-direction: column;
      }

      .toolbar {
        justify-content: flex-start;
      }

      .metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .chart-wrap {
        height: 460px;
      }

      .bar-chart-wrap {
        height: 340px;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <h1>每日盈亏</h1>
      <div class="toolbar">
        <div class="segmented" aria-label="市场">
          <button id="marketUS" class="active" data-market="US">美股</button>
          <button id="marketHK" data-market="HK">港股</button>
        </div>
        <div class="segmented" id="platformSwitch" aria-label="港股平台">
          <button class="platform" data-platform="All">All</button>
          <button class="platform active" data-platform="Trade25">Trade25</button>
          <button class="platform" data-platform="Futu">Futu</button>
        </div>
        <div class="segmented" aria-label="区间">
          <button class="range active" data-range="mtd">MTD</button>
          <button class="range" data-range="30d">30D</button>
          <button class="range" data-range="90d">90D</button>
          <button class="range" data-range="180d">180D</button>
          <button class="range" data-range="ytd">YTD</button>
          <button class="range" data-range="1y">1Y</button>
          <button class="range" data-range="all">All</button>
          <button class="range" data-range="custom">Custom</button>
        </div>
        <div id="customRange" class="custom-range" aria-label="自定义日期范围">
          <input id="startDate" type="date" />
          <span>至</span>
          <input id="endDate" type="date" />
        </div>
      </div>
    </header>
    <main>
      <section class="metrics">
        <div class="metric"><span>累计总收益</span><strong id="totalPnl">-</strong><small id="totalPct">-</small></div>
        <div class="metric"><span>累计已实现</span><strong id="realizedPnl">-</strong><small id="realizedPct">-</small></div>
        <div class="metric"><span>持仓浮盈</span><strong id="unrealizedPnl">-</strong><small id="unrealizedPct">-</small></div>
        <div class="metric"><span>持仓市值</span><strong id="marketValue">-</strong><small id="grossExposure">-</small></div>
        <div class="metric"><span>最新日期</span><strong id="latestDate">-</strong><small id="rowCount">-</small></div>
      </section>
      <div id="error" class="error"></div>
      <section class="chart-wrap">
        <div id="chart"></div>
      </section>
      <section class="chart-wrap bar-chart-wrap">
        <div id="dailyChart"></div>
      </section>
    </main>
  </div>

  <script>
    const chart = echarts.init(document.getElementById("chart"), null, { renderer: "canvas" });
    const dailyChart = echarts.init(document.getElementById("dailyChart"), null, { renderer: "canvas" });
    let market = "US";
    let platform = "Trade25";
    let range = "mtd";

    const currency = () => market === "US" ? "USD" : "HKD";
    const pad2 = (value) => String(value).padStart(2, "0");
    const dateKey = (date) => `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;

    const shiftDays = (date, count) => {
      const copy = new Date(date);
      copy.setDate(copy.getDate() + count);
      return copy;
    };

    const shiftYears = (date, count) => {
      const copy = new Date(date);
      copy.setFullYear(copy.getFullYear() + count);
      return copy;
    };

    const defaultEndDate = () => document.getElementById("endDate").value || dateKey(new Date());

    const rangeParams = () => {
      if (range === "all") return {};
      if (range === "custom") {
        const start = document.getElementById("startDate").value;
        const end = document.getElementById("endDate").value;
        return { ...(start ? { start } : {}), ...(end ? { end } : {}) };
      }

      const end = new Date(`${defaultEndDate()}T00:00:00`);
      let start;
      if (range === "mtd") start = new Date(end.getFullYear(), end.getMonth(), 1);
      if (range === "ytd") start = new Date(end.getFullYear(), 0, 1);
      if (range === "30d") start = shiftDays(end, -29);
      if (range === "90d") start = shiftDays(end, -89);
      if (range === "180d") start = shiftDays(end, -179);
      if (range === "1y") start = shiftYears(end, -1);

      return { start: dateKey(start), end: dateKey(end) };
    };

    const fmtMoney = (value) => {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      return `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currency()}`;
    };

    const fmtAxisMoney = (value) => {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      const number = Number(value);
      const abs = Math.abs(number);
      if (abs >= 100000000) return `${(number / 100000000).toFixed(1)}亿`;
      if (abs >= 10000) return `${(number / 10000).toFixed(1)}万`;
      return number.toLocaleString(undefined, { maximumFractionDigits: 0 });
    };

    const fmtPct = (value) => {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      return `${(Number(value) * 100).toFixed(2)}%`;
    };

    const setTone = (el, value) => {
      el.classList.remove("pos", "neg");
      if (Number(value) > 0) el.classList.add("pos");
      if (Number(value) < 0) el.classList.add("neg");
    };

    const setActive = (selector, attr, value) => {
      document.querySelectorAll(selector).forEach((btn) => {
        btn.classList.toggle("active", btn.dataset[attr] === String(value));
      });
    };

    const latest = (rows) => rows.length ? rows[rows.length - 1] : null;

    async function loadData() {
      const params = new URLSearchParams({ market, platform, ...rangeParams() });
      const res = await fetch(`/api/pnl?${params.toString()}`);
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `HTTP ${res.status}`);
      }
      return res.json();
    }

    function updateMetrics(rows) {
      const last = latest(rows);
      const fields = {
        totalPnl: ["cumulative_total_pnl", fmtMoney],
        totalPct: ["cumulative_total_pnl_pct", fmtPct],
        realizedPnl: ["cumulative_realized_pnl", fmtMoney],
        realizedPct: ["cumulative_realized_pnl_pct", fmtPct],
        unrealizedPnl: ["cumulative_unrealized_pnl", fmtMoney],
        unrealizedPct: ["cumulative_unrealized_pnl_pct", fmtPct],
        marketValue: ["market_value", fmtMoney],
        grossExposure: ["open_cost_basis", (v) => `持仓成本 ${fmtMoney(v)}`],
      };

      Object.entries(fields).forEach(([id, [key, formatter]]) => {
        const el = document.getElementById(id);
        el.textContent = last ? formatter(last[key]) : "-";
        if (key.includes("pnl")) setTone(el, last ? last[key] : 0);
      });

      document.getElementById("latestDate").textContent = last ? last.date : "-";
      document.getElementById("rowCount").textContent = `${rows.length} 条记录`;
    }

    function renderChart(rows) {
      const dates = rows.map((row) => row.date);
      const realized = rows.map((row) => row.cumulative_realized_pnl);
      const unrealized = rows.map((row) => row.cumulative_unrealized_pnl);
      const total = rows.map((row) => row.cumulative_total_pnl);
      const marketValue = rows.map((row) => row.market_value);
      const totalPct = rows.map((row) => row.cumulative_total_pnl_pct * 100);

      chart.setOption({
        color: ["#176f6b", "#3f6fb5", "#c94747", "#8a6f3d", "#d88b26"],
        tooltip: {
          trigger: "axis",
          formatter: (params) => {
            const lines = [params[0]?.axisValue || ""];
            params.forEach((item) => {
              const isPct = item.seriesName.includes("%");
              const value = isPct ? `${Number(item.value).toFixed(2)}%` : fmtMoney(item.value);
              lines.push(`${item.marker}${item.seriesName}: ${value}`);
            });
            return lines.join("<br/>");
          }
        },
        legend: {
          top: 8,
          data: ["累计总收益", "累计已实现", "持仓浮盈", "持仓市值", "累计收益%"]
        },
        grid: { left: 88, right: 72, top: 54, bottom: 72 },
        dataZoom: [
          { type: "inside", throttle: 40 },
          { type: "slider", height: 24, bottom: 22 }
        ],
        xAxis: {
          type: "category",
          boundaryGap: false,
          data: dates,
          axisLabel: { color: "#70757a" },
          axisLine: { lineStyle: { color: "#d6d6cf" } }
        },
        yAxis: [
          {
            type: "value",
            name: `金额 (${currency()})`,
            axisLabel: { formatter: (v) => fmtAxisMoney(v), color: "#70757a" },
            splitLine: { lineStyle: { color: "#eeeeea" } }
          },
          {
            type: "value",
            name: "%",
            axisLabel: { formatter: "{value}%", color: "#70757a" },
            splitLine: { show: false }
          }
        ],
        series: [
          { name: "累计总收益", type: "line", yAxisIndex: 0, data: total, showSymbol: false, smooth: 0.2, lineStyle: { width: 2.4 } },
          { name: "累计已实现", type: "line", yAxisIndex: 0, data: realized, showSymbol: false, smooth: 0.2, lineStyle: { width: 2 } },
          { name: "持仓浮盈", type: "line", yAxisIndex: 0, data: unrealized, showSymbol: false, smooth: 0.2, lineStyle: { width: 2 } },
          { name: "持仓市值", type: "line", yAxisIndex: 0, data: marketValue, showSymbol: false, smooth: 0.15, lineStyle: { width: 2, opacity: 0.82 } },
          { name: "累计收益%", type: "line", yAxisIndex: 1, data: totalPct, showSymbol: false, smooth: 0.2, lineStyle: { width: 1.8, type: "dashed" } }
        ]
      }, true);
    }

    function renderDailyChart(rows) {
      const dates = rows.map((row) => row.date);
      const realized = rows.map((row) => row.realized_pnl);
      const unrealized = rows.map((row) => row.unrealized_pnl);
      const total = rows.map((row) => row.total_pnl);

      dailyChart.setOption({
        color: ["#176f6b", "#3f6fb5", "#c94747"],
        tooltip: {
          trigger: "axis",
          formatter: (params) => {
            const lines = [params[0]?.axisValue || ""];
            params.forEach((item) => {
              lines.push(`${item.marker}${item.seriesName}: ${fmtMoney(item.value)}`);
            });
            return lines.join("<br/>");
          }
        },
        legend: {
          top: 8,
          data: ["每日总盈亏", "每日已实现", "每日未实现"]
        },
        grid: { left: 88, right: 24, top: 54, bottom: 64 },
        dataZoom: [
          { type: "inside", throttle: 40 },
          { type: "slider", height: 22, bottom: 18 }
        ],
        xAxis: {
          type: "category",
          data: dates,
          axisLabel: { color: "#70757a" },
          axisLine: { lineStyle: { color: "#d6d6cf" } }
        },
        yAxis: {
          type: "value",
          name: `每日盈亏 (${currency()})`,
          axisLabel: { formatter: (v) => fmtAxisMoney(v), color: "#70757a" },
          splitLine: { lineStyle: { color: "#eeeeea" } }
        },
        series: [
          { name: "每日总盈亏", type: "bar", data: total, barMaxWidth: 18, itemStyle: { opacity: 0.72 } },
          { name: "每日已实现", type: "bar", stack: "daily", data: realized, barMaxWidth: 18, itemStyle: { opacity: 0.78 } },
          { name: "每日未实现", type: "bar", stack: "daily", data: unrealized, barMaxWidth: 18, itemStyle: { opacity: 0.78 } }
        ]
      }, true);
    }

    async function refresh() {
      const error = document.getElementById("error");
      try {
        error.style.display = "none";
        const payload = await loadData();
        updateMetrics(payload.rows);
        renderChart(payload.rows);
        renderDailyChart(payload.rows);
      } catch (err) {
        error.textContent = err.message;
        error.style.display = "block";
      }
    }

    document.querySelectorAll("[data-market]").forEach((btn) => {
      btn.addEventListener("click", () => {
        market = btn.dataset.market;
        setActive("[data-market]", "market", market);
        document.getElementById("platformSwitch").style.display = market === "HK" ? "inline-flex" : "none";
        refresh();
      });
    });

    document.querySelectorAll(".platform").forEach((btn) => {
      btn.addEventListener("click", () => {
        platform = btn.dataset.platform;
        setActive(".platform", "platform", platform);
        refresh();
      });
    });

    document.querySelectorAll(".range").forEach((btn) => {
      btn.addEventListener("click", () => {
        range = btn.dataset.range;
        setActive(".range", "range", range);
        document.getElementById("customRange").classList.toggle("active", range === "custom");
        refresh();
      });
    });

    document.querySelectorAll("#startDate, #endDate").forEach((input) => {
      input.addEventListener("change", () => {
        range = "custom";
        setActive(".range", "range", range);
        document.getElementById("customRange").classList.add("active");
        refresh();
      });
    });

    window.addEventListener("resize", () => {
      chart.resize();
      dailyChart.resize();
    });
    document.getElementById("platformSwitch").style.display = "none";
    document.getElementById("endDate").value = dateKey(new Date());
    refresh();
    setInterval(refresh, 5 * 60 * 1000);
  </script>
</body>
</html>
"""


def _number(props: dict, name: str) -> float:
    data = props.get(name, {})
    if data.get("type") == "number":
        return float(data.get("number") or 0)
    if data.get("type") == "formula":
        formula = data.get("formula", {})
        if formula.get("type") == "number":
            return float(formula.get("number") or 0)
    return 0.0


def _number_any(props: dict, *names: str) -> float:
    for name in names:
        if name in props:
            return _number(props, name)
    return 0.0


def _date(props: dict, name: str) -> str:
    data = props.get(name, {})
    value = data.get("date") if data.get("type") == "date" else None
    return (value or {}).get("start", "")[:10]


def _select(props: dict, name: str) -> str:
    data = props.get(name, {})
    selected = data.get("select") if data.get("type") == "select" else None
    return selected.get("name", "") if selected else ""


# _daily_pnl_platform_label, _daily_pnl_ds_id, _safe_pct
# are imported from notion_database_manager (see top of file)


def _empty_pnl_row(date: str) -> dict:
    return {
        "date": date,
        "realized_pnl": 0.0,
        "realized_pnl_pct": 0.0,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "total_pnl": 0.0,
        "total_pnl_pct": 0.0,
        "cumulative_realized_pnl": 0.0,
        "cumulative_realized_pnl_pct": 0.0,
        "cumulative_unrealized_pnl": 0.0,
        "cumulative_unrealized_pnl_pct": 0.0,
        "cumulative_total_pnl": 0.0,
        "cumulative_total_pnl_pct": 0.0,
        "open_cost_basis": 0.0,
        "cumulative_cost_basis": 0.0,
        "gross_exposure": 0.0,
        "market_value": 0.0,
        "position_count": 0,
        "realized_trade_count": 0,
    }


def _combine_hk_platform_rows(rows_by_platform: list[list[dict]]) -> list[dict]:
    combined_by_date: dict[str, dict] = {}

    for platform_rows in rows_by_platform:
        for row in platform_rows:
            date = row["date"]
            target = combined_by_date.setdefault(date, _empty_pnl_row(date))
            for key in (
                "realized_pnl",
                "unrealized_pnl",
                "total_pnl",
                "cumulative_realized_pnl",
                "cumulative_unrealized_pnl",
                "cumulative_total_pnl",
                "open_cost_basis",
                "cumulative_cost_basis",
                "gross_exposure",
                "market_value",
                "position_count",
                "realized_trade_count",
            ):
                target[key] += row[key]

    rows = sorted(combined_by_date.values(), key=lambda item: item["date"])
    for row in rows:
        row["realized_pnl_pct"] = _safe_pct(row["realized_pnl"], row["gross_exposure"])
        row["unrealized_pnl_pct"] = _safe_pct(row["unrealized_pnl"], row["gross_exposure"])
        row["total_pnl_pct"] = _safe_pct(row["total_pnl"], row["gross_exposure"])
        row["cumulative_realized_pnl_pct"] = _safe_pct(row["cumulative_realized_pnl"], row["cumulative_cost_basis"])
        row["cumulative_unrealized_pnl_pct"] = _safe_pct(row["cumulative_unrealized_pnl"], row["cumulative_cost_basis"])
        row["cumulative_total_pnl_pct"] = _safe_pct(row["cumulative_total_pnl"], row["cumulative_cost_basis"])
    return rows


def _query_all_rows(ds_id: str) -> list[dict]:
    rows = []
    cursor = None
    while True:
        kwargs = {"data_source_id": ds_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(**kwargs)
        rows.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return rows


def _load_pnl_rows(
    market: str,
    days: int = 0,
    platform: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    if notion is None:
        raise RuntimeError("缺少 NOTION_TOKEN")

    if market == "HK" and (platform or "").strip().lower() == "all":
        rows = _combine_hk_platform_rows([
            _load_pnl_rows("HK", 0, "Trade25", start, end),
            _load_pnl_rows("HK", 0, "Futu", start, end),
        ])
        if days > 0:
            rows = rows[-days:]
        return rows

    try:
        ds_id = _daily_pnl_ds_id(market, platform)
    except ValueError as e:
        raise RuntimeError(str(e)) from e
    if not ds_id:
        suffix = f"_{platform.upper()}" if market == "HK" and platform else ""
        raise RuntimeError(f"缺少 DB_DAILY_PNL_{market}{suffix}")

    rows = []
    expected_platform = _daily_pnl_platform_label(market, platform)
    for row in _query_all_rows(ds_id):
        props = row.get("properties", {})
        if market == "HK" and DB_DAILY_PNL_HK and ds_id == DB_DAILY_PNL_HK:
            if _select(props, "Market") != market:
                continue
            if _select(props, "Platform") != expected_platform:
                continue
        row_date = _date(props, "Date")
        if not row_date:
            continue
        if start and row_date < start:
            continue
        if end and row_date > end:
            continue
        rows.append({
            "date": row_date,
            "realized_pnl": _number_any(props, "D Rlzd", "Realized P&L"),
            "realized_pnl_pct": _number_any(props, "D Rlzd %", "Realized P&L %"),
            "unrealized_pnl": _number_any(props, "D Unrlzd", "Unrealized P&L"),
            "unrealized_pnl_pct": _number_any(props, "D Unrlzd %", "Unrealized P&L %"),
            "total_pnl": _number_any(props, "D Total", "Total P&L"),
            "total_pnl_pct": _number_any(props, "D Total %", "Total P&L %"),
            "cumulative_realized_pnl": _number_any(props, "Cum Rlzd", "Cumulative Realized P&L"),
            "cumulative_realized_pnl_pct": _number_any(props, "Cum Rlzd %", "Cumulative Realized P&L %"),
            "cumulative_unrealized_pnl": _number_any(props, "Cum Unrlzd", "Cumulative Unrealized P&L"),
            "cumulative_unrealized_pnl_pct": _number_any(props, "Cum Unrlzd %", "Cumulative Unrealized P&L %"),
            "cumulative_total_pnl": _number_any(props, "Cum Total", "Cumulative Total P&L"),
            "cumulative_total_pnl_pct": _number_any(props, "Cum Total %", "Cumulative Total P&L %"),
            "open_cost_basis": _number_any(props, "Open Cost", "Open Cost Basis"),
            "cumulative_cost_basis": _number_any(props, "Cum Cost", "Cumulative Cost Basis"),
            "gross_exposure": _number_any(props, "T-1 MV", "Gross Exposure"),
            "market_value": _number_any(props, "Mkt Value", "Market Value"),
            "position_count": int(_number_any(props, "Pos Cnt", "Position Count")),
            "realized_trade_count": int(_number_any(props, "Trade Cnt", "Realized Trade Count")),
        })

    rows.sort(key=lambda item: item["date"])
    if days > 0:
        rows = rows[-days:]
    return rows


def _json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler: BaseHTTPRequestHandler, body: str, status: int = 200, content_type: str = "text/plain; charset=utf-8"):
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class PnlDashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{now}] {self.address_string()} {fmt % args}\n")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/pnl"}:
            _text_response(self, HTML, content_type="text/html; charset=utf-8")
            return

        if parsed.path == "/health":
            _json_response(self, {"status": "ok"})
            return

        if parsed.path == "/api/pnl":
            params = parse_qs(parsed.query)
            market = params.get("market", ["US"])[0].upper()
            platform = params.get("platform", ["Trade25"])[0]
            try:
                days = int(params.get("days", ["0"])[0])
            except ValueError:
                days = 0
            start = (params.get("start", [""])[0] or "").strip()
            end = (params.get("end", [""])[0] or "").strip()

            try:
                rows = _load_pnl_rows(market, days, platform, start or None, end or None)
                _json_response(self, {"market": market, "platform": platform, "start": start, "end": end, "rows": rows})
            except Exception as exc:
                _json_response(self, {"error": str(exc)}, status=500)
            return

        _json_response(self, {"error": "not found"}, status=404)


def main():
    if not NOTION_TOKEN:
        raise SystemExit("缺少 NOTION_TOKEN")

    server = ThreadingHTTPServer((HOST, PORT), PnlDashboardHandler)
    print(f"PNL dashboard listening on http://{HOST}:{PORT}/pnl")
    server.serve_forever()


if __name__ == "__main__":
    main()
