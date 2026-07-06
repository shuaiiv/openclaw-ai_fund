"""
AI audit log viewer for premarket_planner.py and intraday_sentry.py.

Run:
    python for_openclaw/log_view/log_viewer.py --host 127.0.0.1 --port 8766
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


VIEWER_DIR = Path(__file__).resolve().parent
ROOT_DIR = VIEWER_DIR.parent
LOG_DIR = VIEWER_DIR / "ai_logs"

EVENT_LABELS = {
    "premarket_plan": "盘前谋划",
    "grid_trigger": "盘中裁决",
    "order_rebuild": "订单重构",
}

STRATEGY_LABELS = {
    "premarket_planner": "Premarket Planner",
    "intraday_sentry": "Intraday Sentry",
}


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenClaw AI 日志</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f5f2;
      --panel: #ffffff;
      --panel-soft: #fafaf7;
      --text: #1d1d1b;
      --muted: #6b6f68;
      --line: #d9ddd2;
      --accent: #1f7a5b;
      --accent-dark: #14543f;
      --warn: #b25c00;
      --danger: #b42318;
      --code-bg: #151713;
      --code-text: #e8ede1;
      --shadow: 0 8px 24px rgba(31, 39, 27, 0.08);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 20px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 10;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      font-weight: 750;
      letter-spacing: 0;
    }

    .status {
      color: var(--muted);
      white-space: nowrap;
      font-size: 13px;
    }

    .filters {
      display: grid;
      grid-template-columns: 160px 130px minmax(160px, 1fr) 150px 112px;
      gap: 10px;
      padding: 14px 20px;
      background: var(--panel-soft);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 57px;
      z-index: 9;
    }

    label {
      display: flex;
      flex-direction: column;
      gap: 4px;
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }

    input, select, button {
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
      letter-spacing: 0;
    }

    input, select { padding: 0 10px; min-width: 0; }

    button {
      align-self: end;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 0 12px;
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      font-weight: 700;
    }

    button:hover { background: var(--accent-dark); }

    main {
      display: grid;
      grid-template-columns: minmax(280px, 420px) minmax(0, 1fr);
      gap: 14px;
      padding: 14px 20px 20px;
      min-height: calc(100vh - 120px);
    }

    .list, .detail {
      min-width: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .list-head, .detail-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 48px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-soft);
    }

    .count { color: var(--muted); font-size: 13px; }

    .items {
      height: calc(100vh - 190px);
      overflow: auto;
    }

    .item {
      width: 100%;
      height: auto;
      display: block;
      padding: 12px;
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      background: var(--panel);
      color: var(--text);
      text-align: left;
      cursor: pointer;
    }

    .item:hover, .item.active { background: #eef5ef; }
    .item.error { border-left: 4px solid var(--danger); }

    .item-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-weight: 750;
      line-height: 1.25;
    }

    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel-soft);
      color: var(--muted);
      white-space: nowrap;
    }

    .pill.hk { color: #8a4b00; border-color: #dfc89c; background: #fff7e8; }
    .pill.us { color: #0c5f49; border-color: #a9d7c2; background: #ecf8f2; }
    .pill.error { color: var(--danger); border-color: #f0b8b3; background: #fff0ee; }

    .tabs {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }

    .tab {
      align-self: auto;
      height: 30px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--muted);
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
    }

    .tab.active {
      background: var(--text);
      border-color: var(--text);
      color: #fff;
    }

    .detail-body {
      height: calc(100vh - 190px);
      overflow: auto;
      padding: 14px;
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }

    .metric {
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      min-width: 0;
    }

    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 3px;
    }

    .metric strong {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    pre {
      margin: 0;
      padding: 14px;
      border-radius: 8px;
      background: var(--code-bg);
      color: var(--code-text);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 12.5px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }

    .empty {
      padding: 32px 16px;
      color: var(--muted);
      text-align: center;
    }

    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .filters {
        position: static;
        grid-template-columns: 1fr 1fr;
      }
      main { grid-template-columns: 1fr; padding: 12px; }
      .items, .detail-body { height: auto; max-height: none; }
      .summary { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>OpenClaw AI 日志</h1>
    <div class="status" id="status">读取中...</div>
  </header>

  <form class="filters" id="filters">
    <label>日期
      <input type="date" name="date" id="date">
    </label>
    <label>市场
      <select name="market" id="market">
        <option value="">全部</option>
        <option value="HK">港股</option>
        <option value="US">美股</option>
      </select>
    </label>
    <label>标的 Code
      <input type="search" name="symbol" id="symbol" placeholder="例如 0700.HK / AAPL.US">
    </label>
    <label>类型
      <select name="event_type" id="event_type">
        <option value="">全部</option>
        <option value="premarket_plan">盘前谋划</option>
        <option value="grid_trigger">盘中裁决</option>
        <option value="order_rebuild">订单重构</option>
      </select>
    </label>
    <button type="submit" aria-label="筛选日志">筛选</button>
  </form>

  <main>
    <section class="list">
      <div class="list-head">
        <strong>记录</strong>
        <span class="count" id="count">0 条</span>
      </div>
      <div class="items" id="items"></div>
    </section>

    <section class="detail">
      <div class="detail-head">
        <strong id="detail-title">详情</strong>
        <div class="tabs" id="tabs"></div>
      </div>
      <div class="detail-body" id="detail"></div>
    </section>
  </main>

  <script>
    const state = {
      records: [],
      selectedId: null,
      tab: "tg_message",
    };

    const eventLabels = {
      premarket_plan: "盘前谋划",
      grid_trigger: "盘中裁决",
      order_rebuild: "订单重构",
    };

    const tabLabels = {
      tg_message: "TG 内容",
      ai_input: "投喂数据",
      ai_output: "原始回复",
      trigger: "触发信息",
      metadata: "模型元数据",
    };

    const $ = (id) => document.getElementById(id);

    function today() {
      const d = new Date();
      d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
      return d.toISOString().slice(0, 10);
    }

    function setInitialFilters() {
      const params = new URLSearchParams(location.search);
      $("date").value = params.get("date") || today();
      $("market").value = params.get("market") || "";
      $("symbol").value = params.get("symbol") || "";
      $("event_type").value = params.get("event_type") || "";
    }

    function buildParams() {
      const params = new URLSearchParams();
      for (const key of ["date", "market", "symbol", "event_type"]) {
        const value = $(key).value.trim();
        if (value) params.set(key, value);
      }
      return params;
    }

    async function loadRecords() {
      $("status").textContent = "读取中...";
      const params = buildParams();
      history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
      const res = await fetch(`/api/logs?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      state.records = data.records;
      if (!state.records.some((r) => r.id === state.selectedId)) {
        state.selectedId = state.records[0]?.id || null;
      }
      $("status").textContent = data.log_dir;
      renderList();
      renderDetail();
    }

    function renderList() {
      $("count").textContent = `${state.records.length} 条`;
      const root = $("items");
      root.textContent = "";
      if (!state.records.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "没有匹配的 AI 日志";
        root.appendChild(empty);
        return;
      }

      for (const rec of state.records) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = `item ${rec.id === state.selectedId ? "active" : ""} ${rec.error ? "error" : ""}`;
        item.onclick = () => {
          state.selectedId = rec.id;
          renderList();
          renderDetail();
        };

        const title = document.createElement("div");
        title.className = "item-title";
        const titleText = document.createElement("span");
        titleText.textContent = rec.title || `${rec.symbol} ${eventLabels[rec.event_type] || rec.event_type}`;
        const time = document.createElement("span");
        time.className = "pill";
        time.textContent = (rec.created_at || "").replace("T", " ").slice(11, 19);
        title.append(titleText, time);

        const meta = document.createElement("div");
        meta.className = "meta";
        meta.append(pill(rec.market || "-", (rec.market || "").toLowerCase()));
        meta.append(pill(rec.symbol || "-"));
        meta.append(pill(eventLabels[rec.event_type] || rec.event_type || "-"));
        if (rec.error) meta.append(pill("失败", "error"));

        item.append(title, meta);
        root.appendChild(item);
      }
    }

    function pill(text, extra = "") {
      const el = document.createElement("span");
      el.className = `pill ${extra}`;
      el.textContent = text;
      return el;
    }

    function renderDetail() {
      const rec = state.records.find((r) => r.id === state.selectedId);
      const tabs = $("tabs");
      const detail = $("detail");
      tabs.textContent = "";
      detail.textContent = "";

      if (!rec) {
        $("detail-title").textContent = "详情";
        detail.innerHTML = '<div class="empty">选择一条记录查看详情</div>';
        return;
      }

      $("detail-title").textContent = rec.title || rec.symbol || "详情";
      for (const key of Object.keys(tabLabels)) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `tab ${state.tab === key ? "active" : ""}`;
        btn.textContent = tabLabels[key];
        btn.onclick = () => {
          state.tab = key;
          renderDetail();
        };
        tabs.appendChild(btn);
      }

      const summary = document.createElement("div");
      summary.className = "summary";
      summary.append(metric("时间", (rec.created_at || "").replace("T", " ")));
      summary.append(metric("市场", rec.market || "-"));
      summary.append(metric("标的", rec.symbol || "-"));
      summary.append(metric("类型", eventLabels[rec.event_type] || rec.event_type || "-"));
      detail.appendChild(summary);

      if (rec.error) {
        const error = document.createElement("pre");
        error.textContent = `ERROR: ${rec.error}`;
        detail.appendChild(error);
        if (state.tab === "tg_message") return;
      }

      const pre = document.createElement("pre");
      let value = rec[state.tab];
      if (state.tab === "trigger" || state.tab === "metadata") {
        value = JSON.stringify(value || {}, null, 2);
      }
      pre.textContent = value || "无内容";
      detail.appendChild(pre);
    }

    function metric(label, value) {
      const el = document.createElement("div");
      el.className = "metric";
      const span = document.createElement("span");
      span.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value || "-";
      el.append(span, strong);
      return el;
    }

    $("filters").addEventListener("submit", (event) => {
      event.preventDefault();
      loadRecords().catch((err) => {
        $("status").textContent = `读取失败: ${err.message}`;
      });
    });

    setInitialFilters();
    loadRecords().catch((err) => {
      $("status").textContent = `读取失败: ${err.message}`;
    });
  </script>
</body>
</html>
"""


