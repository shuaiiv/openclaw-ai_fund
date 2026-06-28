# OpenClaw 自动交易系统

此目录 (`for_openclaw`) 是基于大型语言模型（LLM）的量化交易系统（OpenClaw）的核心业务执行层与集成层。它负责了从行情抓取、策略编排、AI 推理通信到实盘下单、状态记录以及消息推送的全工作流。

## 📁 目录结构与功能说明

### 1. 数据与缓存 (`data/`)

存放系统运行时的本地状态数据和临时缓存。

- **`data/cache/`**: 本地行情数据缓存目录，由 `shared_utils.py` / `stockscope/fetch.py` 统一管理（`read_cache` / `write_cache`），用于减少外部 API（长桥/富途）的重复调用频率。缓存项包括标的静态信息（永久缓存）、估值指标（当日缓存）、日K线（增量缓存）、分钟K线（当日缓存）以及盘前数据快照（`premarket_memo_*.json`）。
- **`data/daily_trading_plan.json`**: 整个交易系统运行的核心状态文件。此文件由盘前策略（Premarket Planner）通过 AI 分析生成，记录了各标的的操作网格（触发价、操作方向、冷却时间等），供日内哨兵（Intraday Sentry）实时读取和更新。字段包括 `status`, `macro_thesis`, `update_time`, `zones`（多级网格：`stop_loss`, `take_profit`, `buy_dip`, `buy_breakout`, `add_position`, `buy_oversold`）、`cooldown_until`（战术冷却时间戳）和 `pending_order`（挂单锁，含 `id` 和 `time`）。

### 2. 券商集成底层 SDK

#### 长桥模块 (`longbridge/`)

- **`longbridge_server.py`** (1398 行): 长桥证券的核心 Python API 封装层及 FastMCP 服务器。包含：
  - **行情数据**: 实时报价（`get_live_quote`）、含盘前/盘后的延伸报价（`_logic_get_extended_quote`，自动选时段最新价）、基础档案（`get_static_info`）、计算指标（`get_financial_indexes`，支持自定义指标列表）、资金流向分布（`get_capital_distribution`，大/中/小单三档）、市场温度（`get_market_temperature` + 历史温度 `get_market_temperature_history`）。
  - **K 线数据**: 通用 K 线获取（`_logic_get_history_kline`，V6.6 支持自动翻页突破 1000 根限制）、导出工具（`export_kline_data_to_json`，直写磁盘不经大模型）。
  - **期权数据**: 期权到期日列表（`get_option_expiry_dates`）、期权链（`get_option_chain_by_date`）、期权深度行情（`get_option_market_data`），含 YahooQuery 自动降级（权限不足时 `301604` 错误触发）。
  - **交易执行**: 账户资产查询（`get_account_asset`，兼容多版本 SDK 结构）、限价单提交（`submit_trade_order`，内置 TG 通知）、撤单（`cancel_trade_order` / `cancel_order_by_id`）、订单状态查询（`get_order_status_by_id`，含今日 + 近7日历史）、今日标的订单列表（`_logic_get_today_orders_by_symbol`）。
  - **聚合工具**: 全维度数据包（`get_full_analysis_report`，一键获取行情+资金+K线+估值）、保存工具（`save_analysis_to_file`，自动附加缓存数据）。
  - **辅助工具**: 交易日历查询（`get_trading_days`，含半日市标注，支持市场时区感知）、CalcIndex 全量指标字典（`get_calc_index_dictionary`，零 API 调用纯本地）、连接回收（`close_contexts`）。
  - **MCP 入口**: 通过 `FastMCP("longbridge-official")` 注册所有工具，供 OpenClaw 的 strategies 层调用。

#### 富途模块 (`futu/`)

