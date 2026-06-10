import pandas as pd
import sqlite3
import json
from pathlib import Path
import glob
import hashlib
import re
import unicodedata
import uuid

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

def normalize_card_merchant(description: str) -> str:
    """楽天カードの description を「2フォーマット二重取込」検出用に正規化する。

    同一決済が
      - ブランド明細形式: 「ＶＩＳＡ国内利用　VS <店名>」「ＪＣＢ国内利用　QP  <店名>」
                          「ＶＩＳＡ海外利用　<店名>」
      - 生マーチャント形式: 「<店名>」「<店名>利用国US」
    の 2 種類の CSV で別 description として取り込まれると別 fitid になり重複排除を
    すり抜ける。ブランド接頭辞・「利用国XX」接尾辞・末尾の取込コード数字を除去し、
    全角半角を NFKC で畳んで両形式を同一キーに収束させる。

    ＥＴＣ往復（経路違いで同額同日）のように経路情報が残るものは正規化後も別キーに
    なるため、正当な複数取引を誤って統合しない。
    """
    s = unicodedata.normalize("NFKC", description or "")
    # ブランド明細の接頭辞を除去（NFKC 後は ＶＩＳＡ→VISA, 全角空白→半角）
    s = re.sub(
        r"^(VISA|JCB|MasterCard|Master|AMEX|Diners|ダイナース)\s*(国内|海外)利用\s*(VS|QP)?\s*",
        "",
        s,
    )
    # 生マーチャント形式の「利用国XX」接尾辞を除去
    s = re.sub(r"利用国..$", "", s)
    # 末尾の取込コード（2個以上の空白に続く数字列）を除去
    s = re.sub(r"\s{2,}\d+$", "", s)
    # 残りの空白を畳む
    return re.sub(r"\s+", "", s).strip()


def card_dedup_key(date: str, amount, description: str) -> str:
    """(日付, 金額, 正規化マーチャント) の二重取込検出キー。
    正規化が空になる場合は NFKC 畳みした原文を使い、別決済の誤統合を防ぐ。"""
    norm = normalize_card_merchant(description) or unicodedata.normalize("NFKC", description or "")
    return f"{date}|{int(amount)}|{norm}"


