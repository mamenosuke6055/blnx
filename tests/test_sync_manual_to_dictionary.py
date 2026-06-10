import sqlite3

import pytest

from py.init.init_db import create_finance_tables
from py.init.init_dictionary_schema import create_dictionary_tables
from py.processing.sync_manual_to_dictionary import (
    extract_manual_categories,
    run_sync,
    sync_manual_categories_to_dictionary,
)


@pytest.fixture
def dict_conn():
    c = sqlite3.connect(":memory:")
    create_dictionary_tables(c)
    c.commit()
    yield c
    c.close()


def _add_account(conn, guid, name, account_type, parent_guid=None):
    conn.execute(
        "INSERT INTO accounts (guid, name, account_type, parent_guid) VALUES (?, ?, ?, ?)",
        (guid, name, account_type, parent_guid),
    )


def _add_manual_tx(conn, guid, description, manual_category_guid, post_date="2026-05-01"):
    conn.execute(
        "INSERT INTO transactions (guid, post_date, enter_date, description, manual_category_guid) "
        "VALUES (?, ?, ?, ?, ?)",
        (guid, post_date, f"{post_date} 00:00:00", description, manual_category_guid),
    )


@pytest.fixture
def seeded_finance(conn):
    """最小の勘定科目ツリー:

    Expenses > 食費 > {外食, 食料品}
    Income   > 収入 > 給与
    Root     > Transfer (EXPENSE/INCOME 外 = 還元対象外)
    """
    _add_account(conn, "exp", "Expenses", "EXPENSE")
    _add_account(conn, "food", "食費", "EXPENSE", "exp")
    _add_account(conn, "eat_out", "外食", "EXPENSE", "food")
    _add_account(conn, "grocery", "食料品", "EXPENSE", "food")
    _add_account(conn, "inc", "Income", "INCOME")
    _add_account(conn, "income_main", "収入", "INCOME", "inc")
    _add_account(conn, "salary", "給与", "INCOME", "income_main")
    _add_account(conn, "root_t", "Root", "ROOT")
    _add_account(conn, "transfer", "Transfer", "EQUITY", "root_t")
    conn.commit()
    return conn


def test_single_manual_category_creates_rule(seeded_finance, dict_conn):
    _add_manual_tx(seeded_finance, "t1", "ｽﾀｰﾊﾞﾂｸｽ", "eat_out")
    seeded_finance.commit()

    stats = sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    assert stats["new"] == 1
    rows = dict_conn.execute(
        "SELECT d.description, m.main_category, m.sub_category "
        "FROM category_dict d JOIN category_dict_manual m ON d.category_id = m.id"
    ).fetchall()
    assert rows == [("ｽﾀｰﾊﾞﾂｸｽ", "食費", "外食")]


def test_same_category_multiple_descriptions(seeded_finance, dict_conn):
    _add_manual_tx(seeded_finance, "t1", "ｽﾀｰﾊﾞﾂｸｽ", "eat_out")
    _add_manual_tx(seeded_finance, "t2", "ﾏｸﾄﾞﾅﾙﾄﾞ", "eat_out")
    seeded_finance.commit()

    stats = sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    assert stats["new"] == 2
    # (食費,外食) は 1 行に集約、description は 2 行
    assert dict_conn.execute("SELECT COUNT(*) FROM category_dict_manual").fetchone()[0] == 1
    assert dict_conn.execute("SELECT COUNT(*) FROM category_dict").fetchone()[0] == 2
    assert stats["categories"] == 1


def test_latest_post_date_wins(seeded_finance, dict_conn):
    # 同一摘要が古→外食、新→食料品。最新を採用。
    _add_manual_tx(seeded_finance, "t1", "ｱｲﾏｲﾃﾝ", "eat_out", post_date="2026-01-01")
    _add_manual_tx(seeded_finance, "t2", "ｱｲﾏｲﾃﾝ", "grocery", post_date="2026-05-01")
    seeded_finance.commit()

    sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    row = dict_conn.execute(
        "SELECT m.sub_category FROM category_dict d "
        "JOIN category_dict_manual m ON d.category_id = m.id WHERE d.description = ?",
        ("ｱｲﾏｲﾃﾝ",),
    ).fetchone()
    assert row[0] == "食料品"
    # description は UNIQUE なので 1 行のみ
    assert dict_conn.execute("SELECT COUNT(*) FROM category_dict").fetchone()[0] == 1


def test_null_manual_category_ignored(seeded_finance, dict_conn):
    _add_manual_tx(seeded_finance, "t1", "未分類のまま", None)
    seeded_finance.commit()

    stats = sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    assert stats["new"] == 0
    assert dict_conn.execute("SELECT COUNT(*) FROM category_dict").fetchone()[0] == 0


