import pandas as pd
import sqlite3
import json
from pathlib import Path
import glob
import hashlib
import uuid

from py.processing.classify_bank_income import classify_income

# 口座振替でカード負債を返済するパターン。費用ではなく負債の減少として仕訳する。
# (マッチキーワード, 負債口座パス)
CARD_PAYMENT_PATTERNS = [
    ('楽天カードサービス', ['Liabilities', 'Credit Card', 'Rakuten Card']),
]

def get_project_root() -> Path:
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

def get_or_create_account_guid(conn: sqlite3.Connection, name_path: list[str], account_type: str, ofx_type: str = None) -> str:
    """
    階層パスを指定して勘定科目のGUIDを取得または作成します。
    例: ['Assets', 'Bank', 'SBI Sumishin Net Bank']
    """
    cursor = conn.cursor()
    parent_guid = None
    
    for i, name in enumerate(name_path):
        cursor.execute("""
            SELECT guid FROM accounts WHERE name = ? AND parent_guid IS ?
        """, (name, parent_guid) if parent_guid else (name, None))
        
        result = cursor.fetchone()
        
        if result:
            guid = result[0]
        else:
            # 勘定科目が存在しない場合は作成
            guid = uuid.uuid4().hex
            # 最後の要素のみ指定のタイプ、それ以外はPLACEHOLDER (またはROOTならASSETなどだが簡易的に)
            # 厳密には上位階層も適切なタイプを持つべきだが、ここでは簡易化
            current_type = account_type if i == len(name_path) - 1 else 'ASSET' # Default to ASSET for parents
            if i == 0 and name == 'Expenses': current_type = 'EXPENSE'
            if i == 0 and name == 'Income': current_type = 'INCOME'
            
            # 親フォルダ的なものは PLACEHOLDER=1 にしても良いが、スキーマ上タイプが必要
            
            # 最後のノードだけ ofx_type を設定
            current_ofx_type = ofx_type if i == len(name_path) - 1 else None

            cursor.execute("""
                INSERT INTO accounts (guid, name, account_type, ofx_type, parent_guid)
                VALUES (?, ?, ?, ?, ?)
            """, (guid, name, current_type, current_ofx_type, parent_guid))
            print(f"勘定科目を作成しました: {' > '.join(name_path[:i+1])}")
        
        parent_guid = guid
        
    return parent_guid

def get_currency_guid(conn: sqlite3.Connection, mnemonic: str) -> str:
    """通貨のGUIDを取得します。"""
    cursor = conn.cursor()
    cursor.execute("SELECT guid FROM currencies WHERE mnemonic = ?", (mnemonic,))
    result = cursor.fetchone()
    if result:
        return result[0]
    
    guid = uuid.uuid4().hex
    fraction = 100 
    cursor.execute("INSERT INTO currencies (guid, mnemonic, fraction) VALUES (?, ?, ?)", (guid, mnemonic, fraction))
    print(f"通貨を作成しました: {mnemonic}")
    return guid

def parse_amount(value):
    """カンマ付き文字列などを数値に変換"""
    if pd.isna(value) or value == '':
        return 0
    if isinstance(value, str):
        return int(value.replace(',', ''))
    return int(value)