- **`futu_options_server.py`** (677 行): 富途 (Futu OpenD) 的 API 封装层及 FastMCP 服务器封装。专注于期权数据获取，采用三步工作流：
  1. `get_option_expiry_dates` — 获取所有到期日（自动过滤已过期条目）
  2. `get_option_chain_by_date` — 根据到期日获取期权合约列表（静态信息，精简输出核心字段）
  3. `get_option_market_data` — 批量获取合约实时行情（含 IV、OI、Greeks，单次上限 200 个）
  - **聚合工具**: `get_option_full_analysis` — 一步完成期权链查询 + 实时行情拉取 + 最大痛点（Max Pain）计算。
  - **智能降级**: 当富途权限不足时（错误码 4106/4001 或 "no quota"），自动降级到 YahooQuery 拉取美股期权数据。港股无法降级。降级通道通过 `_parse_futu_us_option_code` 解析富途代码格式（`US.AAPL260320C242500`）转换为 YahooQuery 查询参数。
  - **连接管理**: 懒初始化 `OpenQuoteContext`，通过 `close_connections` / `atexit` 自动回收。

### 3. 系统调度与策略编排 (`strategies/`)

交易体系的核心逻辑，构成了"计划您的交易，交易您的计划"的全自动生命周期。

#### 共享工具模块 (`shared_utils.py`, 789 行)

`premarket_planner.py` 和 `intraday_sentry.py` 的公共逻辑提取层：

- **路径与环境**: 统一初始化 `ROOT_DIR`、`PLAN_FILE`、`CACHE_DIR`，确保 `longbridge/`、`futu/`、`telegram/` 模块可寻址。
- **缓存读写**: `read_cache` / `write_cache` — JSON 文件缓存，供全系统共用。
- **交易计划读写**: `load_plan` / `save_plan` — 对 `daily_trading_plan.json` 的统一读写接口。
- **代码转换**: `lb_to_futu` — 长桥格式 (`AAPL.US`) 到富途格式 (`US.AAPL`) 的转换，港股自动补零至 5 位。
- **基础数据获取**: `fetch_static_info`（永久缓存）、`fetch_financial_indexes`（当日缓存）— 延迟导入 longbridge_server 避免循环依赖。
- **Tavily 资讯**: `fetch_latest_news` — 通过 Tavily Search API 拉取最新市场资讯，支持 `basic`（盘中，3 条，10s 超时）和 `advanced`（盘前，5 条，15s 超时）两种模式。
- **期权 ATM 探针**: `fetch_option_snapshot` — 智能选期（本周末 + 中期 + 四周后，自适应调整避免日期过近），提取 ATM Call/Put 的 IV/OI。
- **AI 多通道调用引擎**:
  - `_call_openai_compatible` — 通用底层引擎，适配任何 OpenAI 兼容端点，内置指数退避重试 + 随机抖动（±25%），支持 429/500/502/503/504 状态码重试。
  - `_call_openclaw` — OpenClaw 网关通道薄包装。
  - `_call_new_api` — VPS new-api 通道薄包装，支持多提供商配置（当前 Vertex_AI/Gemini）。
  - `call_ai_with_retry` — 统一入口，按优先级链 `NewAPI/Vertex_AI → OpenClaw` 依次尝试，失败自动降级。全局 `threading.Lock` 串行化防雪崩。
  - `resolve_ai_model_name` / `format_ai_meta_footer` — AI 元数据解析与格式化（通道/提供商/模型名 + Token 用量）。

#### 盘前谋划 (`premarket_planner.py`, 946 行)

每日在固定的开盘前时段（港股 08:30，美股 20:15）由 `schedule` 定时任务触发执行。

