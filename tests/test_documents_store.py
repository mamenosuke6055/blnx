"""documents raw store のテスト。

不変条件:
- 同一内容は blob 1 個に dedup されるが、取り込みログは毎回追記される
  (「この日に再取得した」こと自体が記録)。
- バイト列は改変されない(文字コード変換・改行変換をしない)。
- 用途区分(kind)は語彙が固定され、既存 DB へは移行で追加できる。
"""
import sqlite3

import pytest

from py.documents import store


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    yield c
    c.close()


def test_add_bytes_roundtrip_preserves_exact_bytes(conn):
    # cp932 の CSV は decode すると壊れうる。原本のバイト列がそのまま戻ること。
    raw = "年月,合計料金\r\n202606,6342\r\n".encode("cp932")
    doc_id, sha, is_new = store.add_bytes(
        conn, raw, source="cde_mypage", original_name="PlatformDownload.csv"
    )
    assert is_new
    assert store.get_content(conn, doc_id) == raw
    assert store.get_content(conn, sha) == raw
    assert store.get_content(conn, sha[:12]) == raw


def test_same_content_dedups_blob_but_appends_log(conn):
    raw = b"same bytes"
    id1, sha1, new1 = store.add_bytes(conn, raw, source="s", original_name="a.pdf")
    id2, sha2, new2 = store.add_bytes(conn, raw, source="s", original_name="a.pdf")

    assert (new1, new2) == (True, False)
    assert sha1 == sha2
    assert id1 != id2
    s = store.stats(conn)
    assert s["blobs"] == 1
    assert s["documents"] == 2
    assert s["bytes"] == len(raw)


def test_media_type_guessed_from_extension(conn):
    store.add_bytes(conn, b"x", source="s", original_name="contract.pdf")
    store.add_bytes(conn, b"y", source="s", original_name="detail.CSV")
    store.add_bytes(conn, b"z", source="s", original_name="mystery.bin")
    types = {d.original_name: d.media_type for d in store.list_documents(conn)}
    assert types["contract.pdf"] == "application/pdf"
    assert types["detail.CSV"] == "text/csv"
    assert types["mystery.bin"] == "application/octet-stream"


def test_list_filters_by_source_and_orders_newest_first(conn):
    store.add_bytes(conn, b"a", source="nuro", original_name="a.pdf",
                    acquired_at="2026-08-01T00:00:00+00:00")
    store.add_bytes(conn, b"b", source="cde", original_name="b.csv",
                    acquired_at="2026-08-02T00:00:00+00:00")
    store.add_bytes(conn, b"c", source="cde", original_name="c.csv",
                    acquired_at="2026-08-03T00:00:00+00:00")

    assert [d.original_name for d in store.list_documents(conn)] == ["c.csv", "b.csv", "a.pdf"]
    assert [d.original_name for d in store.list_documents(conn, source="cde")] == ["c.csv", "b.csv"]
    assert [d.original_name for d in store.list_documents(conn, limit=1)] == ["c.csv"]


def test_add_file_reads_bytes_verbatim(conn, tmp_path):
    p = tmp_path / "契約内容.pdf"
    raw = bytes(range(256))
    p.write_bytes(raw)
    doc_id, sha, _ = store.add_file(conn, p, source="nuro_mypage", note="ご契約内容")

    assert store.get_content(conn, doc_id) == raw
    (doc,) = store.list_documents(conn)
    assert doc.original_name == "契約内容.pdf"
    assert doc.note == "ご契約内容"
    assert doc.bytes == 256


def test_missing_reference_raises(conn):
    with pytest.raises(KeyError):
        store.get_content(conn, 999)
    with pytest.raises(KeyError):
        store.get_content(conn, "deadbeef")


def test_ambiguous_sha_prefix_raises(conn):
    # 同じ接頭辞を持つ blob を 2 つ置いて、前方一致が曖昧なときに黙って
    # 片方を返さないことを確かめる
    with conn:
        conn.executemany(
            "INSERT INTO blobs(sha256, content, bytes, first_seen) VALUES (?, ?, ?, ?)",
            [("abc111", b"one", 3, "2026-08-03T00:00:00+00:00"),
             ("abc222", b"two", 3, "2026-08-03T00:00:00+00:00")],
        )
    with pytest.raises(ValueError):
        store.get_content(conn, "abc")
    assert store.get_content(conn, "abc1") == b"one"


def test_kind_defaults_to_reference_and_filters(conn):
    store.add_bytes(conn, b"a", source="sbi_sec", original_name="ALLTYPE.csv",
                    kind=store.KIND_BOOKKEEPING)
    store.add_bytes(conn, b"b", source="nuro_mypage", original_name="contract.pdf")

    (bk,) = store.list_documents(conn, kind=store.KIND_BOOKKEEPING)
    (ref,) = store.list_documents(conn, kind=store.KIND_REFERENCE)
    assert bk.original_name == "ALLTYPE.csv"
    assert ref.original_name == "contract.pdf"
    assert ref.kind == store.KIND_REFERENCE  # 既定は参照資料

    s = store.stats(conn)
    assert (s[store.KIND_BOOKKEEPING], s[store.KIND_REFERENCE]) == (1, 1)


def test_unknown_kind_rejected(conn):
    with pytest.raises(ValueError):
        store.add_bytes(conn, b"x", source="s", original_name="x.csv", kind="misc")
    assert store.stats(conn)["documents"] == 0


def test_migration_adds_kind_to_existing_db(tmp_path):
    """kind 列が無い旧 DB を開いても壊れず、既存行は reference 扱いになる。"""
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE blobs (
            sha256 TEXT PRIMARY KEY, content BLOB NOT NULL,
            bytes INTEGER NOT NULL, first_seen TEXT NOT NULL);
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, acquired_at TEXT NOT NULL, source TEXT NOT NULL,
            original_name TEXT NOT NULL, media_type TEXT NOT NULL,
            sha256 TEXT NOT NULL REFERENCES blobs(sha256),
            doc_date TEXT, note TEXT);
        INSERT INTO blobs VALUES ('ab12', X'00', 1, '2026-08-03T00:00:00+00:00');
        INSERT INTO documents(acquired_at, source, original_name, media_type, sha256)
            VALUES ('2026-08-03T00:00:00+00:00', 'old', 'legacy.pdf',
                    'application/pdf', 'ab12');
    """)
    old.commit()
    old.close()

    conn = store.connect(db)
    try:
        (doc,) = store.list_documents(conn)
        assert doc.kind == store.KIND_REFERENCE
        assert doc.original_name == "legacy.pdf"
        # 移行後も通常どおり追記できる
        store.add_bytes(conn, b"new", source="s", original_name="n.csv",
                        kind=store.KIND_BOOKKEEPING)
        assert store.stats(conn)["documents"] == 2
    finally:
        conn.close()
