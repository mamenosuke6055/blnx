import sqlite3
import pandas as pd
from pathlib import Path
import uuid

# --- 定数とパス設定 ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FINANCE_DB = PROJECT_ROOT / "db" / "finance.db"

def get_db_connection():
    """データベースへの接続を返す"""
    return sqlite3.connect(FINANCE_DB)

def get_root_accounts() -> list[dict]:
    """ルート勘定科目（parent_guid IS NULL）を返す。"""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT name, account_type FROM accounts WHERE parent_guid IS NULL ORDER BY account_type"
    ).fetchall()
    conn.close()
    return [{"name": r[0], "account_type": r[1]} for r in rows]

def get_uncategorized_transactions(filter_pattern='%'):
    """未分類の取引を取得する。同一摘要の合計金額が大きい順に並べる。"""
    conn = get_db_connection()
    query = """
    SELECT
        t.guid,
        t.post_date,
        t.description,
        ROUND(s.value_num * 1.0 / s.value_denom) AS amount,
        SUM(ROUND(s.value_num * 1.0 / s.value_denom))
            OVER (PARTITION BY t.description) AS description_total,
        COUNT(*) OVER (PARTITION BY t.description) AS description_count
    FROM transactions t
    JOIN splits s ON t.guid = s.tx_guid
    JOIN accounts a ON s.account_guid = a.guid
    WHERE t.manual_category_guid IS NULL
      AND t.description IS NOT NULL
      AND a.account_type NOT IN ('ASSET', 'LIABILITY', 'EQUITY')
      AND s.value_num > 0
      AND t.post_date LIKE ?
    ORDER BY description_total DESC, t.description ASC, t.post_date DESC
    """
    df = pd.read_sql_query(query, conn, params=[filter_pattern])
    conn.close()
    return df

def get_categories():
    """勘定科目リストを取得する"""
    conn = get_db_connection()
    query = """
    SELECT 
        a_sub.guid, 
        a_main.name as main_category, 
        a_sub.name as sub_category,
        a_sub.account_type
    FROM accounts a_sub
    JOIN accounts a_main ON a_sub.parent_guid = a_main.guid
    WHERE (a_sub.account_type IN ('EXPENSE', 'INCOME') OR a_sub.name = 'Transfer')
      AND a_sub.placeholder = 0 
    ORDER BY a_main.name, a_sub.name
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

def assign_manual_category(transaction_guid: str, category_guid: str):
    """手動カテゴリ割り当てを実行する"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE transactions SET manual_category_guid = ? WHERE guid = ?",
            (category_guid, transaction_guid)
        )
        cursor.execute("""
            UPDATE splits 
            SET account_guid = ? 
            WHERE tx_guid = ? 
              AND account_guid IN (
                  SELECT guid FROM accounts 
                  WHERE account_type IN ('EXPENSE', 'INCOME')
              )
        """, (category_guid, transaction_guid))
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()

