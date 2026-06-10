import csv as csvmod
import sqlite3

import pytest

from py.init.init_db import create_finance_tables
from py.importers.import_resona_bank import (
    import_resona_bank_csv,
    parse_resona_bank_csv,
)
from py.processing.classify_bank_income import build_rules

# 実フォーマットの21列ヘッダ（全角スペース含む）。parse は index 参照なので中身は形式のみ重要。
HEADER = [
    "レコード区分", "年", "月", "日", "時", "分", "連絡先名", "金融機関名", "支店名",
    "口座番号区分", "口座種別", "口座番号", "再送表示", "取引名",
    "取扱日付　年", "取扱日付　月", "取扱日付　日", "金額", "取引後残高", "摘要", "コメント",
]


def _row(record_type, txn_name, y, m, d, amount, balance, tekiyo):
    r = [""] * 21
    r[0] = record_type
    r[13] = txn_name
    r[14], r[15], r[16] = y, m, d
    r[17] = amount
    r[18] = balance
    r[19] = tekiyo
    return r


def _write_csv(path, rows):
    with open(path, "w", encoding="cp932", newline="") as f:
        w = csvmod.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow(r)


def test_parse_basic(tmp_path):
    p = tmp_path / "resona.csv"
    _write_csv(p, [
        _row("明細", "入金", "2026", "5", "1", "10,000", "50,000", "給与"),
        _row("明細", "支払", "2026", "5", "2", "3,000", "47,000", "コンビニ"),
    ])
    txns = parse_resona_bank_csv(p)
    assert len(txns) == 2
    assert txns[0] == {
        "date": "2026-05-01", "amount": 10000, "txn_name": "入金",
        "description": "給与", "balance": "50,000",
    }
    assert txns[1]["txn_name"] == "支払"
    assert txns[1]["amount"] == 3000


def test_summary_rows_excluded(tmp_path):
    # レコード区分『合計』(取引名=残高) は除外される
    p = tmp_path / "r.csv"
    _write_csv(p, [
        _row("合計", "残高", "", "", "", "", "47,000", ""),
        _row("明細", "入金", "2026", "12", "25", "5,000", "5,000", "ボーナス"),
    ])
    txns = parse_resona_bank_csv(p)
    assert len(txns) == 1
    assert txns[0]["date"] == "2026-12-25"


def test_date_zero_padded(tmp_path):
    p = tmp_path / "r.csv"
    _write_csv(p, [_row("明細", "支払", "2020", "7", "5", "1,200", "900", "カフェ")])
    assert parse_resona_bank_csv(p)[0]["date"] == "2020-07-05"


def test_amount_is_absolute(tmp_path):
    # 金額に符号があっても絶対値（入出金は取引名で判断するため）
    p = tmp_path / "r.csv"
    _write_csv(p, [_row("明細", "支払", "2026", "1", "1", "-2,000", "0", "テスト")])
    assert parse_resona_bank_csv(p)[0]["amount"] == 2000


def _make_db(tmp_path):
    db = tmp_path / "finance.db"
    c = sqlite3.connect(db)
    create_finance_tables(c)
    c.commit()
    c.close()
    return db


def test_import_creates_balanced_splits(tmp_path):
    db = _make_db(tmp_path)
    csvp = tmp_path / "r.csv"
    _write_csv(csvp, [
        _row("明細", "入金", "2026", "5", "1", "10,000", "50,000", "給与"),
        _row("明細", "支払", "2026", "5", "2", "3,000", "47,000", "コンビニ"),
    ])
    import_resona_bank_csv(csvp, str(db))

    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    # 各取引の借貸は0サム
    for (tx,) in c.execute("SELECT guid FROM transactions").fetchall():
        s = c.execute("SELECT SUM(value_num) FROM splits WHERE tx_guid=?", (tx,)).fetchone()[0]
        assert s == 0
    # Resona Bank 残高 = 入金10000 - 支払3000
    bank = c.execute("SELECT guid FROM accounts WHERE name='Resona Bank'").fetchone()[0]
    assert c.execute("SELECT SUM(value_num) FROM splits WHERE account_guid=?", (bank,)).fetchone()[0] == 7000
    c.close()


def test_import_idempotent(tmp_path):
    db = _make_db(tmp_path)
    csvp = tmp_path / "r.csv"
    _write_csv(csvp, [_row("明細", "入金", "2026", "5", "1", "10,000", "50,000", "給与")])
    import_resona_bank_csv(csvp, str(db))
    import_resona_bank_csv(csvp, str(db))  # 再インポート

    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    c.close()


def _income_uncat_guid(c):
    row = c.execute(
        "SELECT a.guid FROM accounts a JOIN accounts p ON p.guid=a.parent_guid "
        "WHERE a.name='Uncategorized' AND a.account_type='INCOME' AND p.name='Income'"
    ).fetchone()
    return row[0] if row else None


def test_import_classifies_self_transfer(tmp_path, monkeypatch):
    # 本人名義振込（半角カナ）は Assets:Transfer に計上され、収入に混入しない
    # 本人名義はコードでなく config 由来のため、サンプル名義を注入する
    monkeypatch.setattr(
        "py.processing.classify_bank_income._RULES",
        build_rules(owner_transfer_names=("ﾔﾏﾀﾞ",)),
    )
    db = _make_db(tmp_path)
    csvp = tmp_path / "r.csv"
    _write_csv(csvp, [_row("明細", "入金", "2026", "1", "24", "29,000", "29,000", "振込　ﾔﾏﾀﾞ ﾀﾛｳ")])
    import_resona_bank_csv(csvp, str(db))

    c = sqlite3.connect(db)
    transfer = c.execute(
        "SELECT guid FROM accounts WHERE name='Transfer' AND account_type='ASSET'"
    ).fetchone()[0]
    assert c.execute("SELECT SUM(value_num) FROM splits WHERE account_guid=?", (transfer,)).fetchone()[0] == -29000
    # Income:Uncategorized には計上されない（勘定が無いか残高0）
    uncat = _income_uncat_guid(c)
    if uncat:
        bal = c.execute("SELECT COALESCE(SUM(value_num),0) FROM splits WHERE account_guid=?", (uncat,)).fetchone()[0]
        assert bal == 0
    c.close()


def test_import_classifies_salary(tmp_path):
    db = _make_db(tmp_path)
    csvp = tmp_path / "r.csv"
    _write_csv(csvp, [_row("明細", "入金", "2026", "1", "27", "210,000", "210,000", "給与　タナカ　ジロウ")])
    import_resona_bank_csv(csvp, str(db))

    c = sqlite3.connect(db)
    salary = c.execute(
        "SELECT a.guid FROM accounts a JOIN accounts p ON p.guid=a.parent_guid "
        "WHERE a.name='Salary' AND p.name='Income'"
    ).fetchone()[0]
    assert c.execute("SELECT SUM(value_num) FROM splits WHERE account_guid=?", (salary,)).fetchone()[0] == -210000
    c.close()


def test_import_unknown_income_stays_uncategorized(tmp_path):
    # 未知の入金（給与/利息/自己移動でない）は Income:Uncategorized に保留される
    db = _make_db(tmp_path)
    csvp = tmp_path / "r.csv"
    _write_csv(csvp, [_row("明細", "入金", "2026", "1", "5", "3,640", "3,640", "サンプルキヨウカイ")])
    import_resona_bank_csv(csvp, str(db))

    c = sqlite3.connect(db)
    uncat = _income_uncat_guid(c)
    assert uncat is not None
    assert c.execute("SELECT SUM(value_num) FROM splits WHERE account_guid=?", (uncat,)).fetchone()[0] == -3640
    c.close()
