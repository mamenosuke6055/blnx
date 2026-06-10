"""
自然言語インターフェース (Text-to-SQL)

自然言語でfinance.dbに問い合わせる。

フェーズ8: 自然言語インターフェース
"""
import sqlite3
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FINANCE_DB_PATH = BASE_DIR / "db" / "finance.db"

# DBスキーマの概要 (LLMに渡すコンテキスト)
SCHEMA_CONTEXT = """
テーブル構成:
- accounts (guid, name, account_type, parent_guid, ofx_type)
  account_type: ASSET, LIABILITY, INCOME, EXPENSE, EQUITY
- transactions (guid, post_date, description, ofx_fitid, manual_category_guid, ai_category_guid)
- splits (guid, tx_guid, account_guid, value_num, value_denom)
  金額 = value_num / value_denom
- investment_transactions (guid, tx_guid, inv_type, ticker, units, unit_price)
  inv_type: BUY, SELL, DIV, FEE, REINVEST, SPLIT
- currencies (guid, mnemonic)
- exchange_rates (guid, from_currency_guid, to_currency_guid, rate, date)
"""


def build_text_to_sql_prompt(question: str) -> str:
    """
    自然言語の質問をSQL生成プロンプトに変換する。

    Args:
        question: ユーザーの質問 (例: "先月の外食費はいくら？")

    Returns:
        str: LLMに送信するプロンプト
    """
    # TODO: SCHEMA_CONTEXT + question からプロンプトを構築
    # TODO: SQLiteの制約を明示
    # TODO: SELECT文のみ生成するよう指示
    raise NotImplementedError


def validate_sql(sql: str) -> bool:
    """
    生成されたSQLが安全かどうか検証する。

    Args:
        sql: LLMが生成したSQL

    Returns:
        bool: 実行可能かどうか
    """
    # TODO: SELECT以外 (INSERT, UPDATE, DELETE, DROP等) を拒否
    # TODO: SQLインジェクション対策
    raise NotImplementedError


def execute_query(conn, sql: str) -> list[dict]:
    """
    検証済みSQLを実行し結果を返す。

    Returns:
        list[dict]: クエリ結果
    """
    # TODO: validate_sql() で検証後に実行
    # TODO: 結果を辞書リストに変換
    raise NotImplementedError


def build_answer_prompt(question: str, query_result: list[dict]) -> str:
    """
    クエリ結果を自然言語の回答に変換するプロンプトを構築する。

    Returns:
        str: LLMに送信するプロンプト
    """
    # TODO: question + query_result から回答生成プロンプトを構築
    raise NotImplementedError


def ask(question: str) -> str:
    """
    自然言語で家計データに問い合わせる。

    Args:
        question: 質問文 (例: "今年の貯蓄率は？")

    Returns:
        str: 自然言語の回答
    """
    from py.ai.llm_router import route_query

    logging.info(f"質問: {question}")

    # Step 1: Text-to-SQL
    sql_prompt = build_text_to_sql_prompt(question)
    sql_result = route_query(sql_prompt)
    sql = sql_result['response']

    if not validate_sql(sql):
        return "安全でないクエリが生成されたため実行できません。"

    # Step 2: SQL実行
    conn = None
    try:
        conn = sqlite3.connect(FINANCE_DB_PATH)
        query_result = execute_query(conn, sql)
    finally:
        if conn:
            conn.close()

    # Step 3: 結果を自然言語に変換
    answer_prompt = build_answer_prompt(question, query_result)
    answer_result = route_query(answer_prompt)

    return answer_result['response']
