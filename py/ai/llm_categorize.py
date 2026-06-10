"""
LLMベースのトランザクション分類

現行の sentence-transformers + scikit-learn を LLM に置換する。

フェーズ6: LLMによるインテリジェント分類
"""
import sqlite3
import pandas as pd
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FINANCE_DB_PATH = BASE_DIR / "db" / "finance.db"


def get_uncategorized_transactions(conn) -> pd.DataFrame:
    """
    未分類のトランザクションを取得する。

    Returns:
        DataFrame: guid, description, post_date, amount
    """
    query = """
    SELECT
        t.guid,
        t.description,
        t.post_date,
        s.value_num
    FROM transactions t
    JOIN splits s ON t.guid = s.tx_guid
    WHERE
        t.manual_category_guid IS NULL
        AND t.ai_category_guid IS NULL
        AND t.description IS NOT NULL
    GROUP BY t.guid
    """
    return pd.read_sql_query(query, conn)


def get_category_list(conn) -> list[dict]:
    """
    利用可能なカテゴリ一覧を取得する。

    Returns:
        list[dict]: [{'guid': str, 'path': str}, ...]
    """
    # TODO: accounts テーブルから Expenses / Income 配下のカテゴリを階層パスで取得
    raise NotImplementedError


def build_classification_prompt(description: str, categories: list[dict]) -> str:
    """
    分類用のプロンプトを構築する。

    Args:
        description: トランザクションの摘要
        categories: カテゴリ一覧

    Returns:
        str: LLMに送信するプロンプト
    """
    # TODO: few-shot例を含むプロンプトを構築
    # TODO: カテゴリ一覧をプロンプトに埋め込む
    raise NotImplementedError


def parse_classification_response(response: str, categories: list[dict]) -> dict:
    """
    LLMレスポンスからカテゴリとconfidenceを抽出する。

    Returns:
        dict: {'category_guid': str, 'confidence': float}
    """
    # TODO: レスポンスをパースし、カテゴリGUIDに変換
    raise NotImplementedError


def update_ai_category(conn, tx_guid: str, category_guid: str):
    """
    トランザクションの ai_category_guid を更新する。
    """
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE transactions SET ai_category_guid = ? WHERE guid = ?",
        (category_guid, tx_guid),
    )


def categorize_with_llm():
    """
    LLMを使って未分類トランザクションを分類する。
    """
    from py.ai.llm_router import route_query

    logging.info("LLMベースのカテゴリ分類を開始します")

    conn = None
    try:
        conn = sqlite3.connect(FINANCE_DB_PATH)
        transactions = get_uncategorized_transactions(conn)

        if transactions.empty:
            logging.info("未分類のトランザクションはありません")
            return

        logging.info(f"{len(transactions)}件の未分類トランザクションを処理します")

        categories = get_category_list(conn)

        for _, tx in transactions.iterrows():
            prompt = build_classification_prompt(tx['description'], categories)
            result = route_query(prompt)
            parsed = parse_classification_response(result['response'], categories)

            if parsed['confidence'] > 0:
                update_ai_category(conn, tx['guid'], parsed['category_guid'])
                logging.info(
                    f"[{result['source']}] {tx['description'][:30]} "
                    f"→ {parsed['category_guid']} (conf: {parsed['confidence']:.2f})"
                )

        conn.commit()
        logging.info("分類完了")
    finally:
        if conn:
            conn.close()