def import_dneobank_csv(
    csv_path: Path,
    account_path: list[str] = None,
    fitid_prefix: str = 'DNEOBANK',
):
    """
    住信SBIネット銀行の入出金明細CSVを読み込み、DBに登録します。

    account_path: 銀行口座の階層パス。デフォルトは代表口座
        (['Assets', 'Bank', 'SBI Sumishin Net Bank'])。
        ハイブリ預金や目的別口座を取り込む場合は呼び出し側で指定する。
    fitid_prefix: ofx_fitid 生成時のプレフィックス。account_path と組で口座ごとに
        ユニークにし、別口座間で同一明細が衝突しないようにする。
    """
    if account_path is None:
        account_path = ['Assets', 'Bank', 'SBI Sumishin Net Bank']
    PROJECT_ROOT = get_project_root()
    CONFIG_FILE = PROJECT_ROOT / "config/settings.json"
    
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        db_path = settings.get("db_path")
        if not db_path:
            print(f"エラー: db_pathが設定ファイルに見つかりません。")
            return
    except FileNotFoundError:
        print(f"エラー: 設定ファイル '{CONFIG_FILE}' が見つかりません。")
        return

    encoding = guess_encoding(csv_path)
    if not encoding:
        print(f"エラー: '{csv_path.name}' の文字コードを特定できませんでした。")
        return
    print(f"'{csv_path.name}' の文字コードは {encoding} と推測されました。")

    try:
        # 住信SBIネット銀行のCSVはヘッダーが1行目にある前提
        # カラム: 日付, 内容, 出金金額(円), 入金金額(円), 残高(円), メモ
        df = pd.read_csv(csv_path, encoding=encoding)
        
        # カラム名のマッピング確認
        # 実際のカラム名に合わせて調整
        column_map = {}
        for col in df.columns:
            if '日付' in col: column_map[col] = 'date'
            elif '内容' in col: column_map[col] = 'description'
            elif '出金金額' in col: column_map[col] = 'withdrawal'
            elif '入金金額' in col: column_map[col] = 'deposit'
            elif '残高' in col: column_map[col] = 'balance'
            elif 'メモ' in col: column_map[col] = 'memo'
        
        df.rename(columns=column_map, inplace=True)
        
        required_cols = ['date', 'description', 'withdrawal', 'deposit']
        if not all(col in df.columns for col in required_cols):
            print(f"エラー: 必要なカラムが見つかりません。検出されたカラム: {df.columns.tolist()}")
            return

    except Exception as e:
        print(f"エラー: CSVファイルの読み込みに失敗しました: {e}")
        return

    # 日付変換
    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
    df.dropna(subset=['date'], inplace=True)
    
    # 金額処理
    df['withdrawal'] = df['withdrawal'].apply(parse_amount)
    df['deposit'] = df['deposit'].apply(parse_amount)
    if 'balance' in df.columns:
        df['balance'] = df['balance'].apply(parse_amount)
    else:
        df['balance'] = 0

    # 1行ごとの処理用データ作成
    # deposit > 0 なら入金、withdrawal > 0 なら出金
    
    def generate_fitid(row):
        # ユニークID生成
        # 同じ日付、同じ内容、同じ金額の場合でも区別したいが、CSVに行番号がないため
        # balanceを含めることでユニーク性を高める
        raw_str = f"{fitid_prefix}:{row['date']}:{row['description']}:{row['withdrawal']}:{row['deposit']}:{row['balance']}"
        return 'SHA256:' + hashlib.sha256(raw_str.encode()).hexdigest()

    df['ofx_fitid'] = df.apply(generate_fitid, axis=1)
    
    # 重複除外 (CSV内)
    df.drop_duplicates(subset=['ofx_fitid'], keep='first', inplace=True)

    conn = None
    try:
        conn = sqlite3.connect(PROJECT_ROOT / db_path)
        cursor = conn.cursor()
        
        # --- 勘定科目設定 ---
        bank_account_guid = get_or_create_account_guid(
            conn, account_path, 'ASSET', 'BANK'
        )
        expense_account_guid = get_or_create_account_guid(
            conn, ['Expenses', 'Uncategorized'], 'EXPENSE'
        )
        income_account_guid = get_or_create_account_guid(
            conn, ['Income', 'Uncategorized'], 'INCOME'
        )
        # カード引き落とし用: パターンごとに負債口座GUIDを事前取得
        card_payment_guids = {
            keyword: get_or_create_account_guid(conn, account_path, 'LIABILITY')
            for keyword, account_path in CARD_PAYMENT_PATTERNS
        }
        
        jpy_guid = get_currency_guid(conn, 'JPY')
        
        conn.commit()

        # --- 既存チェック ---
        cursor.execute("SELECT ofx_fitid FROM transactions WHERE ofx_fitid IS NOT NULL")
        existing_ids = {row[0] for row in cursor.fetchall()}
        # 過去に統合・削除された fitid も復活防止のためチェック対象に含める
        # (詳細: docs/Note_Technical_Decisions.md 2026-05-16 項)
        try:
            cursor.execute("SELECT ofx_fitid FROM merged_ofx_fitids")
            existing_ids.update(row[0] for row in cursor.fetchall())
        except sqlite3.OperationalError:
            pass  # merged_ofx_fitids table 未作成環境への defensive

        new_transactions = 0
        
        # 古い順に並んでいる可能性もあるが、CSVの順序通り処理
        for _, row in df.iterrows():
            if row['ofx_fitid'] in existing_ids:
                continue
                
            deposit = row['deposit']
            withdrawal = row['withdrawal']
            
            if deposit == 0 and withdrawal == 0:
                continue

            conn.execute("BEGIN;")
            try:
                tx_guid = uuid.uuid4().hex
                post_date = row['date']
                description = row['description']
                
                # トランザクション登録
                cursor.execute("""
                    INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
                    VALUES (?, ?, ?, ?, ?)
                """, (tx_guid, post_date, description, row['ofx_fitid'], jpy_guid))
                
                # Split登録
                # 金額は 100倍とかせず、そのまま (Integer/Rational管理なら分母1でOK)
                # スキーマ: value_num/denom (通貨価値), quantity_num/denom (数量)
                # JPYは通常 quantity=value
                
                split1_guid = uuid.uuid4().hex
                split2_guid = uuid.uuid4().hex
                
                if deposit > 0:
                    amount = deposit
                    # 入金の相手勘定を分類: 自己資金移動(→Assets:Transfer)・給与・利息は
                    # 適切な勘定へ。未知の摘要は Income:Uncategorized に保留（人間レビュー用）。
                    klass = classify_income(description)
                    if klass is not None:
                        peer_guid = get_or_create_account_guid(
                            conn, list(klass.account_path), klass.account_type
                        )
                    else:
                        peer_guid = income_account_guid
                    # 借方 (Debit): 資産増加 (+amount)
                    cursor.execute("""
                        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                        VALUES (?, ?, ?, ?, 1, ?, 1)
                    """, (split1_guid, tx_guid, bank_account_guid, amount, amount))
                    
                    # 貸方 (Credit): 収益増加 (-amount) ※本スキーマでは収益・負債・資本の増加はマイナス表記
                    cursor.execute("""
                        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                        VALUES (?, ?, ?, ?, 1, ?, 1)
                    """, (split2_guid, tx_guid, peer_guid, -amount, -amount))
                    
                else: # withdrawal > 0
                    amount = withdrawal
                    # カード引き落としなら負債口座へ、それ以外は費用へ
                    debit_account_guid = expense_account_guid
                    for keyword, liability_guid in card_payment_guids.items():
                        if keyword in description:
                            debit_account_guid = liability_guid
                            break
                    # 借方: 費用増加 or 負債減少 (+amount)
                    cursor.execute("""
                        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                        VALUES (?, ?, ?, ?, 1, ?, 1)
                    """, (split1_guid, tx_guid, debit_account_guid, amount, amount))
                    # 貸方: 資産減少 (-amount)
                    cursor.execute("""
                        INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                        VALUES (?, ?, ?, ?, 1, ?, 1)
                    """, (split2_guid, tx_guid, bank_account_guid, -amount, -amount))

                conn.commit()
                new_transactions += 1
                
            except sqlite3.Error as e:
                conn.rollback()
                print(f"エラー: 行の処理中にDBエラー: {e}")

        if new_transactions > 0:
            print(f"{new_transactions}件の新しい取引データをインポートしました。({csv_path.name})")
        else:
            print(f"'{csv_path.name}' に新しい取引データはありませんでした。")

    except sqlite3.Error as e:
        print(f"データベースエラー: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    PROJECT_ROOT = get_project_root()
    # ターゲットディレクトリ: data/raw/dneobank/
    target_dir = PROJECT_ROOT / "data" / "raw" / "dneobank"
    csv_files = glob.glob(str(target_dir / "*.csv"))
    
    if not csv_files:
        print(f"ディレクトリ '{target_dir}' にCSVファイルが見つかりません。")
    else:
        for csv_file in csv_files:
            import_dneobank_csv(Path(csv_file))
