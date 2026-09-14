"""楽天銀行 importer のテスト(記帳サービス移行にあわせて新設)。

移行前の実装は鍵が `(日付, 金額, 摘要)` だけで残高を含まず、さらに
`drop_duplicates` でファイル内の同一行を潰していた。同日・同額・同摘要の
**正当な 2 件**が実在する口座ではこれは過少計上になる(二重計上の裏返し。
love `docs/Doc_Import_Identity_Simulation.md` 結果 4)。
"""
import csv as csvmod
import sqlite3

from py.importers.import_rakutenbank import import_rakuten_bank_csv
from py.init.init_db import create_finance_tables

HEADER = ["取引日", "入出金(円)", "入出金内容", "残高(円)"]


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csvmod.writer(f)
        w.writerow(HEADER)
        for date, amount, desc, balance in rows:
            w.writerow([date, amount, desc, balance])


def _make_db(tmp_path):
    db = tmp_path / "finance.db"
    c = sqlite3.connect(db)
    create_finance_tables(c)
    c.commit()
    c.close()
    return db


def _query(db, sql, *params):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def test_import_creates_balanced_splits(tmp_path):
    db = _make_db(tmp_path)
    csvp = tmp_path / "rb.csv"
    _write_csv(csvp, [("20260501", "-1200", "カフェ", "48,800")])
    import_rakuten_bank_csv(csvp, str(db))

    rows = _query(db, "SELECT a.name, s.value_num FROM splits s"
                      " JOIN accounts a ON a.guid = s.account_guid ORDER BY s.value_num")
    assert [r[1] for r in rows] == [-1200, 1200]
    assert {r[0] for r in rows} == {"Rakuten Bank", "Uncategorized"}
    assert _query(db, "SELECT fitid_policy FROM transactions") == [("v2",)]


def test_card_payment_goes_to_liability(tmp_path):
    db = _make_db(tmp_path)
    csvp = tmp_path / "rb.csv"
    _write_csv(csvp, [("20260527", "-153,802", "口座振替 ラクテンカ－ト゛サ－ヒ゛ス", "100,000")])
    import_rakuten_bank_csv(csvp, str(db))

    rows = _query(db, "SELECT a.name, a.account_type, s.value_num FROM splits s"
                      " JOIN accounts a ON a.guid = s.account_guid WHERE s.value_num > 0")
    assert rows == [("Rakuten Card", "LIABILITY", 153802)]


def test_reimport_is_idempotent(tmp_path):
    db = _make_db(tmp_path)
    csvp = tmp_path / "rb.csv"
    _write_csv(csvp, [("20260501", "-1200", "カフェ", "48,800"),
                      ("20260502", "230,416", "給与　サンプル", "279,216")])
    import_rakuten_bank_csv(csvp, str(db))
    import_rakuten_bank_csv(csvp, str(db))

    assert _query(db, "SELECT COUNT(*) FROM transactions")[0][0] == 2


def test_same_day_same_amount_rows_are_both_kept(tmp_path):
    """残高が違えば別の取引。旧実装はここで 1 件に潰していた(過少計上)。"""
    db = _make_db(tmp_path)
    csvp = tmp_path / "rb.csv"
    _write_csv(csvp, [("20260501", "-450", "振替 ＳＢＩ証券", "9,550"),
                      ("20260501", "-450", "振替 ＳＢＩ証券", "9,100")])
    import_rakuten_bank_csv(csvp, str(db))

    assert _query(db, "SELECT COUNT(*) FROM transactions")[0][0] == 2
    assert _query(db, "SELECT SUM(value_num) FROM splits s JOIN accounts a"
                      " ON a.guid = s.account_guid WHERE a.name = 'Rakuten Bank'")[0][0] == -900


def test_legacy_v1_rows_are_not_reimported(tmp_path):
    """v1 の鍵で既に入っている行は、v2 に移っても二重計上しない。"""
    import hashlib

    db = _make_db(tmp_path)
    v1 = "SHA256:" + hashlib.sha256("RAKUTENBANK:2026-05-01:-1200:カフェ".encode()).hexdigest()
    c = sqlite3.connect(db)
    c.execute("INSERT INTO transactions (guid, post_date, description, ofx_fitid)"
              " VALUES ('old','2026-05-01','カフェ',?)", (v1,))
    c.commit()
    c.close()

    csvp = tmp_path / "rb.csv"
    _write_csv(csvp, [("20260501", "-1200", "カフェ", "48,800")])
    import_rakuten_bank_csv(csvp, str(db))

    assert _query(db, "SELECT COUNT(*) FROM transactions")[0][0] == 1
