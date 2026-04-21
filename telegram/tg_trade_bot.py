import logging
import os
import sys
import asyncio
import re
from datetime import date, datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

# =================配置区域=================
TOKEN = os.getenv("TG_BOT_TOKEN_QUANT")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_SCRIPT  = os.path.join(_SCRIPT_DIR, "../stockscope/fetch.py")
PASSWD_SCRIPT = os.path.join(_SCRIPT_DIR, "../random_password/random_passwd.py")

# 将 notion 目录加入模块搜索路径，以便导入 notion_database_manager
_NOTION_DIR = os.path.normpath(os.path.join(_SCRIPT_DIR, "../notion"))
if _NOTION_DIR not in sys.path:
    sys.path.insert(0, _NOTION_DIR)
# ==========================================

# 导入肌肉接口层
import notion_database_manager

# 设置日志，方便在 systemd 中查看报错
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

import functools

def restricted(func):
    """鉴权装饰器：限制仅允许环境中的 TG_CHAT_ID 用户执行命令"""
    @functools.wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        admin_id = os.getenv("TG_CHAT_ID")
        user_id = str(update.effective_user.id) if update.effective_user else ""
        if str(admin_id) != user_id:
            logging.warning(f"拦截到未授权命令请求: user_id={user_id}")
            if update.message:
                await update.message.reply_text(f"❌ 鉴权失败: 您的账号 (ID: {user_id}) 没有权限操作该机器人。")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped


# ── 日期解析工具函数 ──────────────────────────────────────────────────────────
def parse_date(raw: str) -> str:
    """
    将用户输入的各种日期格式统一解析为 YYYY-MM-DD 字符串。

    支持的格式（自动以当年/当月补全）：
      完整格式：
        YYYY-MM-DD  /  YYYY/MM/DD  /  YYYYMMDD  /  MM/DD/YYYY
      省略年份（补全为当年）：
        MMDD  /  MM-DD  /  MM/DD
      仅输入日（补全为当年当月）：
        DD（纯 1-2 位数字，值在 1-31 之间）

    返回标准格式字符串，如 "2026-04-21"。
    解析失败时抛出 ValueError。
    """
    today = date.today()
    s = raw.strip()

    # 1. 完整格式：YYYY-MM-DD 或 YYYY/MM/DD
    m = re.fullmatch(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime('%Y-%m-%d')

    # 2. 完整格式：YYYYMMDD（8位纯数字）
    m = re.fullmatch(r'(\d{4})(\d{2})(\d{2})', s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime('%Y-%m-%d')

    # 3. 完整格式：MM/DD/YYYY
    m = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).strftime('%Y-%m-%d')

    # 4. 省略年份：MM-DD 或 MM/DD（补全为当年）
    m = re.fullmatch(r'(\d{1,2})[-/](\d{1,2})', s)
    if m:
        return date(today.year, int(m.group(1)), int(m.group(2))).strftime('%Y-%m-%d')

    # 5. 省略年份：MMDD 4位纯数字（补全为当年）
    m = re.fullmatch(r'(\d{2})(\d{2})', s)
    if m:
        return date(today.year, int(m.group(1)), int(m.group(2))).strftime('%Y-%m-%d')

    # 6. 仅输入日：1-2位纯数字（补全为当年当月）
    m = re.fullmatch(r'(\d{1,2})', s)
    if m:
        day = int(m.group(1))
        if not (1 <= day <= 31):
            raise ValueError(f"日期中的「日」超出范围: {day}")
        return date(today.year, today.month, day).strftime('%Y-%m-%d')

    raise ValueError(f"无法识别的日期格式: {raw}")
# ─────────────────────────────────────────────────────────────────────────────


# ── 代码解析工具函数 ──────────────────────────────────────────────────────────
def parse_code(raw: str) -> tuple[str, str]:
    """
    从 [Market].[Symbol] 格式中解析出市场和标准化代码。

    支持的市场前缀（大小写不敏感）：HK / US
    示例：
      HK.03690  →  market="HK", code="HK.03690"
      US.AAPL   →  market="US", code="US.AAPL"
      hk.03690  →  market="HK", code="HK.03690"（自动转大写）

    解析失败时抛出 ValueError。
    """
    parts = raw.upper().split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"代码格式错误: {raw!r}，请使用 [市场].[代码] 格式，例如 HK.03690 或 US.AAPL")

    market_prefix, symbol = parts
    if market_prefix == "HK":
        market = "HK"
    elif market_prefix == "US":
        market = "US"
    else:
        raise ValueError(f"无法识别的市场前缀: {market_prefix!r}，目前支持 HK 和 US")

    code = f"{market}.{symbol}"
    return market, code
# ─────────────────────────────────────────────────────────────────────────────


