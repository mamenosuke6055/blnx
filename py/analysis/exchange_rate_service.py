"""USD/JPY 為替レートの DB 参照サービス。

exchange_rates テーブルから最新レートを取得する。テーブルが空または
指定日以前のレートがない場合はフォールバック値 150.0 を返す。
レートの更新は scripts/update_exchange_rates.py を使う。
"""
import sqlite3
from datetime import date

USD_GUID = "850599acc6554001bb4803762592ff24"
JPY_GUID = "bd6b14309fa7458ea6ed7ffc0cfcc0b8"
_FALLBACK_RATE = 150.0


def get_usd_jpy_rate(conn: sqlite3.Connection, at_date: str | None = None) -> float:
    """exchange_rates テーブルから at_date 以前の最新 USD/JPY レートを返す。

    テーブルに該当レートがない場合は 150.0 を返す。
    """
    if at_date is None:
        at_date = date.today().isoformat()
    cur = conn.cursor()
    cur.execute(
        "SELECT rate FROM exchange_rates "
        "WHERE from_currency_guid = ? AND to_currency_guid = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 1",
        (USD_GUID, JPY_GUID, at_date),
    )
    row = cur.fetchone()
    return row[0] if row else _FALLBACK_RATE
