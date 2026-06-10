"""SBI『保有証券一覧』(SaveFile) パーサ → asset_snapshots 橋渡しのテスト。

実 SaveFile.csv と同じセクション構造・cp932・全角/切り詰め名の組合せで、
時価が正しい既存口座へ紐付くこと、未マッチが重複口座を作らないことを検証する。
"""
import csv
import sqlite3
import uuid
from pathlib import Path

import pytest

import py.importers.import_sbi_sec as sbi
from py.init.init_db import create_finance_tables


# 実 SaveFile.csv（保有証券一覧）を模した行。NISA成長/つみたての2セクション。
_HOLDINGS_ROWS = [
    [],
    ["保有証券一覧"],
    [],
    ["投資信託（金額/NISA預り（成長投資枠））合計"],
    [],
    ["評価額合計", "評価損益合計"],
    ["78054", "+9343"],
    [],
    ["投資信託（金額/NISA預り（成長投資枠））"],
    [],
    ["ファンド名", "保有口数", "売却注文中", "取得単価", "基準価額", "取得金額", "評価額", "評価損益", "分配金受取方法"],
    ["ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "96口", "", "83334", "92919", "800", "892", "+92", "再投資"],
    ["ニッセイＮＡＳＤＡＱ１００インデックスファンド＜購入・換金手数料なし＞", "17125口", "", "23709", "27856", "40601", "47703", "+7102", "再投資"],
    ["ＳＢＩ日本高配当株式（分配）ファンド（年４回決算型）", "10734口", "", "16041", "16448", "17218", "17655", "+437", "再投資"],
    ["ＳＢＩ・Ｓ・米国高配当株式ファンド（年４回決算型）", "9768口", "", "10332", "12085", "10092", "11804", "+1712", "再投資"],
    [],
    ["投資信託（金額/NISA預り（つみたて投資枠））合計"],
    [],
    ["評価額合計", "評価損益合計"],
    ["24316", "+3766"],
    [],
    ["投資信託（金額/NISA預り（つみたて投資枠））"],
    [],
    ["ファンド名", "保有口数", "売却注文中", "取得単価", "基準価額", "取得金額", "評価額", "評価損益", "分配金受取方法"],
    ["ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "2617口", "", "78526", "92919", "20550", "24316", "+3766", "再投資"],
    [],
]


def _write_savefile(path: Path) -> None:
    with open(path, "w", encoding="cp932", newline="") as f:
        writer = csv.writer(f)
        for row in _HOLDINGS_ROWS:
            writer.writerow(row)


def _mk_account(conn, name, parent_name=None, ofx="INVESTMENT"):
    parent_guid = None
    if parent_name:
        parent_guid = conn.execute(
            "SELECT guid FROM accounts WHERE name=?", (parent_name,)
        ).fetchone()[0]
    guid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO accounts (guid, name, account_type, ofx_type, parent_guid) VALUES (?,?,?,?,?)",
        (guid, name, "ASSET", ofx, parent_guid),
    )
    return guid


@pytest.fixture
def sbi_db(tmp_path, monkeypatch):
    """旧形式由来の簿価口座をseedした一時ファイルDB。get_db_path を差し替える。"""
    db_path = tmp_path / "finance.db"
    conn = sqlite3.connect(db_path)
    create_finance_tables(conn)
    # 口座種別（中間ノード）
    _mk_account(conn, "NISA成長投資枠", ofx=None)
    _mk_account(conn, "NISAつみたて投資枠", ofx=None)
    # 既存簿価口座: NASDAQ100 は旧形式由来で切り詰め名
    ids = {
        "nasdaq": _mk_account(
            conn, "ニッセイＮＡＳＤＡＱ１００インデ＜購入・換金手数料なし＞", "NISA成長投資枠"
        ),
        "sbi_hd": _mk_account(
            conn, "ＳＢＩ日本高配当株式（分配）ファンド（年４回決算型）", "NISA成長投資枠"
        ),
        "fang_tsumitate": _mk_account(
            conn, "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "NISAつみたて投資枠"
        ),
    }
    conn.commit()
    conn.close()
    monkeypatch.setattr(sbi, "get_db_path", lambda: db_path)
    ids["db_path"] = db_path
    return ids


def _snapshots(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT account_guid, market_value_num, book_value_num FROM asset_snapshots"
    ).fetchall()
    conn.close()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_matched_accounts_get_market_value(sbi_db, tmp_path):
    csv_path = tmp_path / "SaveFile.csv"
    _write_savefile(csv_path)
    result = sbi.import_sbi_holdings_list(csv_path, snapshot_date="2026-05-24")

    assert result["matched"] == 3
    snaps = _snapshots(sbi_db["db_path"])
    assert snaps[sbi_db["nasdaq"]][0] == 47703          # 切り詰め名へ類似マッチ
    assert snaps[sbi_db["sbi_hd"]][0] == 17655          # 完全一致
    assert snaps[sbi_db["fang_tsumitate"]][0] == 24316  # つみたて枠 完全一致
    assert snaps[sbi_db["nasdaq"]][1] == 40601          # 取得金額(簿価)も記録


def test_unmatched_does_not_create_account(sbi_db, tmp_path):
    conn = sqlite3.connect(sbi_db["db_path"])
    before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    conn.close()

    csv_path = tmp_path / "SaveFile.csv"
    _write_savefile(csv_path)
    result = sbi.import_sbi_holdings_list(csv_path, snapshot_date="2026-05-24")

    conn = sqlite3.connect(sbi_db["db_path"])
    after = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    conn.close()
    assert after == before  # 未マッチでも口座を作らない

    assert len(result["unmatched"]) == 2
    funds = {u["fund_name"] for u in result["unmatched"]}
    assert any("ＦＡＮＧ" in f for f in funds)       # 成長枠FANG+ (簿価口座なし)
    assert any("米国高配当" in f for f in funds)      # ＳＢＩ・Ｓ米国 (口座なし。SBI日本へ誤マッチしない)


def test_account_type_disambiguation(sbi_db, tmp_path):
    """同名 FANG+ が口座種別で振り分く: つみたて枠にマッチ、成長枠は未マッチ。"""
    csv_path = tmp_path / "SaveFile.csv"
    _write_savefile(csv_path)
    result = sbi.import_sbi_holdings_list(csv_path, snapshot_date="2026-05-24")

    snaps = _snapshots(sbi_db["db_path"])
    assert snaps[sbi_db["fang_tsumitate"]][0] == 24316  # つみたて枠FANG+
    fang_unmatched = [u for u in result["unmatched"] if "ＦＡＮＧ" in u["fund_name"]]
    assert len(fang_unmatched) == 1
    assert fang_unmatched[0]["valuation"] == 892        # 成長枠FANG+ の時価


def test_reimport_is_idempotent(sbi_db, tmp_path):
    csv_path = tmp_path / "SaveFile.csv"
    _write_savefile(csv_path)
    sbi.import_sbi_holdings_list(csv_path, snapshot_date="2026-05-24")
    result2 = sbi.import_sbi_holdings_list(csv_path, snapshot_date="2026-05-24")

    assert result2["matched"] == 0
    assert result2["skipped"] == 3
    assert len(_snapshots(sbi_db["db_path"])) == 3      # 重複行は増えない


def test_content_based_dispatch_routes_to_holdings(sbi_db, tmp_path, monkeypatch):
    """import_sbi_sec_csv が内容1行目『保有証券一覧』で holdings パーサへ振り分ける。"""
    called = {}
    monkeypatch.setattr(sbi, "import_sbi_holdings_list", lambda p: called.setdefault("p", p))
    csv_path = tmp_path / "SaveFile.csv"
    _write_savefile(csv_path)
    sbi.import_sbi_sec_csv(csv_path)
    assert called.get("p") == csv_path
