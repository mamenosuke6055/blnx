"""SBI証券 importer: 旧鍵(v1)突合の範囲と、外貨入出金の相手勘定のテスト。

- v1 の鍵は連番も NISA の口座区分も持たないので、同日・同額・同摘要の 2 本(つみたて枠と
  成長枠の同時買付など)を 1 件に畳んでいた。再取込で 2 本目が補われること。
- 外貨入出金の相手は清算勘定。円建ての銀行口座に USD 額を記帳しないこと。

合成データのみ使用する。
"""
import csv
import hashlib
import sqlite3
from pathlib import Path

import pytest

import py.importers.import_sbi_sec as sbi
from py.init.init_db import create_finance_tables

_HEADER = [
    "約定日", "銘柄", "銘柄コード", "市場", "取引", "期限", "預り", "課税",
    "約定数量", "約定単価", "手数料/諸経費等", "税額", "受渡日", "受渡金額/決済損益",
]
_FUND = "テスト・インデックスファンド"


def _row(deposit):
    return ["2025/03/03", _FUND, "", "", "投信金額買付", "--", deposit, "--",
            "1000", "10000", "--", "--", "2025/03/06", "1000"]


def _write_trades(path: Path, rows) -> None:
    with open(path, "w", encoding="cp932", newline="") as f:
        w = csv.writer(f)
        w.writerow(["約定履歴照会 "])
        w.writerow([])
        w.writerow(_HEADER)
        w.writerows(rows)


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "finance.db"
    conn = sqlite3.connect(db_path)
    create_finance_tables(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(sbi, "get_db_path", lambda: db_path)
    return db_path


def _count(db_path, table="transactions"):
    conn = sqlite3.connect(db_path)
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return n


def _as_v1(db_path):
    """既存の 1 行を v1 の鍵・方式に書き換えて、v1 時代の DB を再現する。"""
    legacy = "SHA256:" + hashlib.sha256(
        f"SBISEC_DOM:2025-03-03:{_FUND}:投信金額買付:{sbi.parse_amount('1000')}:"
        f"{sbi.parse_amount('1000')}".encode()).hexdigest()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    conn.execute("UPDATE transactions SET ofx_fitid=?, fitid_policy='v1', natural_key=NULL", (legacy,))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("second", [" NISA(つ) ", " NISA(成) "])
def test_recovers_second_identical_trade_collapsed_by_v1(db, tmp_path, second):
    """v1 が 1 件に畳んだ同日同額の 2 本目(別枠・同じ枠とも)が、再取込で補われる。"""
    p = tmp_path / "SaveFile_000001_000002.csv"
    _write_trades(p, [_row(" NISA(つ) ")])
    sbi.import_sbi_domestic_trade_history(p)
    _as_v1(db)

    _write_trades(p, [_row(" NISA(つ) "), _row(second)])
    sbi.import_sbi_domestic_trade_history(p)
    assert _count(db) == 2
    assert _count(db, "investment_transactions") == 2
    sbi.import_sbi_domestic_trade_history(p)
    assert _count(db) == 2


def test_v1_row_still_blocks_reimport_of_first(db, tmp_path):
    """旧鍵の突合は 1 本目には効き続ける(v1 時代の行を二重に入れない)。"""
    p = tmp_path / "SaveFile_000001_000001.csv"
    _write_trades(p, [_row(" NISA(つ) ")])
    sbi.import_sbi_domestic_trade_history(p)
    _as_v1(db)
    sbi.import_sbi_domestic_trade_history(p)
    assert _count(db) == 1


def _write_usd_dw(path: Path) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["外貨入出金明細"])
        w.writerow(["入出金日", "区分", "通貨", "摘要", "出金額", "入金額"])
        w.writerow(["2025/03/03", "入金", "米ドル", "住信SBIネット銀行から外貨入金", "-", "75"])


def test_usd_deposit_is_booked_against_clearing_not_jpy_bank(db, tmp_path):
    """銀行からの外貨入金は清算勘定が相手。円建ての銀行口座に USD 額を立てない。"""
    p = tmp_path / "nyushukkin_20250303.csv"
    _write_usd_dw(p)
    sbi.import_sbi_deposit_withdrawal(p)

    conn = sqlite3.connect(db)
    legs = dict(conn.execute(
        """SELECT p.name || ':' || a.name, s.value_num * 1.0 / s.value_denom
           FROM splits s JOIN accounts a ON a.guid = s.account_guid
           JOIN accounts p ON p.guid = a.parent_guid"""))
    conn.close()
    assert legs == {"SBI Securities:USD": 75.0, "Assets:Transfer": -75.0}
