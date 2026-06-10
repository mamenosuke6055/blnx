"""detect_source のファイル名・コンテンツ判定テスト。

今回追加した変更を中心に検証:
- savefile パターン拡張（番号なし/スペース入りバリアント）
- SBI コンテンツフォールバック（保有証券一覧/約定履歴/円貨入出金明細等）
"""
from pathlib import Path

import pytest

from py.importers.detect_source import detect_source


def _write(tmp_path: Path, name: str, content: str, encoding: str = "utf-8") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding=encoding)
    return p


# ---- ファイル名パターン: savefile 拡張 ----

def test_savefile_numbered(tmp_path):
    """SaveFile_000001_000048.csv（連番付き）を sbi_sec と判定する。"""
    p = _write(tmp_path, "SaveFile_000001_000048.csv", "dummy")
    assert detect_source(p) == "sbi_sec"


def test_savefile_no_number(tmp_path):
    """SaveFile.csv（番号なし）を sbi_sec と判定する。"""
    p = _write(tmp_path, "SaveFile.csv", "dummy")
    assert detect_source(p) == "sbi_sec"


def test_savefile_with_space_and_paren(tmp_path):
    """SaveFile (2).csv（スペース+カッコ付き）を sbi_sec と判定する。"""
    p = _write(tmp_path, "SaveFile (2).csv", "dummy")
    assert detect_source(p) == "sbi_sec"


# ---- コンテンツフォールバック: SBI 救済 ----

def test_content_sbi_holdings_list(tmp_path):
    """先頭行に「保有証券一覧」を含む場合 sbi_sec と判定する。"""
    content = "\n保有証券一覧\n\n口座種別,銘柄名,数量\n"
    p = _write(tmp_path, "unknown_sbi.csv", content, encoding="cp932")
    assert detect_source(p) == "sbi_sec"


def test_content_sbi_trade_history(tmp_path):
    """先頭行に「約定履歴」を含む場合 sbi_sec と判定する。"""
    content = "約定履歴\n\n約定日,銘柄,取引\n"
    p = _write(tmp_path, "unknown_sbi.csv", content, encoding="cp932")
    assert detect_source(p) == "sbi_sec"


def test_content_sbi_jpy_deposit(tmp_path):
    """先頭行に「円貨入出金明細」を含む場合 sbi_sec と判定する。"""
    content = "\n円貨入出金明細\n\n受付日時,取引\n"
    p = _write(tmp_path, "DetailInquiry_20260604.csv", content, encoding="utf-8-sig")
    assert detect_source(p) == "sbi_sec"


def test_content_sbi_jpy_operation(tmp_path):
    """先頭行に「円貨操作履歴」を含む場合 sbi_sec と判定する。"""
    content = "\n円貨操作履歴\n\n受付日時,取引\n"
    p = _write(tmp_path, "SaveFile (4).csv", content, encoding="cp932")
    assert detect_source(p) == "sbi_sec"


# ---- 既存パターンが壊れていないことの回帰確認 ----

def test_existing_rakuten_card_filename(tmp_path):
    """enavi ファイル名パターンが引き続き rakuten_card を返す。"""
    p = _write(tmp_path, "enavi202506(1324).csv", "dummy")
    assert detect_source(p) == "rakuten_card"


def test_existing_dneobank_filename(tmp_path):
    """nyushukinmeisai パターンが引き続き dneobank を返す。"""
    p = _write(tmp_path, "nyushukinmeisai_20260604.csv", "dummy")
    assert detect_source(p) == "dneobank"


def test_unknown_returns_none(tmp_path):
    """判定不能ファイルは None を返す。"""
    p = _write(tmp_path, "summaryAll20260604.csv",
               '"口座種別","評価金額 (円)"\n"累計","90775"\n')
    assert detect_source(p) is None
