"""楽天証券 投信取引 importer の 再投資(REINVEST) 記帳のかたちを固定する回帰テスト。

技術ノート 289c50d8ac 症状(2) / fd 012b5bf2f683 の再発防止。旧 importer は
分配金の再投資を「現金(Assets:Bank:Rakuten Securities)で買い付けた」形で書き、
fitid も口数の分母も落としていた。その結果 fitid NULL の行が冪等チェックを
すり抜け、現行版と同じ事象で二重計上になった(実データで 7 件)。

ここで固定するのは次の 4 点:
  - 貸方は Income:Dividend(分配金収益)であって現金口座ではない
  - 借方の口数は denom 10000 で持つ(分母なしで書かない)
  - ofx_fitid が必ず入る(NULL は UNIQUE も等値比較もすり抜ける)
  - 同じ CSV を 2 回取り込んでも増えない
"""
import csv
import sqlite3

import py.importers.import_rakuten_sec as rks
from py.init.init_db import create_finance_tables

HEADER = ["約定日", "受渡日", "ファンド名", "取引", "数量［口］", "単価",
          "受渡金額/(ポイント利用)[円]", "決済通貨"]

FUND = "J-REITオープン(年4回決算型)"


def _write_csv(path, rows):
    with open(path, "w", encoding="cp932", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "finance.db"
    c = sqlite3.connect(db)
    create_finance_tables(c)
    c.commit()
    c.close()
    monkeypatch.setattr(rks, "get_db_path", lambda: db)
    return db


def _splits(db):
    c = sqlite3.connect(db)
    try:
        return c.execute(
            "SELECT a.name, s.value_num * 1.0 / s.value_denom, s.quantity_num, s.quantity_denom"
            "  FROM splits s JOIN accounts a ON a.guid = s.account_guid").fetchall()
    finally:
        c.close()


def test_reinvest_credits_dividend_not_cash(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    csv_path = tmp_path / "toushin.csv"
    _write_csv(csv_path, [["2025/07/23", "2025/07/25", FUND, "再投資", "74", "1986", "147", "円"]])

    rks.import_rakuten_trade_invst(csv_path)

    rows = _splits(db)
    assert len(rows) == 2, rows
    debit = [r for r in rows if r[1] > 0]
    credit = [r for r in rows if r[1] < 0]
    assert len(debit) == 1 and len(credit) == 1

    # 借方 = ファンド、口数は denom 10000 で持つ(旧版は denom なしで 74 と書いていた)
    assert debit[0][0] == FUND
    assert debit[0][1] == 147
    assert debit[0][3] == 10000
    assert debit[0][2] / debit[0][3] == 74

    # 貸方 = 分配金収益。現金を減らさない(再投資は現金を消費しない)
    assert credit[0][0] == "Dividend"
    assert credit[0][1] == -147
    assert "Rakuten Securities" not in [r[0] for r in rows]


def test_reinvest_has_fitid_and_is_idempotent(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    csv_path = tmp_path / "toushin.csv"
    _write_csv(csv_path, [["2025/07/23", "2025/07/25", FUND, "再投資", "74", "1986", "147", "円"]])

    rks.import_rakuten_trade_invst(csv_path)
    rks.import_rakuten_trade_invst(csv_path)   # 2 回目は増えない

    c = sqlite3.connect(db)
    try:
        n, n_null = c.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE ofx_fitid IS NULL) FROM transactions").fetchone()
        inv = c.execute("SELECT type, units, total_amount FROM investment_transactions").fetchall()
    finally:
        c.close()

    assert n == 1, "同じ CSV の再取込で二重計上してはいけない"
    assert n_null == 0, "fitid が NULL だと冪等チェックをすり抜ける"
    assert inv == [("REINVEST", 74.0, 147.0)]