def assign_category_to_all_matching_descriptions(description: str, category_guid: str):
    """同じ摘要を持つすべての未分類取引にカテゴリを割り当てる"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT guid FROM transactions WHERE description = ? AND manual_category_guid IS NULL",
            (description,)
        )
        tx_guids = [row[0] for row in cursor.fetchall()]
        if not tx_guids:
            return 0

        cursor.execute(
            "UPDATE transactions SET manual_category_guid = ? WHERE description = ? AND manual_category_guid IS NULL",
            (category_guid, description)
        )
        placeholders = ', '.join(['?'] * len(tx_guids))
        cursor.execute(f"""
            UPDATE splits 
            SET account_guid = ? 
            WHERE tx_guid IN ({placeholders})
              AND account_guid IN (
                  SELECT guid FROM accounts 
                  WHERE account_type IN ('EXPENSE', 'INCOME')
              )
        """, [category_guid] + tx_guids)
        conn.commit()
        return len(tx_guids)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()

def confirm_ai_categories(tx_guids: list[str] | None = None) -> int:
    """
    AI予測済み・未確定 (ai_confirmed_at IS NULL) の取引を確定する。
    splits.account_guid を ai_category_guid で上書きし、ai_confirmed_at に現在時刻を記録する。
    tx_guids が None なら全件対象、指定すれば対象 guid のみ。
    戻り値: 確定した件数
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if tx_guids is None:
            cursor.execute("""
                SELECT guid FROM transactions
                WHERE ai_category_guid IS NOT NULL AND ai_confirmed_at IS NULL
            """)
        else:
            placeholders = ', '.join(['?'] * len(tx_guids))
            cursor.execute(f"""
                SELECT guid FROM transactions
                WHERE ai_category_guid IS NOT NULL AND ai_confirmed_at IS NULL
                  AND guid IN ({placeholders})
            """, tx_guids)

        targets = [row[0] for row in cursor.fetchall()]
        if not targets:
            return 0

        ph = ', '.join(['?'] * len(targets))
        cursor.execute(f"""
            UPDATE splits
            SET account_guid = (
                SELECT ai_category_guid FROM transactions WHERE guid = splits.tx_guid
            )
            WHERE tx_guid IN ({ph})
              AND account_guid IN (
                  SELECT guid FROM accounts WHERE account_type IN ('EXPENSE', 'INCOME')
              )
        """, targets)
        cursor.execute(f"""
            UPDATE transactions
            SET ai_confirmed_at = datetime('now')
            WHERE guid IN ({ph})
        """, targets)
        conn.commit()
        return len(targets)
    except sqlite3.Error:
        conn.rollback()
        return 0
    finally:
        conn.close()


def get_category_parents(account_type: str = "EXPENSE") -> list[dict]:
    """指定 account_type のルート直下にある親カテゴリ一覧を返す（例: 食費, 通信費, 趣味・娯楽）。

    この下に子カテゴリ（例: ライブ）を作成する。
    """
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT a.guid, a.name
        FROM accounts a
        JOIN accounts r ON a.parent_guid = r.guid
        WHERE r.parent_guid IS NULL
          AND a.account_type = ?
          AND a.placeholder = 1
        ORDER BY a.name
    """, (account_type,)).fetchall()
    conn.close()
    return [{"guid": guid, "name": name} for guid, name in rows]


def create_category_under(parent_guid: str, sub_name: str, account_type: str | None = None) -> bool:
    """指定した親 GUID の配下に新しい勘定科目を作成する"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT account_type FROM accounts WHERE guid = ?", (parent_guid,))
        res = cursor.fetchone()
        if not res:
            return False
        if account_type is None:
            account_type = res[0]
        cursor.execute("SELECT guid FROM accounts WHERE name = ? AND parent_guid = ?", (sub_name, parent_guid))
        if cursor.fetchone():
            return False
        new_guid = uuid.uuid4().hex
        cursor.execute(
            "INSERT INTO accounts (guid, name, account_type, parent_guid, placeholder) VALUES (?, ?, ?, ?, 0)",
            (new_guid, sub_name, account_type, parent_guid)
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def create_new_category(parent_name: str, sub_name: str, account_type: str | None = None):
    """新しい勘定科目（カテゴリ）を作成する。account_type 省略時は親から継承。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT guid, account_type FROM accounts WHERE name = ? AND parent_guid IS NULL",
            (parent_name,)
        )
        res = cursor.fetchone()
        if not res: return False
        root_guid, root_type = res
        if account_type is None:
            account_type = root_type

        # 同名の子勘定が既にないか確認
        cursor.execute("SELECT guid FROM accounts WHERE name = ? AND parent_guid = ?", (sub_name, root_guid))
        if cursor.fetchone(): return False

        new_guid = uuid.uuid4().hex
        cursor.execute("""
            INSERT INTO accounts (guid, name, account_type, parent_guid, placeholder)
            VALUES (?, ?, ?, ?, 0)
        """, (new_guid, sub_name, account_type, root_guid))
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()
