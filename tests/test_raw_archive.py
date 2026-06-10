"""raw_imports アーカイブ(py/importers/raw_archive.py)のテスト。"""

import sqlite3

import pytest

from py.importers import raw_archive


@pytest.fixture
def raw_conn():
    c = sqlite3.connect(":memory:")
    raw_archive.init_raw_imports_db(c)
    yield c
    c.close()


def test_archive_bytes_stores_and_returns_id(raw_conn):
    res = raw_archive.archive_bytes(raw_conn, b"a,b,c\n1,2,3\n", "test.csv", source="rakuten_bank")
    assert res.is_duplicate is False
    assert res.row_id >= 1
    assert res.source == "rakuten_bank"
    assert len(res.sha256) == 64


def test_same_content_is_deduplicated(raw_conn):
    content = b"col1,col2\nx,y\n"
    first = raw_archive.archive_bytes(raw_conn, content, "a.csv")
    second = raw_archive.archive_bytes(raw_conn, content, "b_redownload.csv")
    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.row_id == first.row_id  # 既存行を指す
    # 重複は1行のみ
    assert raw_conn.execute("SELECT COUNT(*) FROM raw_imports").fetchone()[0] == 1


def test_different_content_creates_new_row(raw_conn):
    raw_archive.archive_bytes(raw_conn, b"one\n", "a.csv")
    raw_archive.archive_bytes(raw_conn, b"two\n", "b.csv")
    assert raw_conn.execute("SELECT COUNT(*) FROM raw_imports").fetchone()[0] == 2


def test_cp932_bytes_preserved_for_replay(raw_conn):
    # cp932(Shift-JIS)の生バイト列が無加工で往復できること(リプレイの前提)
    original = "楽天銀行,入金額\nラクテンショウケン,70000\n".encode("cp932")
    res = raw_archive.archive_bytes(raw_conn, original, "rb.csv", source="rakuten_bank")
    fetched = raw_archive.fetch_content(raw_conn, res.row_id)
    assert fetched == original
    assert fetched.decode("cp932").startswith("楽天銀行")


def test_archive_file_reads_bytes(tmp_path, raw_conn):
    p = tmp_path / "enavi202606.csv"
    p.write_bytes(b"line1\nline2\nline3\n")
    res = raw_archive.archive_file(raw_conn, p, source="rakuten_card")
    assert res.is_duplicate is False
    meta = raw_archive.list_archived(raw_conn, source="rakuten_card")
    assert len(meta) == 1
    assert meta[0]["filename"] == "enavi202606.csv"
    assert meta[0]["row_count"] == 3


def test_update_import_meta_fills_fields(raw_conn):
    res = raw_archive.archive_bytes(raw_conn, b"x\n", "a.csv", source="resona_bank")
    raw_archive.update_import_meta(
        raw_conn, res.row_id, new_rows=5, period_start="2026-05-01", period_end="2026-05-31"
    )
    meta = raw_archive.list_archived(raw_conn)[0]
    assert meta["new_rows"] == 5
    assert meta["period_start"] == "2026-05-01"
    assert meta["imported_at"] is not None


def test_list_archived_filters_by_source(raw_conn):
    raw_archive.archive_bytes(raw_conn, b"a\n", "a.csv", source="rakuten_bank")
    raw_archive.archive_bytes(raw_conn, b"b\n", "b.csv", source="rakuten_card")
    assert len(raw_archive.list_archived(raw_conn, source="rakuten_bank")) == 1
    assert len(raw_archive.list_archived(raw_conn)) == 2
