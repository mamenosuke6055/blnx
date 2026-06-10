"""取込ゲート(py/importers/import_gate.py)のテスト。

period 抽出・期間重複判定・パイプライン統合での「上申」検知を検証する。
"""
import sqlite3

import pytest

from py.init.init_db import create_finance_tables
from py.importers import raw_archive
from py.importers import inbox_pipeline
from py.importers import import_gate


CARD_CSV = (
    "利用日,利用店名・商品名,利用金額\n"
    "2025/06/01,AAA,100\n"
    "2025/06/15,BBB,200\n"
)
BANK_CSV = (
    "取引日,入出金(円),入出金内容\n"
    "20250601,1000,test\n"
    "20250620,-500,test2\n"
)


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


# --- extract_period ---

def test_extract_period_rakuten_card(tmp_path):
    f = tmp_path / "enavi.csv"
    f.write_text(CARD_CSV, encoding="utf-8")
    assert import_gate.extract_period(f, "rakuten_card") == ("2025-06-01", "2025-06-15")


def test_extract_period_rakuten_bank(tmp_path):
    f = tmp_path / "rb.csv"
    f.write_text(BANK_CSV, encoding="utf-8")
    assert import_gate.extract_period(f, "rakuten_bank") == ("2025-06-01", "2025-06-20")


def test_extract_period_unsupported_source_returns_none(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text(CARD_CSV, encoding="utf-8")
    assert import_gate.extract_period(f, "sbi_sec") is None


# --- find_period_overlaps ---

def _arch(raw_conn, fname, source, ps, pe):
    ar = raw_archive.archive_bytes(raw_conn, fname.encode(), fname, source=source)
    raw_archive.update_import_meta(raw_conn, ar.row_id, period_start=ps, period_end=pe)
    return ar.row_id


def test_find_overlaps_detects_same_period(raw_conn):
    _arch(raw_conn, "a.csv", "rakuten_card", "2025-06-01", "2025-06-30")
    hits = import_gate.find_period_overlaps(raw_conn, "rakuten_card", "2025-06-10", "2025-07-05")
    assert len(hits) == 1
    assert hits[0]["filename"] == "a.csv"


def test_find_overlaps_ignores_disjoint(raw_conn):
    _arch(raw_conn, "a.csv", "rakuten_card", "2025-06-01", "2025-06-30")
    hits = import_gate.find_period_overlaps(raw_conn, "rakuten_card", "2025-07-01", "2025-07-31")
    assert hits == []


def test_find_overlaps_respects_source_and_exclude(raw_conn):
    rid = _arch(raw_conn, "a.csv", "rakuten_card", "2025-06-01", "2025-06-30")
    _arch(raw_conn, "b.csv", "rakuten_bank", "2025-06-01", "2025-06-30")
    # 別 source は対象外
    assert import_gate.find_period_overlaps(raw_conn, "rakuten_card", "2025-06-05", "2025-06-20") \
        and len(import_gate.find_period_overlaps(raw_conn, "rakuten_card", "2025-06-05", "2025-06-20")) == 1
    # 自分自身は exclude_id で除外
    assert import_gate.find_period_overlaps(
        raw_conn, "rakuten_card", "2025-06-05", "2025-06-20", exclude_id=rid) == []


def test_find_overlaps_skips_null_period(raw_conn):
    raw_archive.archive_bytes(raw_conn, b"x", "noperiod.csv", source="rakuten_card")  # period 未充填
    assert import_gate.find_period_overlaps(raw_conn, "rakuten_card", "2025-06-01", "2025-06-30") == []


# --- パイプライン統合: 同一期間・別フォーマット再取込で上申 ---

def test_pipeline_flags_period_overlap(monkeypatch, finance_db, raw_conn, tmp_path):
    monkeypatch.setattr(inbox_pipeline, "_import_one", lambda *a, **k: None)
    a = tmp_path / "enavi202506.csv"
    a.write_text(CARD_CSV, encoding="utf-8")
    b = tmp_path / "enavi202506_v2.csv"  # 同期間・別内容(別sha)
    b.write_text(CARD_CSV + "2025/06/20,CCC,300\n", encoding="utf-8")

    ra = inbox_pipeline.process_inbox_file(a, finance_db, raw_conn)
    rb = inbox_pipeline.process_inbox_file(b, finance_db, raw_conn)

    assert ra["period"] == ("2025-06-01", "2025-06-15")
    assert ra["period_overlap"] == []          # 先頭は重複なし
    assert rb["period"] == ("2025-06-01", "2025-06-20")
    assert len(rb["period_overlap"]) == 1       # A と期間重複を上申
    assert rb["period_overlap"][0]["filename"] == "enavi202506.csv"
    # period がアーカイブに充填されている
    meta = {m["filename"]: m for m in raw_archive.list_archived(raw_conn)}
    assert meta["enavi202506.csv"]["period_start"] == "2025-06-01"
    assert meta["enavi202506.csv"]["period_end"] == "2025-06-15"


def test_pipeline_redownload_same_file_no_overlap(monkeypatch, finance_db, raw_conn, tmp_path):
    # 完全同一(sha一致)の再DLは duplicate_file 扱いで overlap 上申しない
    monkeypatch.setattr(inbox_pipeline, "_import_one", lambda *a, **k: None)
    a = tmp_path / "enavi202506.csv"; a.write_text(CARD_CSV, encoding="utf-8")
    b = tmp_path / "enavi202506_again.csv"; b.write_text(CARD_CSV, encoding="utf-8")  # 同内容

    inbox_pipeline.process_inbox_file(a, finance_db, raw_conn)
    rb = inbox_pipeline.process_inbox_file(b, finance_db, raw_conn)

    assert rb["duplicate_file"] is True
    assert rb["period_overlap"] == []