- **执行流程** (8 步):
  1. **交易日判断** (`is_trading_day`): 通过长桥交易日历 API 判断，非交易日跳过并通知 TG。
  2. **账户状态** (`fetch_account_status`): 获取购买力、现金余额、全部持仓，计算同市场仓位占比（`市值 ÷ (持仓合计 + 现金)`）。
  3. **市场温度** (`fetch_market_temperature`): 当前温度 + 近 5 个交易日历史温度趋势。
  4. **标的基本面** (`fetch_static_info`): 基础信息（缓存）+ 扩展估值指标（PE/PB/市值/换手率/振幅/5日/10日涨跌幅，实时刷新）。
  5. **60 日日 K 线** (`fetch_daily_kline`): 增量缓存策略，只在缺口时补拉，附 30/60 日极值统计。
  6. **3 日 10 分钟 K 线** (`fetch_min10_kline`): 交易日历驱动，美股按美东时间分段标注（盘前/盘中/盘后）。
  7. **美股盘前 5 分钟 K 线** (`fetch_premarket_kline_us`): 仅美股，04:00-09:29 美东时间。
  8. **实时价格**: 美股用 `_logic_get_extended_quote`（含盘前/盘后），港股用 `_logic_get_live_quote`。
  9. **期权数据** (`fetch_option_snapshot`): 通过 `shared_utils` 统一调用富途 API + YahooQuery 降级。
  10. **最新资讯** (`fetch_latest_news`): Tavily 深度搜索模式。
  11. **AI 推理**: 将全量数据组装为盘前分析提示词（`build_premarket_message`），加载 `premarket_planner_prompt.md` 作为 system prompt，调用 `call_ai_with_retry`（max_tokens=16384, timeout=300s）。
  12. **结果处理** (`handle_ai_result`): 正则提取 `json` 代码块写入 `daily_trading_plan.json`（清除旧 cooldown/pending_order），替换 AI 回复中的 JSON 为落盘后权威数据，分段推送到 TG Analysis 频道。
- **批量调度** (`run_premarket_batch`): 逐标的分析，标的间隔 5 分钟（防 API 限流），完成后释放长桥 + 富途连接。
- **标的列表**:
  - 港股: `0700.HK`, `09988.HK`, `01810.HK`, `00100.HK`, `02513.HK`, `06082.HK`
  - 美股: `NVDA.US`, `TSLA.US`, `GOOGL.US`, `AMD.US`, `AAPL.US`, `INTC.US`, `MU.US`, `SNDK.US`, `DRAM.US`, `GLD.US`

#### 日内哨兵 (`intraday_sentry.py`, 1201 行)

盘中持续高频轮询的后台交易神经中枢。

- **市场状态检测** (`_get_market_status`):
  - 基于长桥 `trading_days` API + 精确交易时段判断（港股 09:30-12:00/13:00-16:00，美股 09:30-15:59 盘中 + 16:00-20:00 盘后）。
  - 交易日结果按日缓存，避免重复 API 调用。
  - 非交易时段自动计算距下一时段的秒数，长休眠前释放长桥 + 富途连接。
- **双频道 TG 推送**:
  - `tg_analysis` → Analysis 频道（AI 报告、盘前分析、系统状态）
  - `tg_order` → Order 频道（订单成交/撤单/告警）
- **主循环三阶段判定** (`run_sentry`, 2 分钟轮询间隔):
  1. **订单状态机** (`_check_pending_order`): 检查 `pending_order` 锁，处理已成交（Filled → 强制唤醒重铸网格）、已失效（Canceled/Rejected/Expired/PartialWithdrawal → 强制唤醒）、挂单超时（>15 分钟 → 战术撤单 + 强制唤醒）、撤单中（WaitToCancel/PendingCancel → 等待）、仍在等待（Pending → 跳过）、未知状态（锁定等人类介入）。状态流转后立即持久化 JSON。
  2. **战术冷却检查**: 有 `cooldown_until` 且未过期则跳过（强制唤醒可绕过）。
  3. **网格触线判定** (`_check_zone_hit`): 按优先级遍历 zones（`stop_loss > take_profit > add_position > buy_oversold > buy_dip > buy_breakout`），支持自定义 zone（如 `tp_1`, `tp_2`）。
- **网格触线裁决流程** (`process_grid_trigger`):
  1. 统一数据采集 (`_collect_market_data`): 账户状态、静态信息、估值指标、市场温度、资金流向、今日 5 分钟 K 线（美股按盘前/盘中/盘后分段）、期权探针、今日订单、盘前缓存快照。
  2. 新闻策略: 日内波动 ≥2% 时才调用 Tavily，节省 API 配额。
  3. AI 裁决 (`call_ai` + `INTRADAY_SENTRY_PROMPT`): max_tokens=8192, timeout=180s。
  4. 结果处理 (`handle_ai_verdict`): 正则提取 `[ACTION: BUY/SELL/HOLD, QTY:, PRICE:, REASON:]` 指令 → 调用 `submit_trade_order` 实盘下单 → 根据交易/观望分别更新网格 JSON（交易时写入 `pending_order` 锁 + 短冷却 5 分钟，观望时全面吸收 AI 新网格 + 长冷却 30 分钟）。
  5. 战术冷却: 每次处理完强制 60 秒休眠。
