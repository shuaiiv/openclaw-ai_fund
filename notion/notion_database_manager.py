from notion_client import Client
import csv
import os
import datetime

from dotenv import load_dotenv, find_dotenv

# 2. ⚡️ 将 find_dotenv() 作为参数传给 load_dotenv()
load_dotenv(find_dotenv())
notion = Client(auth=os.getenv("NOTION_TOKEN"))

# 你的这 4 个常量里，装的其实已经是 Data Source ID 了
DB_POS_HK = os.getenv("DB_POS_HK")
DB_TRANS_HK = os.getenv("DB_TRANS_HK")
DB_POS_US = os.getenv("DB_POS_US")
DB_TRANS_US = os.getenv("DB_TRANS_US")

# ==========================================
# 肌肉功能 1：记录交易流水 (升级版：自带持仓同步)
# ==========================================
def record_transaction(market: str, action: str, name: str, code: str, date: str, amount: int, price: float, fee: float):
    ds_id = DB_TRANS_HK if market == "HK" else DB_TRANS_US
    try:
        # 1. 先写流水账
        notion.pages.create(
            parent={"type": "data_source_id", "data_source_id": ds_id},
            properties={
                "Stock Name": {"title": [{"text": {"content": name}}]},
                "Stock Code": {"rich_text": [{"text": {"content": code}}]},
                "Action": {"select": {"name": action}},
                "Date": {"date": {"start": date}},
                "Price": {"number": float(price)},
                "Count": {"number": int(amount)},
                "Trade Fee": {"number": float(fee)}
            }
        )
        
        # 2. 🚀 流水写入成功后，自动触发持仓更新逻辑！
        pos_result = update_position(market, name, code, action, amount, price, fee)
        
        # 3. 将两者的结果打包返回给大模型
        if pos_result["status"] == "success":
            return {"status": "success", "msg": f"✅ 流水记录成功，且已同步持仓！({pos_result['msg']})"}
        else:
            return {"status": "warning", "msg": f"⚠️ 流水记录成功，但持仓同步失败: {pos_result['msg']}"}
            
    except Exception as e:
        return {"status": "error", "msg": f"❌ 流水记录失败: {str(e)}"}

# ==========================================
# 肌肉功能 2：更新持仓数据
# ==========================================
def update_position(market: str, name: str, code: str, action: str, amount: int, price: float, fee: float):
    ds_id = DB_POS_HK if market == "HK" else DB_POS_US
    try:
        # 🎯 直接拿你的 ID 去查
        query = notion.data_sources.query(
            data_source_id=ds_id,
            filter={"property": "Stock Code", "rich_text": {"equals": code}}
        )
        
        if query['results']:
            page_id = query['results'][0]['id']
            props = query['results'][0]['properties']
            old_count = props['Count'].get('number', 0)
            old_unit_price = props['Unit Price'].get('number', 0)
            
            if action == "Buy":
                new_count = old_count + amount
                total_cost = (old_count * old_unit_price) + (amount * price) + fee
                new_unit_price = total_cost / new_count if new_count > 0 else 0
            elif action == "Sell":
                new_count = old_count - amount
                new_unit_price = old_unit_price 
            else:
                return {"status": "error", "msg": "未知的交易动作"}

            notion.pages.update(
                page_id=page_id,
                properties={
                    "Count": {"number": new_count},
                    "Unit Price": {"number": round(new_unit_price, 4)}
                }
            )
            return {"status": "success", "msg": f"已更新持仓: {code} 数量 {new_count}, 均价 {new_unit_price:.2f}"}
        else:
            if action == "Sell":
                 return {"status": "error", "msg": "没有持仓无法卖出！"}
            new_unit_price = ((amount * price) + fee) / amount
            notion.pages.create(
                parent={"type": "data_source_id", "data_source_id": ds_id}, # 🎯 直接用你的 ID
                properties={
                    "Stock Name": {"title": [{"text": {"content": name}}]},
                    "Stock Code": {"rich_text": [{"text": {"content": code}}]},
                    "Count": {"number": amount},
                    "Unit Price": {"number": round(new_unit_price, 4)}
                }
            )
            return {"status": "success", "msg": f"已新建仓位: {code} 数量 {amount}, 均价 {new_unit_price:.2f}"}
    except Exception as e:
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