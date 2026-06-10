"""アーカイブからのリプレイ(py/importers/replay.py)のテスト。

importer を _import_one スタブ化して source/実 importer 非依存に、
「アーカイブ → 現行 importer で再適用 → 新規件数測定 → バックアップ」を検証する。
"""
import sqlite3

import pytest

from py.init.init_db import create_finance_tables
from py.importers import raw_archive
from py.importers import inbox_pipeline
from py.importers import replay


@pytest.fixture
def finance_db(tmp_path):
    path = tmp_path / "finance.db"
    conn = sqlite3.connect(path)
    create_finance_tables(conn)
    conn.close()
    return path


@pytest.fixture
def raw_conn():
    c = sqlite3.connect(":memory:")
    raw_archive.init_raw_imports_db(c)
    yield c
    c.close()


def _stub_import_adding(n_tx):
    """呼ばれるたび finance.db に n_tx 件 transaction を追加するスタブ importer。"""
    def _stub(csv_file, source, db_path):
        conn = sqlite3.connect(db_path)
        for i in range(n_tx):
            conn.execute(
                "INSERT INTO transactions (guid, post_date, description) VALUES (?, ?, ?)",
                (f"{csv_file.name}-{i}-{source}", "2026-06-01", "stub"),
            )
        conn.commit()
        conn.close()
    return _stub


def test_replay_reapplies_and_counts(monkeypatch, finance_db, raw_conn):
    monkeypatch.setattr(inbox_pipeline, "_import_one", _stub_import_adding(2))
    raw_archive.archive_bytes(raw_conn, b"a\nb\n", "enavi202605.csv", source="rakuten_card")
    raw_archive.archive_bytes(raw_conn, b"x\ny\n", "rb-202605.csv", source="rakuten_bank")

    records = replay.replay_archives(raw_conn, finance_db, backup=False)

    assert len(records) == 2
    assert all(r["error"] is None for r in records)
    assert all(r["new_tx"] == 2 for r in records)
    # finance.db に 2件×2ファイル = 4件入った
    conn = sqlite3.connect(finance_db)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4
    conn.close()


def test_replay_source_filter(monkeypatch, finance_db, raw_conn):
    monkeypatch.setattr(inbox_pipeline, "_import_one", _stub_import_adding(1))
    raw_archive.archive_bytes(raw_conn, b"a\n", "enavi.csv", source="rakuten_card")
    raw_archive.archive_bytes(raw_conn, b"b\n", "rb.csv", source="rakuten_bank")

    records = replay.replay_archives(raw_conn, finance_db, source="rakuten_card", backup=False)

    assert len(records) == 1
    assert records[0]["source"] == "rakuten_card"


def test_replay_dry_run_does_not_import(monkeypatch, finance_db, raw_conn):
    # dry_run では _import_one を呼ばない(呼ばれたら失敗させる)
    monkeypatch.setattr(inbox_pipeline, "_import_one",
                        lambda *a, **k: pytest.fail("dry_run で importer が呼ばれた"))
    raw_archive.archive_bytes(raw_conn, b"a\n", "enavi.csv", source="rakuten_card")

    records = replay.replay_archives(raw_conn, finance_db, dry_run=True)

    assert len(records) == 1
    assert records[0]["new_tx"] is None
    conn = sqlite3.connect(finance_db)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    conn.close()


def test_replay_creates_backup(monkeypatch, finance_db, raw_conn):
    monkeypatch.setattr(inbox_pipeline, "_import_one", _stub_import_adding(0))
    raw_archive.archive_bytes(raw_conn, b"a\n", "enavi.csv", source="rakuten_card")

    replay.replay_archives(raw_conn, finance_db, backup=True)

    baks = list(finance_db.parent.glob("finance.db.bak.before_replay_*"))
    assert len(baks) == 1


def test_replay_unknown_source_records_error(monkeypatch, finance_db, raw_conn):
    monkeypatch.setattr(inbox_pipeline, "_import_one", _stub_import_adding(1))
    raw_archive.archive_bytes(raw_conn, b"a\n", "mystery.csv", source=None)

    records = replay.replay_archives(raw_conn, finance_db, backup=False)

    assert len(records) == 1
    assert records[0]["new_tx"] is None
    assert "source 不明" in records[0]["error"]