- **订单事件重构流程** (`process_order_event`):
  - 使用 `INTRADAY_REBUILD_PROMPT`，AI 只输出 JSON 网格（禁止下单指令）。
  - `handle_rebuild_result`: 全面吸收 AI 新网格，AI 未设 cooldown 时默认 30 分钟。

### 4. 工具模块

#### 消息推送 (`telegram/`)

- **`tg_sender.py`** (189 行): 统一的 Telegram 异步消息推送器。
  - **HTML 渲染**: Markdown `**粗体**` → `<b>` 标签，`` `代码` `` → `<code>` 标签。
  - **代码块折叠**: ` ```json ``` ` 代码块自动渲染为 `<blockquote expandable>` 折叠区；超长普通文本同理。
  - **自动 Tag 提取**: 从消息内容中识别标的代码（`AAPL.US`）、市场标志（🇺🇸 #US）、标的名称（#腾讯控股），并根据目标频道附加 `#Order` / `#Analysis` 固定标签。
  - **异步发送**: `send_message_async` 通过 `threading.Thread` (daemon) 后台发送，不阻塞主交易线程。
  - **限流保护**: 每条消息发送间隔 4 秒防止 TG API 频控。

- **`tg_trade_bot.py`** (434 行): 用户交互侧的 Telegram Bot，基于 `python-telegram-bot` 库。
  - **鉴权**: `@restricted` 装饰器限制仅允许环境变量 `TG_CHAT_ID` 指定的用户操作。
  - **命令**:
    - `/buy` / `/sell` — 交易记录：自动解析市场/代码/数量/价格/手续费/日期（支持 YYYY-MM-DD / YYYYMMDD / MMDD / DD 等多种日期格式），调用 `notion_database_manager.record_transaction` 写入流水 + 同步持仓。
    - `/export` — 数据导出：按市场(HK/US) + 表类型(持仓/流水)导出 Notion 全量数据为 CSV 文件，直接作为 TG 文档发送。
    - `/fetch` — 行情采集：异步调用 `stockscope/fetch.py` 采集多维度行情快照，将生成的 Markdown 报告文件作为 TG 文档发送。
    - `/passwd` — 密码生成：调用 `random_password/random_passwd.py`，支持自定义长度/字符类型/符号列表，密码以 TG Spoiler 格式隐藏。

#### 行情探查器 (`stockscope/`)

- **`fetch.py`** (652 行): 独立的命令行工具，一键抓取指定标的的全维度行情快照。
  - **数据维度**: 实时行情、基础信息（永久缓存）、估值指标（当日缓存）、市场温度（含 5 日历史）、资金流向分布、60 日日 K 线（增量缓存）、3 日 10 分钟 K 线（交易日历驱动，美股过滤盘前/盘后）、今日 5 分钟 K 线、期权 ATM 探针（本周末 + 两周后，含 YahooQuery 降级）。
  - **输出**: Markdown 格式报告，保存至 `data/` 目录，文件名格式 `<SYMBOL>_<date>.md`。
  - **连接管理**: 执行完毕后显式释放长桥 + 富途连接，`os._exit(0)` 强制退出绕过非 daemon 线程。

#### Notion 数字资管与助手面板 (`notion/`)

- **`notion_database_manager.py`** (202 行): 核心记录引擎。
  - `record_transaction`: 向 Notion 流水表（`DB_TRANS_HK` / `DB_TRANS_US`）写入交易记录，成功后自动触发 `update_position` 同步持仓。
  - `update_position`: 更新 Notion 持仓表（`DB_POS_HK` / `DB_POS_US`），买入时按加权平均计算新成本价（含手续费），卖出时保持原成本价。空仓卖出拦截。首次建仓自动新建持仓行。
  - `sync_daily_pnl_snapshot`: 向每日盈亏表（港股合并表 `DB_DAILY_PNL_HK`，美股表 `DB_DAILY_PNL_US`）写入每日快照；港股通过 `Platform` 区分 Trade25/Futu。
  - `export_data_to_file`: 翻页安全的全量 CSV 导出（上限 5000 条），自动解析 Notion 所有属性类型（title/rich_text/number/select/date/formula），保存至脚本同级 `exported_data/` 目录。

