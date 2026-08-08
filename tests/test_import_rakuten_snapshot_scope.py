"""楽天証券『資産残高』の時価スナップが、同名の他階層口座へ迷い込まないこと。

## 背景

スナップの口座解決が `SELECT guid FROM accounts WHERE name = ?`（DB 全体からの
名前検索）だったため、同名口座が他ブローカー／他階層にあると任意の 1 件に当たり、
時価が無関係な口座へ付いた。結果、簿価と時価が別口座に分かれる（片肺）。

2026-08-09 に `scripts/merge_orphan_investment_accounts.py` で 5 件を統合し、
検索を楽天証券の枠内に限定した。fossil 5826f95c93（迷子 Rakuten Card）と同型の
3 度目の再発なので、ここで固定する。
"""
import csv
import sqlite3
import uuid
from pathlib import Path

import pytest

import py.importers.import_rakuten_sec as rak
from py.init.init_db import create_finance_tables

# 実 assetbalance CSV を模した最小構成（明細ヘッダーは『口座』+『評価額』で検出される）
_ROWS = [
    ["資産残高"],
    [],
    ["口座", "銘柄名", "保有数量", "評価額"],
    ["特定", "テスト投信", "100", "52397"],
]


def _write_csv(path: Path) -> None:
    with open(path, "w", encoding="cp932", newline="") as f:
        csv.writer(f).writerows(_ROWS)


def _mk_account(conn, name, parent_name=None, ofx="INVESTMENT"):
    parent_guid = None
    if parent_name:
        parent_guid = conn.execute(
            "SELECT guid FROM accounts WHERE name=?", (parent_name,)
        ).fetchone()[0]
    guid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO accounts (guid, name, account_type, ofx_type, parent_guid)"
        " VALUES (?,?,?,?,?)",
        (guid, name, "ASSET", ofx, parent_guid),
    )
    return guid


@pytest.fixture
def db_with_same_name_in_two_places(tmp_path, monkeypatch):
    """同名口座が『Investments 直下』と『Rakuten Securities 配下』の両方にある DB。"""
    db_path = tmp_path / "finance.db"
    conn = sqlite3.connect(db_path)
    create_finance_tables(conn)
    _mk_account(conn, "Investments", ofx=None)
    _mk_account(conn, "Rakuten Securities", "Investments", ofx=None)
    ids = {
        # 迷子側（先に作る = 素朴な名前検索だとこちらに当たりやすい）
        "orphan": _mk_account(conn, "テスト投信", "Investments"),
        "real": _mk_account(conn, "テスト投信", "Rakuten Securities"),
        "db_path": db_path,
    }
    conn.commit()
    conn.close()
    monkeypatch.setattr(rak, "get_db_path", lambda: db_path)
    return ids


def test_snapshot_lands_on_rakuten_scoped_account(db_with_same_name_in_two_places, tmp_path):
    """同名口座が複数あっても、楽天証券配下の口座にスナップが付く。"""
    ids = db_with_same_name_in_two_places
    csv_path = tmp_path / "assetbalance_20260507_x.csv"
    _write_csv(csv_path)

    rak.import_rakuten_asset_balance(csv_path)

    conn = sqlite3.connect(ids["db_path"])
    rows = conn.execute(
        "SELECT account_guid, market_value_num FROM asset_snapshots"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == ids["real"], "スナップが迷子口座へ付いている"
    assert rows[0][1] == 52397


def test_no_duplicate_account_created(db_with_same_name_in_two_places, tmp_path):
    """既存口座にマッチする限り、新しい口座を増やさない。"""
    ids = db_with_same_name_in_two_places
    csv_path = tmp_path / "assetbalance_20260507_x.csv"
    _write_csv(csv_path)

    conn = sqlite3.connect(ids["db_path"])
    before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    conn.close()

    rak.import_rakuten_asset_balance(csv_path)

    conn = sqlite3.connect(ids["db_path"])
    after = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    conn.close()
    assert after == before
