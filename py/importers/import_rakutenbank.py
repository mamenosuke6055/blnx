import pandas as pd
import sqlite3
import json
from pathlib import Path
import hashlib
import uuid
import glob

from py.processing.classify_bank_income import classify_income

# 口座振替でカード負債を返済するパターン。費用ではなく負債の減少として仕訳する。
# (マッチキーワード, 負債口座パス)
CARD_PAYMENT_PATTERNS = [
    ('ラクテンカ－ト゛サ－ヒ゛ス', ['Liabilities', 'Credit Card', 'Rakuten Card']),
]

def get_project_root() -> Path:
    """プロジェクトのルートディレクトリを取得します。"""
    return Path(__file__).resolve().parent.parent.parent

def guess_encoding(csv_path: Path):
    """CSVファイルの文字コードを推測します。"""
    encodings = ['utf-8', 'cp932', 'euc-jp', 'sjis']
    for enc in encodings:
        try:
            with open(csv_path, 'r', encoding=enc) as f:
                f.read()
            return enc
        except (UnicodeDecodeError, ValueError):
            continue
    return None

def get_or_create_account_guid(conn: sqlite3.Connection, name_path: list[str], account_type: str) -> str:
    """
    階層パスを指定して勘定科目のGUIDを取得または作成します。
    例: ['Assets', 'Bank', 'Rakuten Bank']
    """
    cursor = conn.cursor()
    parent_guid = None
    
    current_path = []
    for i, name in enumerate(name_path):
        current_path.append(name)
        
        query = "SELECT guid FROM accounts WHERE name = ?"
        params = [name]
        
        if parent_guid:
            query += " AND parent_guid = ?"
            params.append(parent_guid)
        else:
            query += " AND parent_guid IS NULL"
            
        cursor.execute(query, tuple(params))
        result = cursor.fetchone()
        
        if result:
            guid = result[0]
        else:
            # 勘定科目が存在しない場合は作成
            guid = uuid.uuid4().hex
            # 最後の要素以外はPLACEHOLDERとする（ただし既存のAssetsなどがPLACEHOLDERでない場合はそのままでよいが、
            # ここでは簡易的に、末尾以外はPLACEHOLDERとして作成するロジックにする。
            # ただしAssets等は既にinit_dbで作られているはずなので、基本的にはSELECTで引っかかるはず）
            
            # 親がAssets/Liabilities/Income/Expenses/Equityの直下でない場合はPlaceholderにするなどの判断が必要だが
            # ここでは末尾以外はPlaceholder=1とする
            is_placeholder = 1 if i < len(name_path) - 1 else 0
            
            # account_typeは引数のものを継承するが、ルートに近いものは固定的に決まる場合も。
            # 簡易的に引数のtypeを使う
            
            cursor.execute("""
                INSERT INTO accounts (guid, name, account_type, parent_guid, placeholder)
                VALUES (?, ?, ?, ?, ?)
            """, (guid, name, account_type, parent_guid, is_placeholder))
            print(f"勘定科目を作成しました: {' > '.join(current_path)}")
        
        parent_guid = guid
        
    return parent_guid