- **`notion_price_syncer.py`**: 后台定时同步精灵（基于 APScheduler `BlockingScheduler`），负责日常最新价格和每日盈亏快照，不需要额外日常 PnL 脚本。
  - **交易日判断**: 每个任务入口先通过长桥交易日历确认对应市场今天是否交易，假期自动跳过。
  - **T-1 收盘价**: 港股 08:30（开盘前）使用历史日 K 最新 close；美股 16:00 ET 盘后开始时先把 T-1 基准切到当日常规盘收盘价，20:05 ET 再用官方日 K close 修正，和每日盈亏快照保持同一收盘口径。
  - **实时价格**: 港股 09:25 建立/刷新当日快照，09:30-16:10 每 5 分钟刷新；美股 04:00-15:55 ET 每 5 分钟刷新当日快照，16:00-20:00 ET 盘后价格归入下一美股交易日快照。港股用 `_logic_get_live_quote`，美股用 `_logic_get_extended_quote`（自动选时段最新价）。
  - **每日盈亏快照**: 每轮价格更新结束后刷新当日快照，写入日度 PnL、累计 PnL、持仓市值、成本基准等字段；港股写入合并表并按 `Platform` 区分 Trade25/Futu，美股写入独立表。
  - **架构**: 不直接依赖 longbridge SDK，所有 API 调用通过 `longbridge_server.py` 封装函数完成。

- **`notion_mcp_server.py`** (43 行): Notion 集成的 MCP（Model Context Protocol）服务框架入口。
  - 暴露 `record_transaction`、`update_position`、`query_portfolio_data` 三个 MCP 工具。
  - 底层调用 `notion_database_manager.py` 的同名函数。

#### 随机密码生成器 (`random_password/`)

- **`random_passwd.py`** (195 行): 命令行密码生成工具。
  - 支持自定义长度（6-60）、大写/小写/数字/符号开关。
  - 符号参数支持传入自定义符号列表（如 `~!@#$`）。
  - 智能比例分配：数字+符号各占 ≥1/4；字母占 ≥1/2；大小写各占字母的 ≥1/3。
  - 通过 `tg_trade_bot.py` 的 `/passwd` 命令暴露给 Telegram 用户。

### 5. 系统指令大脑 (`prompts/`)

AI 模型做决定的"大纲"和"性格设计"。

- **`premarket_planner_prompt.md`** (160 行): 指导模型如何在开盘前综合各项基本面/技术面信息来规划全天交易网格。
  - 分析框架：技术面（最高权重）→ 估值面 → 期权面 → 资讯面 → 市场环境。
  - 仓位纪律：单只持仓上限 55% 净资产，优先用现金建仓，空仓禁设 stop_loss。
  - JSON 契约：`status`, `macro_thesis`, `update_time`, `_ai_model`, `zones` 字段规范。
  - 持仓 vs 空仓差异化 zone 设置规则。

- **`intraday_sentry_prompt.md`** (120 行): 指导模型如何在发现网格越界时立刻启动防御研判。
  - 决策链条：资金池约束 → 盘前战略回顾 → 盘中微观信号（5min K 线 + 主力资金终审权 + 期权异动 + 黑天鹅防线）。
  - 双部分输出：`[ACTION: BUY/SELL/HOLD]` 指令 + JSON 网格重构。
  - BUY/SELL 时只设冷却（底层锁定等订单成交后再大修）；HOLD 时必须大修网格（防同一失效点位重复触发）。

- **`intraday_rebuild_prompt.md`** (101 行): 指导模型针对订单成交/失效后如何重构交易网格。
  - 场景：订单成交、挂单超时撤销、订单被拒。
  - 严禁下单（`[ACTION]` 指令），仅输出 JSON 网格。
  - 强制设置 `cooldown_until`（止损后 2-6h，止盈后 1-2h，建仓后 30-60min）。
  - 新 BUY 网格必须距刚卖出价 >1.5% 防噪音。

---

## 🔄 整体系统工作流 (Workflow)

