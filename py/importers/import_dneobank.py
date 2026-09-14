import pandas as pd

from py.importers import account_identity, raw_archive
from py.importers.account_identity import AccountUndetermined
import sqlite3
import json
from pathlib import Path
import glob
import hashlib
import uuid

from py.importers import ledger
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
    account_key: str = None,
    db_path: str = None,
    raw_db_path: str = None,
):
    """
    住信SBIネット銀行の入出金明細CSVを読み込み、DBに登録します。

    口座は CSV に書かれていないため、`account_identity` で同定する(通貨ヘッダ +
    相手口座名の排除 + 残高連鎖)。判定できなければ `AccountUndetermined` を送出して
    **取込を拒否**する —— 代表口座へ固定で流し込む旧挙動は口座混入の原因だった。

    account_key: 同定を省いて口座を指定する(人の宣言・リプレイ用)。
    account_path / fitid_prefix: 旧シグネチャ互換。account_path を明示すると
        同定を行わずその口座に記帳する(fitid は v1 のまま)。
    """
    raw_conn = raw_archive.open_raw_db(Path(raw_db_path) if raw_db_path else None)
    try:
        return _import_dneobank_csv(csv_path, account_path, fitid_prefix,
                                    account_key, raw_conn, db_path)
    finally:
        raw_conn.close()


def _import_dneobank_csv(csv_path, account_path, fitid_prefix, account_key, raw_conn,
                         db_path=None):
    legacy_mode = account_path is not None and account_key is None
    PROJECT_ROOT = get_project_root()
    if db_path is None:
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
        db_path = PROJECT_ROOT / db_path

    # --- 生CSVの読みと口座同定 ---------------------------------------------
    # 通貨は列名(円/USD)で判別する。旧実装は 残高(円) 固定だったため USD 建て
    # ファイルが全行ゼロに落ち、$75 が ¥75 として代表口座に記帳されていた。
    parsed = account_identity.parse_dneobank(
        account_identity._decode(Path(csv_path).read_bytes()))
    if parsed is None:
        print(f"エラー: '{csv_path.name}' を住信SBIの明細として読めませんでした。")
        return

    reg = account_identity.registry(raw_conn)
    if legacy_mode:
        resolved_path, denom, currency = account_path, 1, 'JPY'
        reason = '呼び出し側の指定(旧シグネチャ)'
    else:
        if account_key is None:
            sha = hashlib.sha256(Path(csv_path).read_bytes()).hexdigest()
            ident = account_identity.identify(raw_conn, parsed, exclude_sha=sha)
            if not ident.decided:
                # 推測して記帳しない。取込拒否(indeterminate)。
                raise AccountUndetermined(ident.reason, ident.candidates)
            account_key = ident.account_key
            reason = ident.reason
            account_identity.declare(raw_conn, sha, account_key,
                                     origin='chain', evidence=reason)
        else:
            reason = '指定'
        if account_key not in reg:
            raise AccountUndetermined(f'未登録の口座キー: {account_key}')
        resolved_path = reg[account_key]['account_path']
        currency = reg[account_key]['currency']
        denom = parsed.denom
        print(f"口座同定: {reg[account_key]['label']} ({account_key}) — 根拠: {reason}")

    # --- 行の組み立て(fitid は v1 互換 + v2 を両方持つ) ---------------------
    # v1 = 旧実装の式(口座を含まない。既存 4,167 件との突合にのみ使う)
    # v2 = 口座キー・通貨・ファイル内連番を含む決定的関数(これから書く値)
    seen = {}
    records = []
    for idx, (date, desc, amt_minor, bal_minor) in enumerate(parsed.rows):
        if amt_minor == 0:
            continue
        key = (date, desc, amt_minor)
        seen[key] = seen.get(key, 0) + 1
        withdrawal_v1 = (-amt_minor if amt_minor < 0 else 0) / denom
        deposit_v1 = (amt_minor if amt_minor > 0 else 0) / denom
        v1_src = (f"{fitid_prefix}:{date}:{desc}:"
                  f"{int(withdrawal_v1) if withdrawal_v1 == int(withdrawal_v1) else withdrawal_v1}:"
                  f"{int(deposit_v1) if deposit_v1 == int(deposit_v1) else deposit_v1}:"
                  f"{int(bal_minor / denom) if (bal_minor / denom) == int(bal_minor / denom) else bal_minor / denom}")
        v2_src = (f"v2:dneobank:{account_key or ':'.join(resolved_path)}:{currency}:"
                  f"{date}:{desc}:{amt_minor}:{bal_minor}:{seen[key]}")
        records.append({
            'date': date, 'description': desc,
            'deposit': amt_minor if amt_minor > 0 else 0,
            'withdrawal': -amt_minor if amt_minor < 0 else 0,
            'balance_minor': bal_minor, 'seq': seen[key],
            'ofx_fitid': 'SHA256:' + hashlib.sha256(v2_src.encode()).hexdigest(),
            'fitid_v1': 'SHA256:' + hashlib.sha256(v1_src.encode()).hexdigest(),
        })
    if legacy_mode:
        # 旧挙動: v1 を書き込む(既存データとの互換)
        for r in records:
            r['ofx_fitid'] = r['fitid_v1']
    df = pd.DataFrame(records)
    if df.empty:
        print(f"'{csv_path.name}' に取込対象の明細行がありませんでした。")
        return
    df.drop_duplicates(subset=['ofx_fitid'], keep='first', inplace=True)
    account_path = resolved_path

    conn = None
    try:
        conn = sqlite3.connect(db_path)
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
        
        currency_guid = get_currency_guid(conn, currency)
        
        conn.commit()

        new_transactions = 0

        # 記帳は py/importers/ledger.py(記帳サービス)が行う。冪等の判定・墓標の参照・
        # 借貸の検算はサービス側。ここで渡す legacy_fitids は、この importer が過去に
        # 書いた 2 つの式(v1 = 口座を含まない旧式 / v2 = 本 importer 独自式)で、
        # 既に入っている行を再取込しないためのもの(hash policy = auto)。
        for _, row in df.iterrows():
            deposit = row['deposit']
            withdrawal = row['withdrawal']
            if deposit == 0 and withdrawal == 0:
                continue

            description = row['description']
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
                # 借方: 資産増加(+) / 貸方: 収益増加(-)
                entries = (
                    ledger.Entry(bank_account_guid, amount, denom, amount, denom),
                    ledger.Entry(peer_guid, -amount, denom, -amount, denom),
                )
                signed = amount
            else:
                amount = withdrawal
                # カード引き落としなら負債口座へ、それ以外は費用へ
                debit_account_guid = expense_account_guid
                for keyword, liability_guid in card_payment_guids.items():
                    if keyword in description:
                        debit_account_guid = liability_guid
                        break
                # 借方: 費用増加 or 負債減少(+) / 貸方: 資産減少(-)
                entries = (
                    ledger.Entry(debit_account_guid, amount, denom, amount, denom),
                    ledger.Entry(bank_account_guid, -amount, denom, -amount, denom),
                )
                signed = -amount

            posting = ledger.Posting(
                source="dneobank",
                account_key=account_key or ":".join(account_path),
                date=row['date'],
                description=description,
                amount=signed / denom,
                balance=row['balance_minor'] / denom,
                seq=row['seq'],
                entries=entries,
                currency_guid=currency_guid,
                legacy_fitids=(row['ofx_fitid'], row['fitid_v1']),
            )
            try:
                if ledger.post(conn, posting) is not None:
                    new_transactions += 1
            except ValueError as e:
                print(f"取込拒否(形が合わない): {e}")
        conn.commit()

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
