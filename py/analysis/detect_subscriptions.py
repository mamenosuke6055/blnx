"""
固定費・サブスクリプションの自動検出

フェーズ5: 財務諸表と資産管理
"""
import sqlite3
import pandas as pd
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FINANCE_DB_PATH = BASE_DIR / "db" / "finance.db"


def find_recurring_transactions(conn, min_occurrences: int = 3) -> pd.DataFrame:
    """
    周期的な取引を検出する。

    Args:
        conn: SQLite接続
        min_occurrences: 最低出現回数

    Returns:
        DataFrame: description, frequency, avg_amount, last_date, count
    """
    # TODO: description の類似度でグルーピング
    # TODO: 出現間隔から周期を推定 (月次、年次 等)
    # TODO: min_occurrences 以上のものを返却
    raise NotImplementedError


def classify_fixed_variable(subscriptions: pd.DataFrame) -> pd.DataFrame:
    """
    固定費と変動費に分類する。

    Args:
        subscriptions: find_recurring_transactions() の結果

    Returns:
        DataFrame: classification 列を追加 ('fixed' or 'variable')
    """
    # TODO: 金額のばらつきで固定/変動を判定
    raise NotImplementedError


def detect_subscriptions():
    """
    固定費・サブスクリプションを検出して一覧を生成する。
    """
    logging.info("固定費・サブスクリプションの検出を開始します")

    conn = None
    try:
        conn = sqlite3.connect(FINANCE_DB_PATH)
        recurring = find_recurring_transactions(conn)
        classified = classify_fixed_variable(recurring)
        # TODO: 結果を表示または保存
    finally:
        if conn:
            conn.close()