这是一个形成彻底闭环的量化 AI 自动化监控系统，从观察、预演、防御、扣动扳机到投后记录全部独立自主闭环。

1. **资产同步预备（自动化底盘维稳）**
   - **执行主体**: `notion_price_syncer.py` 等后台精灵脚本。
   - **流程**: 无论是否产生交易，后台任务每 5 分钟无感运行，借助长桥 API 拉取行情刷新到 Notion 数据库，甚至在美股盘前/夜盘自动寻找有效报价以更新资产表。同时保持 Telegram 操作机器人在线，随时响应人类直接的 `/buy` 和 `/fetch` 干预，全天候修正确认资产的账本健康。

2. **制定战略（盘前生成与排兵布阵）**
   - **执行主体**: `premarket_planner.py`
   - **流程**: 在每个交易日开局前抢先发难。脚本并行调度跨券商数据（长桥取技术和盘面温度，富途查期权合约极值痛点），连同外部引擎捕获的市场新闻情绪，组合成"盘前大数据全息沙盘"。基于 `premarket_planner_prompt.md` 唤醒推理引擎（如本地大模型），AI 对当天大盘和标的走向拍板定调，划定支撑位阻力位的具体操作预案（买入/卖出区间等）。最终将这些参数写死到标准 JSON 网格文件 `data/daily_trading_plan.json` 中作为当天唯一的行动纲领，并生成"晨间看盘日报"递送 Telegram 引发人类关注。

3. **雷达探测（盘中全天候巡逻）**
   - **执行主体**: `intraday_sentry.py`（监控相）
   - **流程**: 市场钟声一响，神经中枢哨兵脚本自动接管开机。采用主心跳探测节奏（2 分钟轮询），按极短周期读取最新的 `daily_trading_plan.json`。在每个跳动心跳内，哨兵比对线上每个标的的最新现价是否击穿了 AI 盘前布下的买卖网点（阵型边界）。在这个相变期间如果什么都没发生，哨兵保持缄默并等待下个 Tick。

4. **战术对抗（日内即时裁决与强杀执行）**
   - **执行主体**: `intraday_sentry.py`（开火相）+ `longbridge_server.py`
   - **流程**: 一旦价格击沉了网点红线，"伪突破"过滤机制马上启动。哨兵挂起对该标的的雷达盲扫模式，转为锁定狙击，急速提取该标的当时的资金分歧大小、当天的相关挂单执行状态、及期权瞬时突变数据，紧急唤醒大模型进行最终弹道二次验证（即 `intraday_sentry_prompt.md` 中的"确认与否"流程）：
     - 若模型判定为毛刺波动（如诱空/诱多骗线），反身驳回开火请求输出 `HOLD`。**此时系统会立刻提取 AI 生成的最新 JSON 网格配置，覆写更新到 `data/daily_trading_plan.json` 中**，为该标的施加数分钟战术级冻结冷却。
     - 若模型认可击穿有效且筹码健康，输出确认战术命令 `BUY/SELL`。此时系统直接调用长桥 SDK `submit_trade_order` 实现在真实账户硬性下单。**对该标的挂上 `pending_order` 锁，此时暂不更新核心网格的冷却状态**，耐心等待市场撮合。

5. **订单状态巡检与网格落锁更新**
   - **执行主体**: `intraday_sentry.py`（状态机）
   - **流程**: 开火下单后并非就此不管。在后续的每个心跳周期中，哨兵会专门针对有 `pending_order`（挂单中）的标的拦截查询长桥底层订单状态（`get_order_status_by_id`）。
     - 如果订单 **已成交 (Filled)** 或 **被拒绝/撤销 (Canceled/Rejected/Expired/PartialWithdrawal)**：交易结果尘埃落定。系统会立刻解除本地锁定状态，**唤醒 AI 使用 `intraday_rebuild_prompt.md` 重构网格**，刷新冷却期与新水位，使得该标的重返雷达常规侦测池。
     - 如果订单 **挂单超时（超过 15 分钟未成交）**：系统会强行发起底层战术撤单。并在成功后，以"挂单超时未成交"为特殊注入事件，无视该标的的常规冷却期限制，直接强制再次唤醒 AI 重新索要对策。