def get_or_create_account_guid(conn: sqlite3.Connection, name_path: list[str], account_type: str) -> str:
    """
    階層パスを指定して勘定科目のGUIDを取得または作成します。
    例: ['Liabilities', 'Credit Card', 'Rakuten Card']
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
            # 勘定科目が存在しない場合は作成（実取引が付く末端のみ実タイプ、中間はプレースホルダ）
            guid = uuid.uuid4().hex
            current_type = account_type if i == len(name_path) - 1 else 'PLACEHOLDER'
            cursor.execute("""
                INSERT INTO accounts (guid, name, account_type, parent_guid)
                VALUES (?, ?, ?, ?)
            """, (guid, name, current_type, parent_guid))
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
    # 通貨が存在しない場合は作成 (例として)
    guid = uuid.uuid4().hex
    fraction = 100 if mnemonic == 'JPY' else 100 # JPYは100でなく0では？->本DBは歴史的経緯により1/100を基準とする
    cursor.execute("INSERT INTO currencies (guid, mnemonic, fraction) VALUES (?, ?, ?)", (guid, mnemonic, fraction))
    print(f"通貨を作成しました: {mnemonic}")
    return guid

def import_rakuten_card_csv(csv_path: Path, db_path: str = None):
    """
    楽天カードの利用明細CSVを読み込み、新しい複式簿記スキーマでDBに登録します。
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
        with open(csv_path, 'r', encoding=encoding) as f:
            lines = f.readlines()
        
        header_index = -1
        for i, line in enumerate(lines):
            if '利用日' in line and '利用店名・商品名' in line:
                header_index = i
                break
        
        if header_index == -1:
            print(f"エラー: '{csv_path.name}' にヘッダー行が見つかりません。")
            return
            
        df = pd.read_csv(csv_path, encoding=encoding, header=header_index)

    except Exception as e:
        print(f"エラー: CSVファイルの読み込みに失敗しました: {e}")
        return

    # 列名の正規化
    df.rename(columns={
        '利用日': 'date',
        '利用店名・商品名': 'description'
    }, inplace=True)

    # 金額カラムを 'amount' に統一する
    if '利用金額' in df.columns:
        df.rename(columns={'利用金額': 'amount'}, inplace=True)
    elif '支払総額' in df.columns:
        df.rename(columns={'支払総額': 'amount'}, inplace=True)

    required_columns = ['date', 'description', 'amount']
    if not all(col in df.columns for col in required_columns):
        print(f"エラー: CSVに必要なカラム {required_columns} が見つかりません。 (利用金額 or 支払総額)")
        return

    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
    df.dropna(subset=['date'], inplace=True)
    
    df['amount'] = pd.to_numeric(df['amount'].astype(str).str.replace(',', ''), errors='coerce').fillna(0).astype(int)
    df = df[['date', 'amount', 'description']]

    def generate_fitid(row):
        return 'SHA256:' + hashlib.sha256(
            f"RAKUTENCARD:{row['date']}:{row['amount']}:{row['description']}".encode()
        ).hexdigest()
    df['ofx_fitid'] = df.apply(generate_fitid, axis=1)
    df['_dedup_key'] = df.apply(
        lambda r: card_dedup_key(r['date'], r['amount'], r['description']), axis=1
    )

    # 完全一致(fitid)と正規化キー(2フォーマット二重取込対策)の両方でファイル内重複排除
    df.drop_duplicates(subset=['ofx_fitid'], keep='first', inplace=True)
    df.drop_duplicates(subset=['_dedup_key'], keep='first', inplace=True)

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # --- 勘定科目と通貨のGUIDを取得 ---
        liability_account_guid = get_or_create_account_guid(conn, ['Liabilities', 'Credit Card', 'Rakuten Card'], 'LIABILITY')
        expense_account_guid = get_or_create_account_guid(conn, ['Expenses', 'Uncategorized'], 'EXPENSE')
        jpy_guid = get_currency_guid(conn, 'JPY')

        # 勘定科目作成のトランザクションをコミット
        conn.commit()

        # --- 既存のFITIDを取得（完全一致用） ---
        cursor.execute("SELECT ofx_fitid FROM transactions WHERE ofx_fitid IS NOT NULL")
        existing_ids = {row[0] for row in cursor.fetchall()}

        # --- 既存の楽天カード取引を正規化キーで取得（2フォーマット二重取込対策） ---
        # description 違いで fitid をすり抜ける同一決済を (日付, 金額, 正規化マーチャント)
        # で検出し二重計上を防ぐ。
        cursor.execute(
            """
            SELECT t.post_date, CAST(-s.value_num*1.0/s.value_denom AS INTEGER), t.description
            FROM splits s JOIN transactions t ON t.guid = s.tx_guid
            WHERE s.account_guid = ?
            """,
            (liability_account_guid,),
        )
        existing_keys = {card_dedup_key(d, amt, desc) for d, amt, desc in cursor.fetchall()}

        new_transactions = 0
        for _, row in df.iterrows():
            if row['ofx_fitid'] in existing_ids or row['_dedup_key'] in existing_keys:
                continue

            conn.execute("BEGIN;")
            try:
                tx_guid = uuid.uuid4().hex
                post_date = row['date']
                amount = int(row['amount'])

                # 1. transactions テーブルに登録
                cursor.execute("""
                    INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
                    VALUES (?, ?, ?, ?, ?)
                """, (tx_guid, post_date, row['description'], row['ofx_fitid'], jpy_guid))
                
                # 2. splits テーブルに登録 (仕訳)
                # 費用(未分類)の増加 / 負債(楽天カード)の増加
                split1_guid = uuid.uuid4().hex
                split2_guid = uuid.uuid4().hex
                
                # 費用 (借方 Debit)
                cursor.execute("""
                    INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                    VALUES (?, ?, ?, ?, 1, ?, 1)
                """, (split1_guid, tx_guid, expense_account_guid, amount, amount))

                # 負債 (貸方 Credit)
                cursor.execute("""
                    INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                    VALUES (?, ?, ?, ?, 1, ?, 1)
                """, (split2_guid, tx_guid, liability_account_guid, -amount, -amount))

                conn.commit()
                existing_ids.add(row['ofx_fitid'])
                existing_keys.add(row['_dedup_key'])
                new_transactions += 1
            except sqlite3.Error as e:
                conn.rollback()
                print(f"エラー: DB登録中にエラーが発生しました。スキップします。詳細: {e}")

        if new_transactions > 0:
            print(f"{new_transactions}件の新しい取引データを '{db_path}' にインポートしました。({csv_path.name})")
        else:
            print(f"'{csv_path.name}' に新しい取引データはありませんでした。")

    except sqlite3.Error as e:
        print(f"データベースエラー: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    PROJECT_ROOT = get_project_root()
    csv_files = glob.glob(str(PROJECT_ROOT / "data" / "raw" / "rakuten_card" / "enavi*.csv"))
    
    if not csv_files:
        print("楽天カードのCSVファイルが見つかりません。")
    else:
        for csv_file in csv_files:
            import_rakuten_card_csv(Path(csv_file))