def _json_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler) -> None:
    body = HTML.encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _available_files(log_date: str) -> list[Path]:
    if log_date:
        path = LOG_DIR / f"ai_audit_{log_date}.jsonl"
        return [path] if path.exists() else []
    return sorted(LOG_DIR.glob("ai_audit_*.jsonl"), reverse=True)


def _load_records(query: dict[str, list[str]]) -> list[dict]:
    log_date = _first(query, "date")
    market = _first(query, "market").upper()
    symbol = _first(query, "symbol").upper()
    event_type = _first(query, "event_type")

    records: list[dict] = []
    for path in _available_files(log_date):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec_market = str(rec.get("market", "")).upper()
                rec_symbol = str(rec.get("symbol", "")).upper()
                if market and rec_market != market:
                    continue
                if symbol and symbol not in rec_symbol:
                    continue
                if event_type and rec.get("event_type") != event_type:
                    continue

                rec["event_label"] = EVENT_LABELS.get(rec.get("event_type"), rec.get("event_type", ""))
                rec["strategy_label"] = STRATEGY_LABELS.get(rec.get("strategy"), rec.get("strategy", ""))
                records.append(rec)

    records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return records


def _first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or [""]
    return values[0].strip()


class LogViewerHandler(BaseHTTPRequestHandler):
    server_version = "OpenClawLogViewer/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            _html_response(self)
            return
        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            records = _load_records(query)
            _json_response(
                self,
                HTTPStatus.OK,
                {
                    "records": records,
                    "log_dir": str(LOG_DIR),
                    "today": date.today().isoformat(),
                },
            )
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[log-viewer] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw AI audit log viewer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8766, type=int)
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), LogViewerHandler)
    print(f"OpenClaw AI 日志页面: http://{args.host}:{args.port}")
    print(f"日志目录: {LOG_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭日志页面...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
