#!/usr/bin/env python3
"""USD/JPY レートを frankfurter.app から取得して finance.db の exchange_rates テーブルに保存する。

使い方:
    cd /path/to/blnx && uv run python scripts/update_exchange_rates.py

APIキー不要。欧州中央銀行（ECB）のデータを使用（平日に日次更新）。
weekendや祝日は最新の営業日レートが返る。
"""
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.modules.pop("py", None)

from py.analysis.exchange_rate_service import JPY_GUID, USD_GUID  # noqa: E402
from py.util.db import get_db_path  # noqa: E402

_API_URL = "https://api.frankfurter.app/latest?from=USD&to=JPY"


def fetch_usd_jpy() -> tuple[str, float]:
    """frankfurter.app から最新 USD/JPY レートを取得。(date_str, rate) を返す。"""
    with urllib.request.urlopen(_API_URL, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    return data["date"], float(data["rates"]["JPY"])


def save_rate(db_path: Path, rate_date: str, rate: float) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO exchange_rates "
        "(date, from_currency_guid, to_currency_guid, rate) VALUES (?, ?, ?, ?)",
        (rate_date, USD_GUID, JPY_GUID, rate),
    )
    conn.commit()
    conn.close()
    print(f"USD/JPY 更新: {rate:.2f} 円  (date: {rate_date}, db: {db_path})")


if __name__ == "__main__":
    db_path = get_db_path()
    rate_date, rate = fetch_usd_jpy()
    save_rate(db_path, rate_date, rate)
