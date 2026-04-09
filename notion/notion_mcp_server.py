import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 确保能寻址同目录的 notion_database_manager
from fastmcp import FastMCP
import notion_database_manager  # 引入我们写好的纯净版 API 库

# 创建一个新的 MCP 服务器
mcp = FastMCP("Notion-Quant-Manager")

@mcp.tool()
def record_transaction(market: str, action: str, name: str, code: str, date: str, amount: int, price: float, fee: float):
    """
    当用户告诉你他买入或卖出了股票时（记账指令），调用此工具记录到 Notion 流水表。
    参数要求:
    - market: "US" 或 "HK"
    - action: 必须是 "Buy" 或 "Sell"
    - code: 股票代码 (例如 AAPL, 0700.HK)
    - date: 交易日期，格式 YYYY-MM-DD
    """
    return notion_database_manager.record_transaction(market, action, name, code, date, amount, price, fee)

@mcp.tool()
def update_position(market: str, name: str, code: str, action: str, amount: int, price: float, fee: float):
    """
    重要：在调用 record_transaction 记录流水后，必须紧接着调用此工具来同步更新用户的 Notion 持仓表（计算平均成本）。
    """
    return notion_database_manager.update_position(market, name, code, action, amount, price, fee)

@mcp.tool()
def query_portfolio_data(market: str, table_type: str):
    """
    当用户询问他的持仓情况、盈亏情况，或要求复盘时，调用此工具获取底层数据。
    - market: "US" 或 "HK"
    - table_type: "Position" (查询持仓) 或 "Transaction" (查询流水)
    """
    result = notion_database_manager.export_data_to_file(market, table_type)
    return {
        "raw_data": result,
        "_ai_instruction": "请根据以上数据准确回答用户的问题，如果是复盘，请计算胜率或盈亏金额。"
    }

if __name__ == "__main__":
    mcp.run()