# 1. 核心交易处理逻辑 (处理 Buy 和 Sell)
async def handle_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, side: str):
    """
    参数结构（市场从代码中自动识别，日期可省略）：
      /buy  名字 代码 数量 价格 手续费 [日期]
      /sell 名字 代码 数量 价格 手续费 [日期]

    代码格式：[Market].[Symbol]，例如 HK.03690 或 US.AAPL

    示例：
      /buy 美团 HK.03690 100 76.8 60              ← 日期默认今天
      /buy 美团 HK.03690 100 76.8 60 20260312     ← YYYYMMDD
      /buy 美团 HK.03690 100 76.8 60 2026/03/12   ← YYYY/MM/DD
      /buy 美团 HK.03690 100 76.8 60 03/12/2026   ← MM/DD/YYYY
      /buy 美团 HK.03690 100 76.8 60 0312          ← MMDD → 当年
      /buy 美团 HK.03690 100 76.8 60 03/12         ← MM/DD → 当年
      /buy 美团 HK.03690 100 76.8 60 12            ← DD → 当年当月
      /buy 苹果 US.AAPL 10 210.5 5
    """
    try:
        # 参数数量：5个（不含日期）或 6个（含日期）
        if len(context.args) < 5:
            raise ValueError("参数数量不足，至少需要 5 个参数")

        name, raw_code, amount, price, fee = context.args[:5]
        raw_date = context.args[5] if len(context.args) >= 6 else None

        # 1.1 从代码中自动识别市场
        try:
            market, code = parse_code(raw_code)
        except ValueError as e:
            await update.message.reply_text(
                f"❌ {e}\n\n"
                "💡 代码格式: [市场].[代码]\n"
                "  港股示例: HK.03690\n"
                "  美股示例: US.AAPL"
            )
            return

        # 1.2 日期解析（省略时使用今天）
        if raw_date is None:
            trade_date = date.today().strftime('%Y-%m-%d')
            date_note = "（默认今日）"
        else:
            try:
                trade_date = parse_date(raw_date)
                date_note = ""
            except ValueError as e:
                await update.message.reply_text(
                    f"❌ 日期格式错误: {e}\n\n"
                    "💡 支持的日期格式:\n"
                    "  完整: YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD / MM/DD/YYYY\n"
                    "  省略年份: MMDD / MM-DD / MM/DD（自动补全当年）\n"
                    "  仅输入日: DD（自动补全当年当月）\n"
                    "  省略日期: 不填则默认今天"
                )
                return

        # 1.3 调用逻辑层写入 Notion
        # 注意：notion_db_manager 内部应处理 side="Sell" 时数量转负数的逻辑
        res = notion_database_manager.record_transaction(
            market=market,
            action=side,
            name=name,
            code=code,
            date=trade_date,
            amount=int(amount),
            price=float(price),
            fee=float(fee)
        )

        icon = "🟢" if side == "Buy" else "🔴"
        status_text = "买入" if side == "Buy" else "卖出"

        await update.message.reply_text(
            f"{icon} 识别为 [{market}] 市场 {status_text}\n"
            f"📅 交易日期: {trade_date} {date_note}\n"
            f"--------------------------\n"
            f"{res['msg']}"
        )

    except Exception as e:
        logging.error(f"交易记录失败: {str(e)}")
        await update.message.reply_text(
            f"❌ 录入失败!\n原因: {str(e)}\n\n"
            f"💡 正确格式:\n"
            f"/{side.lower()} 名字 代码 数量 价格 手续费 [日期]\n\n"
            f"代码格式: [市场].[代码]，例如 HK.03690 或 US.AAPL\n\n"
            f"日期可省略（默认今天），支持多种格式:\n"
            f"  YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD / MM/DD/YYYY\n"
            f"  MMDD / MM-DD / MM/DD（自动补全当年）\n"
            f"  DD（自动补全当年当月）\n\n"
            f"示例:\n"
            f"  /{side.lower()} 美团 HK.03690 100 76.8 60\n"
            f"  /{side.lower()} 美团 HK.03690 100 76.8 60 20260312\n"
            f"  /{side.lower()} 苹果 US.AAPL 10 210.5 5 0312"
        )

# 2. 导出逻辑
@restricted
async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            raise ValueError("缺少参数")

        raw_market, raw_type = context.args

        # 2.1 市场识别
        if raw_market.upper() in ["HK", "港股"]:
            market = "HK"
        elif raw_market.upper() in ["US", "美股"]:
            market = "US"
        else:
            await update.message.reply_text(f"❌ 市场错误: {raw_market}")
            return

        # 2.2 表类型识别
        if raw_type in ["持仓", "position", "Position"]:
            t_type = "Position"
        elif raw_type in ["流水", "交易", "transaction", "Transaction"]:
            t_type = "Transaction"
        else:
            await update.message.reply_text(f"❌ 类型错误: {raw_type}\n请使用: 持仓 或 流水")
            return

        # 2.3 执行导出
        res = notion_database_manager.export_data_to_file(market, t_type)

        if res['status'] == "success":
            file_path = res['msg']
            # 🚀 核心改动：直接把文件发给用户
            with open(file_path, 'rb') as doc:
                await update.message.reply_document(
                    document=doc,
                    filename=os.path.basename(file_path),
                    caption=f"📊 {market} {t_type} 全量数据导出成功！\n共计 {res['count']} 条数据。"
                )
        else:
            await update.message.reply_text(f"❌ 导出失败: {res['msg']}")

    except Exception as e:
        await update.message.reply_text(
            "❌ 导出指令格式错误!\n\n"
            "💡 示例:\n"
            "/export 港股 持仓\n"
            "/export 美股 流水"
        )

