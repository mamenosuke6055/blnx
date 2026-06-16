"""inbox 取込パイプライン拡張(scripts/inbox_pipeline.py)のテスト。

process_inbox_file の「アーカイブ→取込→新規/重複カウント」を、importer を
スタブ化して source/finance.db 非依存に検証する。
"""

import sqlite3

import pytest

from py.init.init_db import create_finance_tables
from py.importers import raw_archive
from py.importers import inbox_pipeline


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
    """呼ばれるたび finance.db に n_tx 件の transaction を追加するスタブ importer。"""
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


def test_process_file_archives_and_counts_new_tx(monkeypatch, finance_db, raw_conn, tmp_path):
    monkeypatch.setattr(inbox_pipeline, "_import_one", _stub_import_adding(3))
    # detect_source はファイル名 'enavi...' で rakuten_card 判定
    csv = tmp_path / "enavi202606.csv"
    csv.write_bytes(b"line\n" * 4)

    rec = inbox_pipeline.process_inbox_file(csv, finance_db, raw_conn)

    assert rec["source"] == "rakuten_card"
    assert rec["archived"] is True
    assert rec["duplicate_file"] is False
    assert rec["new_tx"] == 3
    assert rec["imported"] is True
    # アーカイブに1件、メタも充填
    meta = raw_archive.list_archived(raw_conn)
    assert len(meta) == 1
    assert meta[0]["new_rows"] == 3


def test_redownload_same_content_is_marked_duplicate(monkeypatch, finance_db, raw_conn, tmp_path):
    monkeypatch.setattr(inbox_pipeline, "_import_one", _stub_import_adding(0))  # 冪等で新規0
    content = b"enavi,header\nrow1\n"
    f1 = tmp_path / "enavi2026061.csv"
    f2 = tmp_path / "enavi2026062.csv"  # 別名・同内容(再DL)
    f1.write_bytes(content)
    f2.write_bytes(content)

    r1 = inbox_pipeline.process_inbox_file(f1, finance_db, raw_conn)
    r2 = inbox_pipeline.process_inbox_file(f2, finance_db, raw_conn)

    assert r1["duplicate_file"] is False
    assert r2["duplicate_file"] is True            # 同内容は再DLとして検知
    assert r2["new_tx"] == 0
    assert len(raw_archive.list_archived(raw_conn)) == 1  # アーカイブは重複しない


def test_unknown_source_is_skipped(finance_db, raw_conn, tmp_path):
    csv = tmp_path / "mystery.csv"
    csv.write_bytes(b"foo,bar\n1,2\n")
    rec = inbox_pipeline.process_inbox_file(csv, finance_db, raw_conn)
    assert rec["source"] is None
    assert rec["imported"] is False
    assert rec["ignored"] is False  # 真の未知は「対象外」とは区別する
    assert rec["error"] == "種別不明（未対応フォーマット）"
    assert len(raw_archive.list_archived(raw_conn)) == 0


def test_run_inbox_import_moves_processed(monkeypatch, finance_db, raw_conn, tmp_path):
    monkeypatch.setattr(inbox_pipeline, "_import_one", _stub_import_adding(1))
    monkeypatch.setattr(inbox_pipeline.raw_archive, "open_raw_db", lambda: raw_conn)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "rb-20260601.csv").write_bytes("取引日,入出金(円)\n2026-06-01,100\n".encode("cp932"))
    processed = tmp_path / "processed"

    inbox_pipeline.run_inbox_import(inbox, finance_db, processed_dir=processed, show_watermark=False)

    assert not list(inbox.glob("*.csv"))           # inbox は空に
    assert (processed / "rb-20260601.csv").exists()  # processed へ移動
