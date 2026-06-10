"""りそな銀行（マイゲート）入出金明細CSVインポーター。

全銀協系の21列フォーマット（"レコード区分","年",...,"取引名",...,"金額","取引後残高","摘要",...）。
楽天銀行（import_rakutenbank）と異なる点:
  - 金額は絶対値（符号なし）。入出金種別は『取引名』列（入金/支払）で判定する。
  - レコード区分『合計』行（残高表示）は明細ではないため除外する。
  - 日付は「取扱日付」の 年/月/日 3列を結合する。

解析（parse_resona_bank_csv）はDB非依存でテスタブルにし、DB登録（import_resona_bank_csv）から分離する。
"""

import csv
import glob
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from py.processing.classify_bank_income import classify_income

# 全銀協系フォーマットの列インデックス
COL_RECORD_TYPE = 0   # レコード区分（明細/合計）
COL_TXN_NAME = 13     # 取引名（入金/支払/残高）
COL_DATE_Y = 14       # 取扱日付 年
COL_DATE_M = 15       # 取扱日付 月
COL_DATE_D = 16       # 取扱日付 日
COL_AMOUNT = 17       # 金額（絶対値）
COL_BALANCE = 18      # 取引後残高
COL_TEKIYO = 19       # 摘要
MIN_COLS = 21

ENCODING = "cp932"


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _to_int(s: str) -> int:
    """カンマ・空白を除去して整数化。空文字は0。"""
    s = (s or "").replace(",", "").strip()
    return int(float(s)) if s else 0


def parse_resona_bank_csv(csv_path: Path) -> list[dict]:
    """りそな銀行CSVを解析して取引リストを返す（DB非依存・テスタブル）。

    レコード区分『明細』かつ取引名が入金/支払の行のみを対象とする。
    金額は絶対値、入出金種別は ``txn_name`` で表す。
    """
    transactions = []
    with open(csv_path, "r", encoding=ENCODING) as f:
        reader = csv.reader(f)
        next(reader, None)  # ヘッダ行
        for row in reader:
            if len(row) < MIN_COLS:
                continue
            if row[COL_RECORD_TYPE].strip() != "明細":
                continue
            txn_name = row[COL_TXN_NAME].strip()
            if txn_name not in ("入金", "支払"):
                continue
            y = row[COL_DATE_Y].strip()
            m = row[COL_DATE_M].strip()
            d = row[COL_DATE_D].strip()
            if not (y and m and d):
                continue
            transactions.append({
                "date": f"{y}-{int(m):02d}-{int(d):02d}",
                "amount": abs(_to_int(row[COL_AMOUNT])),
                "txn_name": txn_name,
                "description": row[COL_TEKIYO].strip(),
                "balance": row[COL_BALANCE].strip(),
            })
    return transactions


def _make_fitid(t: dict) -> str:
    """取引後残高まで含めて一意性を確保（同日同額同摘要でも残高で区別）。"""
    raw = f"RESONABANK:{t['date']}:{t['amount']}:{t['txn_name']}:{t['description']}:{t['balance']}"
    return "SHA256:" + hashlib.sha256(raw.encode()).hexdigest()


def get_or_create_account_guid(conn: sqlite3.Connection, name_path: list[str], account_type: str) -> str:
    """階層パスを指定して勘定科目GUIDを取得または作成する（例: ['Assets','Bank','Resona Bank']）。"""
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
            guid = uuid.uuid4().hex
            is_placeholder = 1 if i < len(name_path) - 1 else 0
            cursor.execute(
                "INSERT INTO accounts (guid, name, account_type, parent_guid, placeholder) "
                "VALUES (?, ?, ?, ?, ?)",
                (guid, name, account_type, parent_guid, is_placeholder),
            )
            print(f"勘定科目を作成しました: {' > '.join(current_path)}")
        parent_guid = guid
    return parent_guid


def import_resona_bank_csv(csv_path, db_path: str = None):
    """りそな銀行CSVを複式簿記スキーマでDBに登録する。ofx_fitid で冪等。"""
    csv_path = Path(csv_path)
    PROJECT_ROOT = get_project_root()

    if db_path is None:
        CONFIG_FILE = PROJECT_ROOT / "config/settings.json"
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
            db_path = PROJECT_ROOT / settings.get("db_path", "db/finance.db")
        except FileNotFoundError:
            print(f"エラー: 設定ファイル '{CONFIG_FILE}' が見つかりません。デフォルトパス 'db/finance.db' を使用します。")
            db_path = PROJECT_ROOT / "db/finance.db"

    transactions = parse_resona_bank_csv(csv_path)
    if not transactions:
        print(f"'{csv_path.name}' に有効な明細がありませんでした。")
        return

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        bank_guid = get_or_create_account_guid(conn, ["Assets", "Bank", "Resona Bank"], "ASSET")
        income_guid = get_or_create_account_guid(conn, ["Income", "Uncategorized"], "INCOME")
        expense_guid = get_or_create_account_guid(conn, ["Expenses", "Uncategorized"], "EXPENSE")
        conn.commit()

        cursor.execute("SELECT ofx_fitid FROM transactions WHERE ofx_fitid IS NOT NULL")
        existing_ids = {r[0] for r in cursor.fetchall()}

        seen = set()  # ファイル間・ファイル内の重複を排除
        new_transactions = 0
        for t in transactions:
            fitid = _make_fitid(t)
            if fitid in existing_ids or fitid in seen:
                continue
            seen.add(fitid)

            cursor.execute("BEGIN;")
            try:
                tx_guid = uuid.uuid4().hex
                amount = t["amount"]
                cursor.execute(
                    "INSERT INTO transactions (guid, post_date, description, ofx_fitid) VALUES (?, ?, ?, ?)",
                    (tx_guid, t["date"], t["description"], fitid),
                )
                s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
                if t["txn_name"] == "入金":
                    # 入金の相手勘定を分類: 自己資金移動(→Assets:Transfer)・給与・利息は
                    # 適切な勘定へ。未知の摘要は Income:Uncategorized に保留（人間レビュー用）。
                    klass = classify_income(t["description"])
                    if klass is not None:
                        peer_guid = get_or_create_account_guid(
                            conn, list(klass.account_path), klass.account_type
                        )
                    else:
                        peer_guid = income_guid
                    # 借方: 銀行(資産増 +) / 貸方: 相手勘定(-)
                    cursor.execute(
                        "INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom) VALUES (?, ?, ?, ?, 1, ?, 1)",
                        (s1, tx_guid, bank_guid, amount, amount),
                    )
                    cursor.execute(
                        "INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom) VALUES (?, ?, ?, ?, 1, ?, 1)",
                        (s2, tx_guid, peer_guid, -amount, -amount),
                    )
                else:  # 支払
                    # 借方: 費用(+) / 貸方: 銀行(資産減 -)
                    cursor.execute(
                        "INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom) VALUES (?, ?, ?, ?, 1, ?, 1)",
                        (s1, tx_guid, expense_guid, amount, amount),
                    )
                    cursor.execute(
                        "INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom) VALUES (?, ?, ?, ?, 1, ?, 1)",
                        (s2, tx_guid, bank_guid, -amount, -amount),
                    )
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
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    PROJECT_ROOT = get_project_root()
    csv_files = glob.glob(str(PROJECT_ROOT / "data" / "raw" / "resona_bank" / "*.csv"))
    if not csv_files:
        print("りそな銀行のCSVファイルが見つかりません。")
    else:
        for csv_file in csv_files:
            import_resona_bank_csv(Path(csv_file))
