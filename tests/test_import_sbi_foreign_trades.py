"""SBI 外国株式等 約定履歴 (yakujo) importer のテスト。

2026-07-15 取込で発覚 (fd f16a1b73): 円貨決済行の約定単価が '0.01(160.55)' 形式
（括弧内 = 適用為替レート）で float 変換に失敗し、**ファイル全体が取込不能**だった。
また全行を USD 前提で記帳していたため、円貨決済行(通貨 = 日本円)の受渡金額(JPY)が
USD 現金勘定に混入する構造だった。

- 括弧付き単価のパース（外貨単価のみ採用）
- 円貨決済行は JPY_clearing / JPY 通貨で記帳、外貨行は USD 勘定のまま
- 貸借均衡

合成データのみ使用し、残高等の PII は含まない。
"""
import csv
import sqlite3
from pathlib import Path

import pytest

import py.importers.import_sbi_sec as sbi
from py.init.init_db import create_finance_tables


_HEADER = [
    "国内約定日", "通貨", "銘柄名", "取引", "預り区分",
    "約定数量", "約定単価", "国内受渡日", "受渡金額",
]

_ROWS = [
    ["通貨指定", "商品指定", "期間（国内約定日）開始", "期間（国内約定日）終了", "明細数"],
    ["すべての通貨", "すべての商品", "2026年06月15日", "2026年07月16日", "3"],
    _HEADER,
    # 外貨(USD)買付
    ["2026年06月17日", "米国ドル", "テスト・グロース ETF TST / NASDAQ", "買付", "NISA",
     "1", "206.87", "26/06/19", "206.87"],
    # 外貨(USD)MMF買付
    ["2026年06月17日", "米国ドル", "テストＭＭＦ（米ドル） X0000000", "買付", "特定",
     "1791", "0.01", "26/06/18", "17.91"],
    # 円貨決済買付: 約定単価に括弧付き為替レート、受渡金額は JPY
    ["2026年06月17日", "日本円", "テストＭＭＦ（米ドル） X0000000", "買付", "特定",
     "3114", "0.01(160.55)", "26/06/18", "4999"],
]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", encoding="cp932", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "finance.db"
    conn = sqlite3.connect(db_path)
    create_finance_tables(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(sbi, "get_db_path", lambda: db_path)
    return db_path


def _import(db, tmp_path):
    p = tmp_path / "yakujo20260715075038.csv"
    _write_csv(p, _ROWS)
    sbi.import_sbi_trade_history(p)


def test_parenthesized_unit_price_does_not_abort_file(db, tmp_path):
    """'0.01(160.55)' 形式の単価行があってもファイル全体が取り込まれる。"""
    _import(db, tmp_path)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
    prices = {
        r[0] for r in conn.execute(
            "SELECT unit_price FROM investment_transactions"
        ).fetchall()
    }
    conn.close()
    assert n == 3
    assert prices == {206.87, 0.01}  # 括弧内の為替レートは単価に混入しない


def test_jpy_settled_row_booked_in_jpy_clearing(db, tmp_path):
    """円貨決済行(通貨=日本円)の現金側が JPY_clearing、通貨が JPY で記帳される。"""
    _import(db, tmp_path)
    conn = sqlite3.connect(db)
    row = conn.execute(
        """SELECT s.value_num, s.value_denom FROM splits s
           JOIN accounts a ON a.guid = s.account_guid
           WHERE a.name = 'JPY_clearing'"""
    ).fetchone()
    ccy = conn.execute(
        """SELECT c.mnemonic FROM transactions t
           JOIN currencies c ON c.guid = t.currency_guid
           JOIN splits s ON s.tx_guid = t.guid
           JOIN accounts a ON a.guid = s.account_guid
           WHERE a.name = 'JPY_clearing'"""
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] / row[1] == -4999  # 買付の現金側 = 貸方(負値)
    assert ccy[0] == "JPY"


def test_usd_rows_stay_in_usd_account(db, tmp_path):
    """外貨行の現金側は従来どおり USD 勘定に記帳される。"""
    _import(db, tmp_path)
    conn = sqlite3.connect(db)
    total = conn.execute(
        """SELECT SUM(s.value_num * 1.0 / s.value_denom) FROM splits s
           JOIN accounts a ON a.guid = s.account_guid
           WHERE a.name = 'USD' AND a.ofx_type = 'BANK'"""
    ).fetchone()[0]
    conn.close()
    assert total == pytest.approx(-(206.87 + 17.91))


def test_splits_balance_to_zero(db, tmp_path):
    """全仕訳の貸借が 0 で均衡する（複式健全性）。"""
    _import(db, tmp_path)
    conn = sqlite3.connect(db)
    total = conn.execute(
        "SELECT COALESCE(SUM(value_num * 1.0 / value_denom), 0) FROM splits"
    ).fetchone()[0]
    conn.close()
    assert total == pytest.approx(0)