def import_rakuten_bank_csv(csv_path: Path, db_path: str = None):
    """
    楽天銀行の取引明細CSVを読み込み、新しい複式簿記スキーマでDBに登録します。
    """
    PROJECT_ROOT = get_project_root()
    
    if db_path is None:
        CONFIG_FILE = PROJECT_ROOT / "config/settings.json"
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            db_path = PROJECT_ROOT / settings.get("db_path", "db/finance.db")
        except FileNotFoundError:
            print(f"エラー: 設定ファイル '{CONFIG_FILE}' が見つかりません。デフォルトパス 'db/finance.db' を使用します。")
            db_path = PROJECT_ROOT / "db/finance.db"

    encoding = guess_encoding(csv_path)
    if not encoding:
        print(f"エラー: '{csv_path.name}' の文字コードを特定できませんでした。")
        return
    print(f"'{csv_path.name}' の文字コードは {encoding} と推測されました。")

    try:
        df = pd.read_csv(csv_path, encoding=encoding, header=0, on_bad_lines='skip')
    except Exception as e:
        print(f"エラー: CSVファイルの読み込みに失敗しました: {e}")
        return

    required_columns = ['取引日', '入出金(円)', '入出金内容']
    if not all(col in df.columns for col in required_columns):
        print(f"エラー: CSVファイルに必要なカラム {required_columns} が見つかりません。")
        return

    df.rename(columns={
        '取引日': 'date',
        '入出金(円)': 'amount',
        '入出金内容': 'description'
    }, inplace=True)

    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
    # 金額のカンマ除去と数値変換
    df['amount'] = df['amount'].astype(str).str.replace(',', '').astype(int)
    
    df = df[['date', 'amount', 'description']]

    df['description'] = df['description'].str.strip()

    def generate_fitid(row):
        return 'SHA256:' + hashlib.sha256(
            f"RAKUTENBANK:{row['date']}:{row['amount']}:{row['description']}".encode()
        ).hexdigest()
    df['ofx_fitid'] = df.apply(generate_fitid, axis=1)

    # --- ファイル内の重複を除外 ---
    df.drop_duplicates(subset=['ofx_fitid'], keep='first', inplace=True)

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # --- 勘定科目の取得/作成 ---
        # 資産: 楽天銀行
        bank_account_guid = get_or_create_account_guid(conn, ['Assets', 'Bank', 'Rakuten Bank'], 'ASSET')
        
        # 相手方勘定（デフォルト）
        # 収入: Income:Uncategorized
        income_account_guid = get_or_create_account_guid(conn, ['Income', 'Uncategorized'], 'INCOME')
        # 支出: Expenses:Uncategorized
        expense_account_guid = get_or_create_account_guid(conn, ['Expenses', 'Uncategorized'], 'EXPENSE')
        # カード引き落とし用: パターンごとに負債口座GUIDを事前取得
        card_payment_guids = {
            keyword: get_or_create_account_guid(conn, account_path, 'LIABILITY')
            for keyword, account_path in CARD_PAYMENT_PATTERNS
        }

        conn.commit()

        # --- 既存のFITIDを取得 ---
        cursor.execute("SELECT ofx_fitid FROM transactions WHERE ofx_fitid IS NOT NULL")
        existing_ids = {row[0] for row in cursor.fetchall()}

        new_transactions = 0
        for _, row in df.iterrows():
            if row['ofx_fitid'] in existing_ids:
                continue

            cursor.execute("BEGIN;")
            try:
                tx_guid = uuid.uuid4().hex
                post_date = row['date']
                amount = row['amount']
                description = row['description']

                # 1. transactions テーブルに登録
                cursor.execute("""
                    INSERT INTO transactions (guid, post_date, description, ofx_fitid)
                    VALUES (?, ?, ?, ?)
                """, (tx_guid, post_date, description, row['ofx_fitid']))
                
                # 2. splits テーブルに登録 (仕訳)
                split1_guid = uuid.uuid4().hex
                split2_guid = uuid.uuid4().hex
                
                if amount > 0: # 入金
                    # 入金の相手勘定を分類: 自己資金移動(→Assets:Transfer)・給与・利息は
                    # 適切な勘定へ。未知の摘要は Income:Uncategorized に保留（人間レビュー用）。
                    klass = classify_income(description)
                    if klass is not None:
                        peer_guid = get_or_create_account_guid(
                            conn, list(klass.account_path), klass.account_type
                        )
                    else:
                        peer_guid = income_account_guid
                    # 借方(Debit): 銀行 (資産増) +amount
                    # 貸方(Credit): 相手勘定 -amount
                    
                    # Split 1: Bank (Debit)
                    cursor.execute("""
                        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                        VALUES (?, ?, ?, ?, 1, ?, 1)
                    """, (split1_guid, tx_guid, bank_account_guid, amount, amount))

                    # Split 2: Income (Credit)
                    cursor.execute("""
                        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                        VALUES (?, ?, ?, ?, 1, ?, 1)
                    """, (split2_guid, tx_guid, peer_guid, -amount, -amount))

                else: # 出金 (amount < 0)
                    abs_amount = abs(amount)
                    # カード引き落としなら負債口座へ、それ以外は費用へ
                    debit_account_guid = expense_account_guid
                    for keyword, liability_guid in card_payment_guids.items():
                        if keyword in description:
                            debit_account_guid = liability_guid
                            break

                    # Split 1: Expense or Liability (Debit) -> Positive value
                    cursor.execute("""
                        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                        VALUES (?, ?, ?, ?, 1, ?, 1)
                    """, (split1_guid, tx_guid, debit_account_guid, abs_amount, abs_amount))

                    # Split 2: Bank (Credit) -> Negative value
                    cursor.execute("""
                        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                        VALUES (?, ?, ?, ?, 1, ?, 1)
                    """, (split2_guid, tx_guid, bank_account_guid, amount, amount)) # amount is negative

                cursor.execute("COMMIT;")
                new_transactions += 1
            except sqlite3.Error as e:
                cursor.execute("ROLLBACK;")
                print(f"エラー: DB登録中にエラーが発生しました。スキップします。詳細: {e}")

        if new_transactions > 0:
            print(f"{new_transactions}件の新しい取引データを '{db_path}' にインポートしました。({csv_path.name})")
        else:
            print(f"'{csv_path.name}' に新しい取引データはありませんでした。")

    except sqlite3.Error as e:
        print(f"データベースエラー: {e}")
    except ValueError as e:
        print(f"エラー: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    PROJECT_ROOT = get_project_root()
    
    csv_files = glob.glob(str(PROJECT_ROOT / "data" / "raw" / "rakuten_bank" / "*.csv"))
    
    if not csv_files:
        print("楽天銀行のCSVファイルが見つかりません。")
    else:
        for csv_file in csv_files:
            import_rakuten_bank_csv(Path(csv_file))