def test_transfer_category_excluded(seeded_finance, dict_conn):
    # Transfer は EXPENSE/INCOME 外なので辞書還元対象外
    _add_manual_tx(seeded_finance, "t1", "口座振替", "transfer")
    seeded_finance.commit()

    stats = sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    assert stats["new"] == 0
    assert dict_conn.execute("SELECT COUNT(*) FROM category_dict").fetchone()[0] == 0


def test_income_category_included(seeded_finance, dict_conn):
    _add_manual_tx(seeded_finance, "t1", "給料振込", "salary")
    seeded_finance.commit()

    sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    row = dict_conn.execute(
        "SELECT m.main_category, m.sub_category FROM category_dict d "
        "JOIN category_dict_manual m ON d.category_id = m.id WHERE d.description = ?",
        ("給料振込",),
    ).fetchone()
    assert row == ("収入", "給与")


def test_idempotent(seeded_finance, dict_conn):
    _add_manual_tx(seeded_finance, "t1", "ｽﾀｰﾊﾞﾂｸｽ", "eat_out")
    seeded_finance.commit()

    sync_manual_categories_to_dictionary(seeded_finance, dict_conn)
    stats2 = sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    assert stats2["new"] == 0
    assert stats2["unchanged"] == 1
    assert dict_conn.execute("SELECT COUNT(*) FROM category_dict").fetchone()[0] == 1


def test_existing_rule_updated_on_recategorize(seeded_finance, dict_conn):
    _add_manual_tx(seeded_finance, "t1", "ｺﾝﾋﾞﾆ", "eat_out", post_date="2026-05-01")
    seeded_finance.commit()
    sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    # 後日、同じ摘要を別カテゴリに付け替え
    _add_manual_tx(seeded_finance, "t2", "ｺﾝﾋﾞﾆ", "grocery", post_date="2026-06-01")
    seeded_finance.commit()
    stats = sync_manual_categories_to_dictionary(seeded_finance, dict_conn)

    assert stats["updated"] == 1
    assert stats["new"] == 0
    row = dict_conn.execute(
        "SELECT m.sub_category FROM category_dict d "
        "JOIN category_dict_manual m ON d.category_id = m.id WHERE d.description = ?",
        ("ｺﾝﾋﾞﾆ",),
    ).fetchone()
    assert row[0] == "食料品"


def test_run_sync_end_to_end_with_file_dbs(tmp_path):
    """run_sync が file DB のパスを受けて open→sync→close を完結する（run_categorize 前段で使う経路）。"""
    fdb = tmp_path / "finance.db"
    ddb = tmp_path / "dictionary.db"

    fc = sqlite3.connect(fdb)
    create_finance_tables(fc)
    _add_account(fc, "exp", "Expenses", "EXPENSE")
    _add_account(fc, "food", "食費", "EXPENSE", "exp")
    _add_account(fc, "eat_out", "外食", "EXPENSE", "food")
    _add_manual_tx(fc, "t1", "ｽﾀｰﾊﾞﾂｸｽ", "eat_out")
    fc.commit()
    fc.close()

    dc = sqlite3.connect(ddb)
    create_dictionary_tables(dc)
    dc.commit()
    dc.close()

    stats = run_sync(fdb, ddb)
    assert stats is not None
    assert stats["new"] == 1

    # 実際に辞書へ書き込まれていることを確認
    dc = sqlite3.connect(ddb)
    n = dc.execute("SELECT COUNT(*) FROM category_dict").fetchone()[0]
    dc.close()
    assert n == 1


def test_run_sync_missing_db_returns_none(tmp_path):
    """DB 未整備時は None を返してパイプライン（run_categorize）を止めない。"""
    assert run_sync(tmp_path / "absent_finance.db", tmp_path / "absent_dict.db") is None


def test_extract_manual_categories(seeded_finance):
    _add_manual_tx(seeded_finance, "t1", "ｽﾀｰﾊﾞﾂｸｽ", "eat_out")
    _add_manual_tx(seeded_finance, "t2", "給料振込", "salary")
    _add_manual_tx(seeded_finance, "t3", "未分類", None)
    seeded_finance.commit()

    entries = extract_manual_categories(seeded_finance)

    by_desc = {e["description"]: e for e in entries}
    assert set(by_desc) == {"ｽﾀｰﾊﾞﾂｸｽ", "給料振込"}
    assert by_desc["ｽﾀｰﾊﾞﾂｸｽ"] == {
        "description": "ｽﾀｰﾊﾞﾂｸｽ",
        "main_category": "食費",
        "sub_category": "外食",
    }