6. **战后报告与清算回传**
   - **执行主体**: `tg_sender.py` / `notion_database_manager.py` / 用户介入
   - **流程**: 系统每个节点的事件轮转——包括清晨收到 AI 万字研报布阵、雷达警报截获日志、子弹离膛提交命令、挂单超时撤单防流控拦截、直至真实的券商成交通知全回流等所有微观进程，均在 Telegram 消息管道被分级分色呈现（Analysis 频道收 AI 报告，Order 频道收订单事件）。如果产生真实成交，通过 `notion_price_syncer` 或 Telegram Bots 可追踪流水落回 Notion。这最终实现了彻底脱手的量化体系体验：AI 只负责厮杀，人类只负责接收结果报告。

---

## 🔌 环境变量清单

| 变量名 | 用途 | 使用模块 |
| --- | --- | --- |
| `LONGBRIDGE_APP_KEY` | 长桥 API Key | `longbridge_server.py` |
| `LONGBRIDGE_APP_SECRET` | 长桥 API Secret | `longbridge_server.py` |
| `LONGBRIDGE_ACCESS_TOKEN` | 长桥 Access Token | `longbridge_server.py` |
| `OPEND_HOST` / `OPEND_PORT` | 富途 OpenD 连接地址 | `futu_options_server.py` |
| `NOTION_TOKEN` | Notion API Token | `notion/` 全部模块 |
| `DB_POS_HK` / `DB_POS_US` | Notion 持仓表 Data Source ID | `notion_database_manager.py`, `notion_price_syncer.py` |
| `DB_TRANS_HK` / `DB_TRANS_US` | Notion 流水表 Data Source ID | `notion_database_manager.py` |
| `DB_DAILY_PNL_HK` / `DB_DAILY_PNL_US` | Notion 每日盈亏表 Data Source ID；港股表通过 `Platform` 区分 Trade25/Futu | `notion_database_manager.py`, `notion_price_syncer.py`, `pnl_dashboard.py` |
| `TG_BOT_TOKEN_CLAW` | TG Bot Token (策略频道) | `premarket_planner.py`, `intraday_sentry.py` |
| `TG_BOT_TOKEN_QUANT` | TG Bot Token (交易频道) | `longbridge_server.py`, `intraday_sentry.py`, `tg_trade_bot.py` |
| `TG_CHANNEL_ID_ANALYSIS` | TG Analysis 频道 ID | `premarket_planner.py`, `intraday_sentry.py` |
| `TG_CHANNEL_ID_ORDER` | TG Order 频道 ID | `longbridge_server.py`, `intraday_sentry.py` |
| `TG_CHAT_ID` | TG 管理员用户 ID (鉴权) | `tg_trade_bot.py` |
| `TAVILY_API_KEY` | Tavily Search API Key | `shared_utils.py` |
| `OPENCLAW_GATEWAY_TOKEN` | OpenClaw 网关 Token | `shared_utils.py` |
| `NEW_API_BASE_URL` | VPS new-api 基础 URL | `shared_utils.py` |
| `NEW_API_KEY_Vertex_AI` | new-api Vertex AI 渠道 Key | `shared_utils.py` |

---

## 🚀 部署与运行

```bash
# 安装依赖
pip install -r requirements.txt

# 后台常驻服务（建议使用 systemd 或 pm2）
python strategies/premarket_planner.py    # 盘前谋划调度器
python strategies/intraday_sentry.py      # 日内哨兵主循环
python notion/notion_price_syncer.py      # Notion 价格 + 每日盈亏快照同步器
python notion/pnl_dashboard.py            # 每日盈亏 Web 看板
python telegram/tg_trade_bot.py           # Telegram 交易指令 Bot

# MCP 服务（供 OpenClaw Gateway 调用）
python longbridge/longbridge_server.py    # 长桥 MCP 服务
python futu/futu_options_server.py        # 富途期权 MCP 服务
python notion/notion_mcp_server.py        # Notion MCP 服务

# 独立工具
python stockscope/fetch.py AAPL.US 0700.HK    # 行情快照采集
python random_password/random_passwd.py 32     # 密码生成
```
