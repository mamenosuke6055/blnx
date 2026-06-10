"""
SBI証券 円貨入出金明細インポーター + JPY_clearing ゼロ近傍不変条件のテスト。

検証する性質:
  - Hybrid 自動振替エントリ（「ハイブリッド預金より/へ」）はスキップ（二重計上防止）
  - それ以外（即時入金・定時定額買付金・源泉徴収）は JPY_clearing へ取込
  - 2回実行しても重複しない（冪等性）
  - 清算口座残高不変条件: 買付出金・Hybrid振替入金・非Hybrid入金が揃った状態で残高≈0
"""
import csv
import io
import sqlite3
import uuid
from pathlib import Path

import pytest

import py.importers.import_sbi_sec as sbi
from py.init.init_db import create_finance_tables


# ── フィクスチャ ──────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "finance.db"
    conn = sqlite3.connect(db_path)
    create_finance_tables(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(sbi, "get_db_path", lambda: db_path)
    return db_path


# ── CSV 生成ヘルパー ──────────────────────────────────────────────────────

def _make_detail_inquiry_csv(rows: list[dict], path: Path) -> None:
    """
    DetailInquiry_*.csv（円貨入出金明細, UTF-8 BOM）を合成する。
    rows の各要素: {date, desc, out, inn} (int型, 0 = 空欄)
    """
    header_lines = [
        "",
        "円貨入出金明細",
        "",
        "指定期間,指定期間(開始),指定期間(終了),スィープ専用銀行口座 明細表示,指定取引区分,明細数",
        f'"期間指定","2025/01/01","2026/06/04","あり","入金：すべて、出金：すべて","{len(rows)}"',
        "出金額合計,うち振替出金,入金額合計,うち振替入金",
        '"0","0","0","0"',
    ]
    data_header = "入出金日,取引,区分,摘要,出金額,入金額"
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        for line in header_lines:
            f.write(line + "\n")
        f.write(data_header + "\n")
        for r in rows:
            out = str(r["out"]) if r["out"] else ""
            inn = str(r["inn"]) if r["inn"] else ""
            f.write(f'"{r["date"]}","入金","金融機関からの入金","{r["desc"]}","{out}","{inn}"\n')


def _jpy_clearing_balance(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    bal = conn.execute(
        """SELECT COALESCE(SUM(s.value_num * 1.0 / s.value_denom), 0)
           FROM splits s
           JOIN accounts a ON a.guid = s.account_guid
           WHERE a.name = 'JPY_clearing'"""
    ).fetchone()[0]
    conn.close()
    return int(bal)


def _tx_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.close()
    return n


# ── Hybrid エントリのスキップ ────────────────────────────────────────────

def test_skips_hybrid_inflow(db, tmp_path):
    """「ハイブリッド預金より自動振替入金」は realign 済みとしてスキップされる。"""
    p = tmp_path / "DetailInquiry_test.csv"
    _make_detail_inquiry_csv([
        {"date": "2026/03/01", "desc": "SBIハイブリッド預金より自動振替入金", "out": 0, "inn": 1000},
        {"date": "2026/03/02", "desc": "SBIハイブリッド預金より自動振替入金", "out": 0, "inn": 950},
    ], p)
    sbi.import_sbi_jpy_deposit_withdrawal(p)
    assert _tx_count(db) == 0


def test_skips_hybrid_outflow(db, tmp_path):
    """「ハイブリッド預金へ自動振替出金」もスキップされる。"""
    p = tmp_path / "DetailInquiry_test.csv"
    _make_detail_inquiry_csv([
        {"date": "2026/02/25", "desc": "SBIハイブリッド預金へ自動振替出金", "out": 26400, "inn": 0},
    ], p)
    sbi.import_sbi_jpy_deposit_withdrawal(p)
    assert _tx_count(db) == 0


# ── 非 Hybrid エントリの取込 ──────────────────────────────────────────────

def test_imports_non_hybrid_entries(db, tmp_path):
    """Hybrid 以外のエントリ（即時入金・定時定額・源泉徴収）が JPY_clearing に取込まれる。"""
    p = tmp_path / "DetailInquiry_test.csv"
    _make_detail_inquiry_csv([
        {"date": "2026/06/04", "desc": "SBIハイブリッド預金より自動振替入金", "out": 0, "inn": 100},   # スキップ
        {"date": "2025/06/27", "desc": "即時入金　楽天銀行",              "out": 0, "inn": 5000},  # 取込
        {"date": "2026/03/01", "desc": "定時定額買付金の入金",            "out": 0, "inn": 27600}, # 取込
        {"date": "2026/01/07", "desc": "譲渡益税源泉徴収金",              "out": 71, "inn": 0},    # 取込
    ], p)
    sbi.import_sbi_jpy_deposit_withdrawal(p)

    assert _tx_count(db) == 3  # Hybrid 1件スキップ、残り3件

    conn = sqlite3.connect(db)
    # JPY_clearing の入出金合計を確認
    rows = conn.execute(
        """SELECT ROUND(SUM(s.value_num * 1.0 / s.value_denom))
           FROM splits s
           JOIN accounts a ON a.guid = s.account_guid
           WHERE a.name = 'JPY_clearing'"""
    ).fetchone()
    conn.close()
    # 即時入金 +5000 + 定時定額 +27600 + 源泉徴収 -71 = +32529
    assert rows[0] == 32529


def test_imports_rakuten_bank_entry(db, tmp_path):
    """「即時入金　楽天銀行」は Rakuten Bank を対向として取込まれる。"""
    p = tmp_path / "DetailInquiry_test.csv"
    _make_detail_inquiry_csv([
        {"date": "2025/06/27", "desc": "即時入金　楽天銀行", "out": 0, "inn": 5000},
    ], p)
    sbi.import_sbi_jpy_deposit_withdrawal(p)

    conn = sqlite3.connect(db)
    row = conn.execute(
        """SELECT a.name
           FROM transactions t
           JOIN splits s ON s.tx_guid = t.guid
           JOIN accounts a ON a.guid = s.account_guid
           WHERE t.description = '即時入金　楽天銀行' AND s.value_num < 0"""
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "Rakuten Bank"


# ── 冪等性 ───────────────────────────────────────────────────────────────

def test_idempotent(db, tmp_path):
    """同じファイルを2回インポートしても重複しない。"""
    p = tmp_path / "DetailInquiry_test.csv"
    _make_detail_inquiry_csv([
        {"date": "2025/06/27", "desc": "即時入金　楽天銀行", "out": 0, "inn": 5000},
        {"date": "2026/03/01", "desc": "定時定額買付金の入金", "out": 0, "inn": 27600},
    ], p)
    sbi.import_sbi_jpy_deposit_withdrawal(p)
    count_first = _tx_count(db)
    sbi.import_sbi_jpy_deposit_withdrawal(p)
    assert _tx_count(db) == count_first


# ── JPY_clearing ゼロ近傍不変条件 ────────────────────────────────────────

def _setup_clearing_scenario(db_path: Path, monkeypatch) -> None:
    """
    JPY_clearing がゼロに収束するべきシナリオをセットアップする。

    取込が揃った状態:
      - SaveFile 由来の買付出金:   JPY_clearing → 投信  -30,000
      - Hybrid realign 由来の入金: Hybrid       → JPY_clearing  +27,600
      - DetailInquiry 非Hybrid:    楽天銀行     → JPY_clearing   +2,400
    合計: -30,000 + 27,600 + 2,400 = 0
    """
    conn = sqlite3.connect(db_path)
    monkeypatch.setattr(sbi, "get_db_path", lambda: db_path)

    def _guid():
        return uuid.uuid4().hex

    def _acct(name, atype="ASSET", parent_guid=None):
        g = _guid()
        conn.execute(
            "INSERT INTO accounts (guid, name, account_type, parent_guid) VALUES (?,?,?,?)",
            (g, name, atype, parent_guid),
        )
        return g

    def _tx(date, splits, desc=""):
        tg = _guid()
        conn.execute(
            "INSERT INTO transactions (guid, post_date, description) VALUES (?,?,?)",
            (tg, date, desc),
        )
        for acc, val in splits:
            conn.execute(
                "INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom,"
                " quantity_num, quantity_denom) VALUES (?,?,?,?,1,?,1)",
                (_guid(), tg, acc, val, val),
            )

    # 口座ツリー
    assets  = _acct("Assets")
    bank    = _acct("Bank",         parent_guid=assets)
    sbi_sec = _acct("SBI Securities", parent_guid=bank)
    clr     = _acct("JPY_clearing",   parent_guid=sbi_sec)
    inv     = _acct("Investments",  parent_guid=assets)
    fund    = _acct("FundA",         parent_guid=inv)
    hybrid  = _acct("SBI Sumishin Hybrid Deposit", parent_guid=bank)

    # SaveFile 由来の買付 (JPY_clearing -30000 / FundA +30000)
    _tx("2026-03-01", [(clr, -30000), (fund, 30000)], "投信金額買付 FundA")

    # Hybrid realign 済みの振替 (Hybrid -27600 / JPY_clearing +27600)
    _tx("2026-03-01", [(hybrid, -27600), (clr, 27600)], "振替　ＳＢＩ証券")

    conn.commit()
    conn.close()
    return clr  # JPY_clearing の guid (検証用)


def test_jpy_clearing_zero_invariant_after_full_import(db, tmp_path, monkeypatch):
    """
    買付出金 + Hybrid振替入金 + DetailInquiry非Hybrid入金 がそろうと
    JPY_clearing 残高はゼロになる。
    """
    _setup_clearing_scenario(db, monkeypatch)
    # DetailInquiry 取込前は -2400（買付 -30000 + 振替 +27600 = -2400）
    assert _jpy_clearing_balance(db) == -2400

    # DetailInquiry で残り +2400 を入金
    p = tmp_path / "DetailInquiry_test.csv"
    _make_detail_inquiry_csv([
        {"date": "2026/03/01", "desc": "定時定額買付金の入金", "out": 0, "inn": 2400},
    ], p)
    sbi.import_sbi_jpy_deposit_withdrawal(p)

    assert _jpy_clearing_balance(db) == 0


def test_jpy_clearing_hybrid_skip_prevents_double_credit(db, tmp_path, monkeypatch):
    """
    DetailInquiry に Hybrid 振替入金エントリが含まれていても、
    スキップにより JPY_clearing に二重計上されない。
    """
    _setup_clearing_scenario(db, monkeypatch)
    balance_before = _jpy_clearing_balance(db)  # -2400

    # DetailInquiry に「Hybrid 振替」エントリだけ (= スキップされるべき)
    p = tmp_path / "DetailInquiry_test.csv"
    _make_detail_inquiry_csv([
        {"date": "2026/03/01", "desc": "SBIハイブリッド預金より自動振替入金", "out": 0, "inn": 27600},
    ], p)
    sbi.import_sbi_jpy_deposit_withdrawal(p)

    # スキップされるので残高不変
    assert _jpy_clearing_balance(db) == balance_before
