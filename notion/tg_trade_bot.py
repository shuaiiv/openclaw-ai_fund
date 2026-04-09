import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 导入你之前写好的肌肉接口层
import notion_database_manager 

from dotenv import load_dotenv, find_dotenv

# 2. ⚡️ 将 find_dotenv() 作为参数传给 load_dotenv()
load_dotenv(find_dotenv())

# =================配置区域=================
# 建议通过环境变量读取，或者直接替换为字符串
import os
TOKEN = os.getenv("TG_BOT_TOKEN_QUANT")
# ==========================================

# 设置日志，方便在 systemd 中查看报错
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 1. 核心交易处理逻辑 (处理 Buy 和 Sell)
async def handle_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, side: str):
    try:
        # 参数结构: /buy HK 美团 03690 2026-03-12 100 76.8 60
        # 参数数量应为 7 个
        if len(context.args) < 7:
            raise ValueError("参数数量不足")

        raw_market, name, raw_code, date, amount, price, fee = context.args
        
        # 1.1 市场识别增强
        if raw_market.upper() in ["HK", "港股"]:
            market = "HK"
        elif raw_market.upper() in ["US", "美股"]:
            market = "US"
        else:
            await update.message.reply_text(f"❌ 无法识别市场: {raw_market}\n请使用: HK/港股 或 US/美股")
            return

        # 1.2 代码格式清洗与标准化 (确保输出永远带有 HK. 或 US. 前缀)
        # 第一步：统一转大写，并去掉用户可能误输入的旧前缀，拿到纯代码（如 03690 或 AAPL）
        pure_code = raw_code.upper().replace("HK.", "").replace("US.", "")
        
        # 第二步：根据识别出的市场，强制补全前缀
        if market == "HK":
            code = f"HK.{pure_code}"  # 变成 HK.03690
        else:
            code = f"US.{pure_code}"  # 变成 US.AAPL
        
        # 1.3 调用逻辑层写入 Notion
        # 注意：notion_db_manager 内部应处理 side="Sell" 时数量转负数的逻辑
        res = notion_database_manager.record_transaction(
            market=market,
            action=side,
            name=name,
            code=code,
            date=date,
            amount=int(amount),
            price=float(price),
            fee=float(fee)
        )
        
        icon = "🟢" if side == "Buy" else "🔴"
        status_text = "买入" if side == "Buy" else "卖出"
        
        await update.message.reply_text(
            f"{icon} 识别为 [{market}] 市场 {status_text}\n"
            f"--------------------------\n"
            f"{res['msg']}"
        )
        
    except Exception as e:
        logging.error(f"交易记录失败: {str(e)}")
        await update.message.reply_text(
            f"❌ 录入失败!\n原因: {str(e)}\n\n"
            f"💡 正确格式:\n/{side.lower()} 市场 名字 代码 日期 数量 价格 手续费\n"
            f"示例: /{side.lower()} HK 美团 03690 2026-03-12 100 76.8 60"
        )

# 2. 导出逻辑
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

# 3. 命令注册
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_trade(update, context, "Buy")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_trade(update, context, "Sell")

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

    print("🚀 Command Bot 正在运行...")
    print("✅ 已载入命令: /buy, /sell, /export")
    
    # 启动机器人
    app.run_polling()

if __name__ == "__main__":
    main()