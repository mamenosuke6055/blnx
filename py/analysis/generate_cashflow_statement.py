"""
キャッシュフロー計算書 (Cash Flow Statement) の生成

フェーズ5: 財務諸表と資産管理
"""
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FINANCE_DB_PATH = BASE_DIR / "db" / "finance.db"
REPORTS_DIR = BASE_DIR / "data" / "reports"


def get_cashflows(conn, year: int, month: int) -> pd.DataFrame:
    """
    指定月のキャッシュフローを取得する。

    Args:
        conn: SQLite接続
        year: 年
        month: 月

    Returns:
        DataFrame: 勘定科目ごとのキャッシュフロー
    """
    # TODO: splits から現金系勘定 (ASSET:Bank等) の増減を集計
    # TODO: 営業/投資/財務 の3区分に分類
    raise NotImplementedError


def build_cashflow_statement(cashflows: pd.DataFrame) -> dict:
    """
    キャッシュフロー計算書を構成する。

    Returns:
        dict: {
            'operating': DataFrame,   # 営業活動によるCF
            'investing': DataFrame,   # 投資活動によるCF
            'financing': DataFrame,   # 財務活動によるCF
            'net_change': int,        # 現金純増減額
        }
    """
    # TODO: カテゴリ別に営業/投資/財務を割り当て
    raise NotImplementedError


def generate_cashflow_statement(year: int = None, month: int = None):
    """
    キャッシュフロー計算書を生成する。

    Args:
        year: 年。省略時は前月。
        month: 月。省略時は前月。
    """
    if year is None or month is None:
        today = datetime.today().replace(day=1)
        from datetime import timedelta
        prev = today - timedelta(days=1)
        year, month = prev.year, prev.month

    logging.info(f"キャッシュフロー計算書を生成します（{year}-{month:02d}）")

    conn = None
    try:
        conn = sqlite3.connect(FINANCE_DB_PATH)
        cashflows = get_cashflows(conn, year, month)
        statement = build_cashflow_statement(cashflows)
        # TODO: Markdown または HTML で出力
        # TODO: REPORTS_DIR に保存
    finally:
        if conn:
            conn.close()