# 3. 数据抓取逻辑
@restricted
async def fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 1:
            raise ValueError("缺少参数")

        symbols = context.args
        await update.message.reply_text(f"⏳ 正在采集 {' '.join(symbols)} 的数据，可能需要 15-30 秒，请稍候...")

        process = await asyncio.create_subprocess_exec(
            sys.executable, FETCH_SCRIPT, *symbols,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            await update.message.reply_text(f"❌ 采集失败:\n{stderr.decode('utf-8')}")
            return

        stdout_text = stdout.decode('utf-8')

        # 解析输出的文件路径
        output_files = []
        parsing_files = False
        for line in stdout_text.split('\n'):
            if "📁 全部报告：" in line:
                parsing_files = True
                continue
            if parsing_files:
                if "=" in line:
                    parsing_files = False
                    continue
                line_stripped = line.strip()
                if line_stripped:
                    output_files.append(line_stripped)

        if not output_files:
            await update.message.reply_text("❌ 数据采集可能已完成，但未能找到报告文件。\n相关日志：\n" + stdout_text[-1000:])
            return

        for file_path in output_files:
            if os.path.exists(file_path):
                with open(file_path, 'rb') as doc:
                    await update.message.reply_document(
                        document=doc,
                        filename=os.path.basename(file_path),
                        caption=f"📊 {os.path.basename(file_path)} 行情快照采集成功！"
                    )
            else:
                await update.message.reply_text(f"❌ 找不到文件: {file_path}")

    except Exception as e:
        await update.message.reply_text(f"❌ 采集指令错误: {str(e)}\n\n💡 示例:\n/fetch AAPL.US")

# 4. 命令注册
@restricted
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_trade(update, context, "Buy")

@restricted
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_trade(update, context, "Sell")

# 5. 密码生成逻辑
@restricted
async def passwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # 最多接受 5 个参数，透传给 random_passwd.py
        args = context.args[:5] if context.args else []

        process = await asyncio.create_subprocess_exec(
            sys.executable, PASSWD_SCRIPT, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            err = stderr.decode("utf-8").strip()
            await update.message.reply_text(f"❌ 生成失败:\n{err}")
            return

        output = stdout.decode("utf-8").strip()

        # 从输出中拆分 "生成的密码: XXXX" 那一行
        password = None
        info_lines = []
        for line in output.splitlines():
            if line.startswith("生成的密码:"):
                password = line.replace("生成的密码:", "").strip()
            else:
                info_lines.append(line)

        if not password:
            await update.message.reply_text(f"❌ 未能解析到密码内容:\n{output}")
            return

        info_text = "\n".join(info_lines)
        # 密码使用 Spoiler 隐藏（MarkdownV2 格式）
        escaped_info = info_text.replace(".", "\\.").replace("-", "\\-").replace("(", "\\(").replace(")", "\\)").replace("!", "\\!").replace("+", "\\+").replace("=", "\\=")
        escaped_pwd = "".join(
            f"\\{c}" if c in r"\_*[]()~`>#+-=|{}.!" else c
            for c in password
        )

        reply = (
            f"🔐 *密码已生成*\n"
            f"{escaped_info}\n"
            f"密码: ||{escaped_pwd}||"
        )
        await update.message.reply_text(reply, parse_mode="MarkdownV2")

    except Exception as e:
        await update.message.reply_text(
            f"❌ 指令错误: {str(e)}\n\n"
            "💡 用法:\n"
            "/passwd \\[长度] \\[大写0/1] \\[小写0/1] \\[数字0/1] \\[符号0/1或符号列表]\n\n"
            "示例:\n"
            "/passwd          → 默认20位\n"
            "/passwd 32       → 32位\n"
            "/passwd 16 1 1 1 ~!@#$  → 指定符号"
        )

# 主程序
def main():
    if TOKEN == "你的_NEW_BOT_TOKEN_HERE":
        print("❌ 错误: 请先在代码中填入新机器人的 Token!")
        return

    # 创建应用
    app = Application.builder().token(TOKEN).build()

    # 注册指令处理器
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("sell", sell))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("fetch", fetch))
    app.add_handler(CommandHandler("passwd", passwd))

    print("🚀 Command Bot 正在运行...")
    print("✅ 已载入命令: /buy, /sell, /export, /fetch, /passwd")

    app.run_polling()

if __name__ == "__main__":
    main()
