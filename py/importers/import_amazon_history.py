import pandas as pd
from pathlib import Path
import sqlite3
import logging

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def import_amazon_history(file_path: Path, db_path: Path):
    """
    Amazon購入履歴CSVを読み込み、整形してfinance.dbに保存する。
    """
    logging.info(f"Amazon購入履歴CSVのインポートを開始します: {file_path}")

    try:
        df = pd.read_csv(file_path, encoding='utf-8')
        logging.info("CSVファイルを読み込みました。")

        # 必要に応じて列名の変更や不要な列の削除を行う
        # 例: df = df.rename(columns={'注文日': 'date', '商品名': 'description', '金額': 'amount'})
        # df = df[['date', 'description', 'amount']]

        # データ整形処理（日付フォーマット、金額の数値化など）
        # df['date'] = pd.to_datetime(df['date'])
        # df['amount'] = pd.to_numeric(df['amount'].str.replace(',', ''))

        # 複式簿記形式への変換ロジックをここに実装
        # 例:
        # transactions = []
        # for index, row in df.iterrows():
        #     # 支出の取引
        #     transactions.append({
        #         'date': row['date'],
        #         'description': row['description'],
        #         'amount': row['amount'],
        #         'from_account': 'Assets:Cash' if '現金' in row['payment_method'] else 'Liabilities:CreditCard:RakutenCard',
        #         'to_account': 'Expenses:Shopping:Amazon'
        #     })
        #
        #     # 支払い方法に応じた取引
        #     if 'CreditCard' in transactions[-1]['from_account']:
        #         transactions.append({
        #             'date': row['date'],
        #             'description': row['description'] + ' (カード支払い)',
        #             'amount': row['amount'],
        #             'from_account': 'Liabilities:CreditCard:RakutenCard',
        #             'to_account': 'Assets:Cash' # または銀行口座
        #         })

        # 仮の処理として、DataFrameをそのままDBに保存（後で複式簿記に変換）
        conn = sqlite3.connect(db_path)
        df.to_sql('amazon_transactions_raw', conn, if_exists='replace', index=False)
        conn.close()
        logging.info(f"整形されたAmazon購入履歴データを {db_path} に保存しました。")

    except FileNotFoundError:
        logging.error(f"ファイルが見つかりません: {file_path}")
    except Exception as e:
        logging.error(f"Amazon購入履歴のインポート中にエラーが発生しました: {e}")

if __name__ == '__main__':
    # 実行例
    # project_root = Path(__file__).resolve().parents[2]
    # sample_csv_path = project_root / 'data' / 'raw' / 'amazon' / 'amazon_history_sample.csv'
    # finance_db_path = project_root / 'db' / 'finance.db'
    #
    # # ダミーのCSVファイルを作成（テスト用）
    # if not sample_csv_path.parent.exists():
    #     sample_csv_path.parent.mkdir(parents=True)
    # pd.DataFrame({
    #     '注文日': ['2025/01/01', '2025/01/05'],
    #     '商品名': ['商品A', '商品B'],
    #     '金額': [1000, 2500],
    #     '支払い方法': ['クレジットカード', 'クレジットカード']
    # }).to_csv(sample_csv_path, index=False, encoding='utf-8')
    #
    # import_amazon_history(sample_csv_path, finance_db_path)
    pass
