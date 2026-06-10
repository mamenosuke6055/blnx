import sqlite3
import pandas as pd
import json
from pathlib import Path

def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent

def get_db_path():
    root = get_project_root()
    config_path = root / "config/settings.json"
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    return root / config.get("db_path", "db/finance.db")

def generate_portfolio_report():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)

    # 1. 資産(ASSET)勘定の残高を集計
    # quantity: 保有数量 (株数, 口数, 通貨量)
    # value: 簿価 (取得等のための金額ベース)
    query = """
    SELECT 
        a.name,
        a.ofx_type,
        c.mnemonic as currency,
        SUM(CAST(s.quantity_num AS REAL) / s.quantity_denom) as units,
        SUM(CAST(s.value_num AS REAL) / s.value_denom) as book_value
    FROM accounts a
    JOIN splits s ON a.guid = s.account_guid
    LEFT JOIN transactions t ON s.tx_guid = t.guid
    LEFT JOIN currencies c ON t.currency_guid = c.guid
    WHERE a.account_type = 'ASSET'
    GROUP BY a.guid
    HAVING abs(units) > 0.000001
    ORDER BY a.ofx_type DESC, a.name;
    """
    
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        print("保有資産は見つかりませんでした。")
        return

    # 見やすく整形
    # 投資商品 (INVESTMENT) と 現金/銀行 (BANK/CASH) を分けるなど
    
    print("\n=== ポートフォリオ概要 (簿価ベース) ===")
    
    # 投資商品
    investments = df[df['ofx_type'] == 'INVESTMENT'].copy()
    if not investments.empty:
        print("\n[投資商品]")
        # 簡易的な現在単価計算 (簿価 / 数量) -> これは平均取得単価
        investments['avg_price'] = investments['book_value'] / investments['units']
        
        # 表示用カラム選択
        display_inv = investments[['name', 'units', 'currency', 'book_value', 'avg_price']]
        # 数値フォーマット
        pd.options.display.float_format = '{:,.2f}'.format
        print(display_inv.to_string(index=False))
        
        total_inv = investments['book_value'].sum() # 通貨混在だと単純合計は危険だが、一旦表示
        print(f"\n投資商品合計(簿価/通貨混在): {total_inv:,.2f}")

    # 現金・銀行
    cash = df[df['ofx_type'].isin(['BANK', 'CASH', None]) & (df['name'] != 'Placeholder')].copy() # None for generic ASSET
    # Remove rows that look like purely placeholder or intermediate if necessary
    
    if not cash.empty:
        print("\n[現金・預金]")
        display_cash = cash[['name', 'units', 'currency', 'book_value']]
        print(display_cash.to_string(index=False))
        
    print("\n=======================================")
    print("※ 注意: 'book_value' は取得原価（簿価）の合計です。現在の時価評価額ではありません。")
    print("※ 投資信託等の時価評価を行うには、最新の基準価額データが必要です。")

if __name__ == "__main__":
    generate_portfolio_report()
