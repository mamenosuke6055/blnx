"""
貸借対照表 (Balance Sheet) スナップショット

blnx は sign 付き整数表現 (audit §1) を採用:
- ASSET: value_num > 0 が借方 = 残高あり
- LIABILITY / EQUITY: value_num < 0 が貸方 = 残高あり

期末閉鎖 (損益勘定振替、audit §4 K) は close_fiscal_year.py スクリプトで実施。
閉鎖済み年度の INCOME / EXPENSE は仕訳残高ゼロ、累積 RE は Retained Earnings
EQUITY 口座に計上される。未閉鎖の当期分は「当期 RE = -INCOME - EXPENSE」で
オンザフライ計算し、閉鎖済み RE (EQUITY 口座) と合算して pure資産合計とする。

円建て前提。USD cents (denom=100) の splits は集計から除外する。
placeholder アカウントは表示から除外（残高があれば異常扱い）。
"""
import sqlite3
from datetime import date
from pathlib import Path


def _compute_account_balances(
    conn: sqlite3.Connection,
    account_type: str,
    at: date | None,
) -> list[dict]:
    """
    指定 account_type のアカウントごとに残高を集計する (denom=1 のみ)。
    at が指定されていれば post_date <= at に絞る。
    placeholder=1 や残高 0 は除外する。
    Returns: [{'guid', 'name', 'balance' (sign付き int)}, ...] balance 降順
    """
    params: list = [account_type]
    where_date = ""
    if at is not None:
        where_date = "AND (t.post_date IS NULL OR t.post_date <= ?)"
        params.append(at.isoformat())

    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT a.guid, a.name,
               COALESCE(SUM(CASE WHEN s.value_denom = 1 THEN s.value_num ELSE 0 END), 0) AS balance
        FROM accounts a
        LEFT JOIN splits s ON s.account_guid = a.guid
        LEFT JOIN transactions t ON t.guid = s.tx_guid
        WHERE a.account_type = ?
          AND COALESCE(a.placeholder, 0) = 0
          {where_date}
        GROUP BY a.guid
        HAVING balance != 0
        ORDER BY balance DESC
    """, params)

    return [
        {'guid': guid, 'name': name, 'balance': int(balance)}
        for guid, name, balance in cursor.fetchall()
    ]


def compute_balance_sheet(
    conn: sqlite3.Connection,
    at: date | None = None,
) -> dict:
    """
    指定基準日 (at, デフォルト今日) 時点の BS を計算する。

    sign 規約: ASSET/EXPENSE は正値 = 借方残高、LIABILITY/EQUITY/INCOME は負値 = 貸方残高。
    出力では LIABILITY / EQUITY / RE を符号反転して「正値 = 残高あり」で表示する。

    Returns: {
        'as_of': ISO 日付,
        'assets': [{guid, name, balance(正値=資産あり)}, ...],
        'liabilities': [{guid, name, balance(正値=借金あり)}, ...],
        'equity_accounts': [{guid, name, balance(正値=純資産あり)}, ...],
        'retained_earnings': int (正値=利益剰余、負値=損失剰余),
        'total_assets': int,
        'total_liabilities': int,
        'total_equity_accounts': int,
        'total_equity_with_re': int,
        'balanced': bool,
        'diff': int,  # total_assets - (total_liabilities + total_equity_with_re)
    }
    """
    if at is None:
        at = date.today()

    raw_assets = _compute_account_balances(conn, 'ASSET', at)
    raw_liabilities = _compute_account_balances(conn, 'LIABILITY', at)
    raw_equity = _compute_account_balances(conn, 'EQUITY', at)
    raw_income = _compute_account_balances(conn, 'INCOME', at)
    raw_expense = _compute_account_balances(conn, 'EXPENSE', at)

    assets = raw_assets
    # LIABILITY / EQUITY は規約上 value_num が負値で「残高あり」を表す。
    # 表示用に符号反転して「正値 = 残高あり」にする。
    liabilities = [
        {'guid': r['guid'], 'name': r['name'], 'balance': -r['balance']}
        for r in raw_liabilities
    ]
    equity_accounts = [
        {'guid': r['guid'], 'name': r['name'], 'balance': -r['balance']}
        for r in raw_equity
    ]

    # 当期（未閉鎖期間）の RE: INCOME (-) と EXPENSE (+) なので RE = -INCOME - EXPENSE。
    # 閉鎖済み年度は INCOME/EXPENSE が仕訳上ゼロになり、Retained Earnings EQUITY 口座に
    # 計上済み（equity_accounts に含まれる）。合算することで全期間の純資産を得る。
    total_income = sum(r['balance'] for r in raw_income)
    total_expense = sum(r['balance'] for r in raw_expense)
    retained_earnings = -total_income - total_expense

    total_assets = sum(r['balance'] for r in assets)
    total_liabilities = sum(r['balance'] for r in liabilities)
    total_equity_accounts = sum(r['balance'] for r in equity_accounts)
    total_equity_with_re = total_equity_accounts + retained_earnings

    diff = total_assets - (total_liabilities + total_equity_with_re)

    return {
        'as_of': at.isoformat(),
        'assets': assets,
        'liabilities': liabilities,
        'equity_accounts': equity_accounts,
        'retained_earnings': retained_earnings,
        'total_assets': total_assets,
        'total_liabilities': total_liabilities,
        'total_equity_accounts': total_equity_accounts,
        'total_equity_with_re': total_equity_with_re,
        'balanced': diff == 0,
        'diff': diff,
    }


def generate_balance_sheet(
    db_path: Path | None = None,
    at: date | None = None,
) -> dict:
    """ファイル DB を開いて BS を返す薄いラッパ。"""
    if db_path is None:
        from py.util.db import get_db_path
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        return compute_balance_sheet(conn, at=at)
    finally:
        conn.close()
