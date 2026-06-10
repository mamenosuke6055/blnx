"""
ポートフォリオ・リバランス計算

フェーズ5: 財務諸表と資産管理
"""
import sqlite3
import pandas as pd
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FINANCE_DB_PATH = BASE_DIR / "db" / "finance.db"


def get_current_allocation(conn) -> pd.DataFrame:
    """
    現在のアセットアロケーションを取得する。

    Returns:
        DataFrame: asset_class, current_value, current_ratio
    """
    # TODO: investment_transactions + 最新評価額からアセットクラス別の残高を集計
    raise NotImplementedError


def calculate_rebalance(
    current: pd.DataFrame,
    target: dict,
    additional_investment: int = 0,
) -> pd.DataFrame:
    """
    ノーセル・リバランスの売買提案を計算する。

    Args:
        current: get_current_allocation() の結果
        target: 目標アロケーション {'国内株式': 0.25, '先進国株式': 0.50, ...}
        additional_investment: 追加投資額

    Returns:
        DataFrame: asset_class, current_value, target_value, diff, action
    """
    # TODO: 追加投資額を配分して目標アロケーションに近づける
    # TODO: 売却なし（ノーセル）の制約で計算
    raise NotImplementedError


def suggest_rebalance(target: dict = None, additional_investment: int = 0):
    """
    リバランス提案を生成する。

    Args:
        target: 目標アロケーション。省略時はデフォルト設定を使用。
        additional_investment: 追加投資額
    """
    logging.info("リバランス計算を開始します")

    if target is None:
        # TODO: config/settings.json または別ファイルから読み込み
        target = {}

    conn = None
    try:
        conn = sqlite3.connect(FINANCE_DB_PATH)
        current = get_current_allocation(conn)
        proposal = calculate_rebalance(current, target, additional_investment)
        # TODO: 結果を表示または保存
    finally:
        if conn:
            conn.close()
