import pandas as pd
import sqlite3
import json
from pathlib import Path
import hashlib
import uuid
import glob

from py.importers import ledger
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
    
    # 残高欄があれば自然キーに使う(同日・同額・同摘要の正当な 2 件を分けるため)
    balance_col = next((c for c in df.columns if '残高' in str(c)), None)
    if balance_col:
        df['balance'] = (df[balance_col].astype(str).str.replace(',', '')
                         .str.extract(r'(-?\d+)', expand=False).astype('Int64'))
    else:
        df['balance'] = None

    df = df[['date', 'amount', 'description', 'balance']]

    df['description'] = df['description'].str.strip()

    def legacy_fitid(row):
        # v1(旧実装の式。残高を含まないため同日同額同摘要が潰れた)。既存行との突合にのみ使う
        return 'SHA256:' + hashlib.sha256(
            f"RAKUTENBANK:{row['date']}:{row['amount']}:{row['description']}".encode()
        ).hexdigest()

    conn = None
    try:
        conn = sqlite3.connect(db_path)

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

        seq = ledger.SeqCounter()
        new_transactions = 0
        for _, row in df.iterrows():
            amount = int(row['amount'])
            description = row['description']

            if amount > 0:   # 入金
                # 入金の相手勘定を分類: 自己資金移動(→Assets:Transfer)・給与・利息は
                # 適切な勘定へ。未知の摘要は Income:Uncategorized に保留（人間レビュー用）。
                klass = classify_income(description)
                if klass is not None:
                    peer_guid = get_or_create_account_guid(
                        conn, list(klass.account_path), klass.account_type
                    )
                else:
                    peer_guid = income_account_guid
                entries = (
                    ledger.Entry(bank_account_guid, amount, quantity_num=amount, quantity_denom=1),
                    ledger.Entry(peer_guid, -amount, quantity_num=-amount, quantity_denom=1),
                )
            else:            # 出金
                # カード引き落としなら負債口座へ、それ以外は費用へ
                debit_account_guid = expense_account_guid
                for keyword, liability_guid in card_payment_guids.items():
                    if keyword in description:
                        debit_account_guid = liability_guid
                        break
                abs_amount = abs(amount)
                entries = (
                    ledger.Entry(debit_account_guid, abs_amount,
                                 quantity_num=abs_amount, quantity_denom=1),
                    ledger.Entry(bank_account_guid, amount, quantity_num=amount, quantity_denom=1),
                )

            balance = None if row['balance'] is None or pd.isna(row['balance']) else int(row['balance'])
            posting = ledger.Posting(
                source="rakuten_bank",
                account_key="main",
                date=row['date'],
                description=description,
                amount=amount,
                balance=balance,
                seq=seq.next(row['date'], description, amount),
                entries=entries,
                legacy_fitids=(legacy_fitid(row),),
            )
            try:
                if ledger.post(conn, posting) is not None:
                    new_transactions += 1
            except ValueError as e:
                print(f"取込拒否(形が合わない): {e}")
        conn.commit()

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