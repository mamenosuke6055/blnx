"""SBI 約定履歴 (SaveFile) パーサの新旧両形式対応テスト。

2026-05 に SBI が CSV を刷新した。新形式(utf-8-sig)と旧形式(cp932)で
  (a) エンコーディング (b) ヘッダー行位置 (c) 末尾の金額列名 (d) 預り表記
が異なる。import_sbi_domestic_trade_history がそれらを吸収し、いずれも
複式仕訳 (transactions/splits/investment_transactions) を生成することを検証する。
合成データのみ使用し、残高等の PII は含まない。
"""
import csv
import sqlite3
import uuid
from pathlib import Path

import pytest

import py.importers.import_sbi_sec as sbi
from py.init.init_db import create_finance_tables


# 新形式(utf-8-sig): 末尾列『受渡金額』、預り『NISA (成長)/(つみたて)』、銘柄フルネーム、ヘッダー行=8
_NEW_HEADER = [
    "約定日", "銘柄", "銘柄コード", "市場", "取引", "期限", "預り", "課税",
    "約定数量", "約定単価", "手数料/諸経費等", "税額", "受渡日", "受渡金額",
]
_NEW_ROWS = [
    ["約定履歴"],
    [],
    ["商品指定", "約定開始年月日", "約定終了年月日", "明細数", "明細指定開始", "明細指定終了"],
    ["投資信託", "2026年04月25日", "2026年05月24日", "3", "1", "3"],
    [],
    [],
    ["（注）明細数はご指定された期間の合計です。"],
    [],
    _NEW_HEADER,
    ["2026/04/27", "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "未設定", "未設定",
     "投信金額買付", "--", "NISA (つみたて)", "--", "12", "87543", "0", "0", "2026/05/01", "100"],
    ["2026/04/27", "ニッセイＮＡＳＤＡＱ１００インデックスファンド＜購入・換金手数料なし＞", "未設定", "未設定",
     "投信金額買付", "--", "NISA (成長)", "--", "97", "25955", "0", "0", "2026/05/01", "250"],
    ["2026/04/27", "ＳＢＩ日本高配当株式（分配）ファンド（年４回決算型）", "未設定", "未設定",
     "投信金額買付", "--", "NISA (成長)", "--", "63", "15808", "0", "0", "2026/05/07", "100"],
    [],
]

# 旧形式(cp932): 末尾列『受渡金額/決済損益』、預り『 NISA(成) 』(前後空白)、銘柄切り詰め、ヘッダー行=8
_OLD_HEADER = [
    "約定日", "銘柄", "銘柄コード", "市場", "取引", "期限", "預り", "課税",
    "約定数量", "約定単価", "手数料/諸経費等", "税額", "受渡日", "受渡金額/決済損益",
]
_OLD_ROWS = [
    [],
    ["約定履歴照会 "],
    [],
    ["商品指定", "約定開始年月日", "約定終了年月日", "明細数", "明細指定開始", "明細指定終了"],
    ["すべての商品", "2024年04月08日", "2026年05月07日", "2", "1", "2"],
    [],
    ["（注）明細数はご指定された期間の合計です。"],
    [],
    _OLD_HEADER,
    ["2026/01/07", "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "", "",
     "投信金額買付", "--", " NISA(成) ", "--", "48", "83320", "--", "--", "2026/01/13", "400"],
    ["2026/01/08", "ニッセイＮＡＳＤＡＱ１００インデ＜購入・換金手数料なし＞", "", "",
     "投信金額買付", "--", " NISA(成) ", "--", "251", "23909", "--", "--", "2026/01/14", "600"],
    [],
]


def _write_csv(path: Path, rows: list[list[str]], encoding: str) -> None:
    with open(path, "w", encoding=encoding, newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "finance.db"
    conn = sqlite3.connect(db_path)
    create_finance_tables(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(sbi, "get_db_path", lambda: db_path)
    return db_path


def _inv_count(db_path):
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
    conn.close()
    return n


def _accounts_under(db_path, parent_name):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """SELECT a.name FROM accounts a JOIN accounts p ON a.parent_guid = p.guid
           WHERE p.name = ? AND a.ofx_type = 'INVESTMENT'""",
        (parent_name,),
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_new_format_utf8sig_imports_all_trades(db, tmp_path):
    """新形式(utf-8-sig / 受渡金額列 / NISA (成長))を全件取り込む。"""
    p = tmp_path / "SaveFile_000001_000048.csv"
    _write_csv(p, _NEW_ROWS, "utf-8-sig")
    sbi.import_sbi_domestic_trade_history(p)

    assert _inv_count(db) == 3
    assert _accounts_under(db, "NISAつみたて投資枠") == {"ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス"}
    # _normalize_fund_name により新長表記の銘柄名は旧短表記に統一される
    # (新旧 CSV を両方取り込んでも同一銘柄が別 account にならないため)
    assert _accounts_under(db, "NISA成長投資枠") == {
        "ニッセイＮＡＳＤＡＱ１００インデ＜購入・換金手数料なし＞",
        "ＳＢＩ日本高配当株式（分配）ファンド（年４回決算型）",
    }


def test_old_format_cp932_still_imports(db, tmp_path):
    """旧形式(cp932 / 受渡金額/決済損益列 / 前後空白付き NISA(成))の後方互換を維持。"""
    p = tmp_path / "SaveFile_000001_000217.csv"
    _write_csv(p, _OLD_ROWS, "cp932")
    sbi.import_sbi_domestic_trade_history(p)

    assert _inv_count(db) == 2
    # 前後空白付き ' NISA(成) ' も成長枠へ正規化される
    assert _accounts_under(db, "NISA成長投資枠") == {
        "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス",
        "ニッセイＮＡＳＤＡＱ１００インデ＜購入・換金手数料なし＞",
    }


def test_unknown_tx_type_does_not_rollback_others(db, tmp_path):
    """Unknown tx type (分配金再投資) は他の正常な買付の取込に影響しない。

    過去のバグ (fd 0280bc08 で発見): Unknown 時に conn.rollback() を呼んでいたが、
    commit はループ後の1回のみのため、Unknown 1件で **それまで保存した全 tx も
    巻き戻していた**。217件CSV 取込時、分配金再投資 2件が混じり、その前にあった
    投信買付の大半 (135件) が消失していた。
    """
    mixed_rows = [
        [],
        ["約定履歴照会 "],
        [],
        ["商品指定", "約定開始年月日", "約定終了年月日", "明細数", "明細指定開始", "明細指定終了"],
        ["すべての商品", "2026年01月07日", "2026年01月09日", "3", "1", "3"],
        [],
        ["（注）明細数はご指定された期間の合計です。"],
        [],
        _OLD_HEADER,
        ["2026/01/07", "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "", "",
         "投信金額買付", "--", " NISA(つ) ", "--", "48", "83320", "--", "--", "2026/01/13", "400"],
        # ↓ Unknown tx type。これで他の買付が rollback されないことを検証する
        ["2026/01/08", "ニッセイＮＡＳＤＡＱ１００インデ＜購入・換金手数料なし＞", "", "",
         "分配金再投資", "--", " NISA(成) ", "--", "10", "1000", "--", "--", "2026/01/14", "1000"],
        ["2026/01/09", "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "", "",
         "投信金額買付", "--", " NISA(つ) ", "--", "48", "83353", "--", "--", "2026/01/15", "400"],
        [],
    ]
    p = tmp_path / "SaveFile_mixed.csv"
    _write_csv(p, mixed_rows, "cp932")
    sbi.import_sbi_domestic_trade_history(p)

    # 分配金再投資はスキップされるが、前後の買付 2件は取り込まれる
    assert _inv_count(db) == 2


def test_buy_splits_balance_to_zero(db, tmp_path):
    """買付仕訳の貸借が 0 で均衡する（複式の健全性）。"""
    p = tmp_path / "SaveFile_000001_000048.csv"
    _write_csv(p, _NEW_ROWS, "utf-8-sig")
    sbi.import_sbi_domestic_trade_history(p)

    conn = sqlite3.connect(db)
    total = conn.execute("SELECT COALESCE(SUM(value_num), 0) FROM splits").fetchone()[0]
    conn.close()
    assert total == 0


def test_dynamic_header_detection_when_position_shifts(db, tmp_path):
    """ヘッダー行位置が変わっても内容で検出する（skiprows=8 固定への依存を排除）。"""
    shifted = [["約定履歴"], []] + _NEW_ROWS[8:]  # メタ行を削ってヘッダーを前方へ
    p = tmp_path / "SaveFile_shifted.csv"
    _write_csv(p, shifted, "utf-8-sig")
    sbi.import_sbi_domestic_trade_history(p)
    assert _inv_count(db) == 3


def test_content_dispatch_routes_trade_history(db, tmp_path, monkeypatch):
    """import_sbi_sec_csv が内容1行目『約定履歴』を trade_history パーサへ振り分ける。"""
    called = {}
    monkeypatch.setattr(
        sbi, "import_sbi_domestic_trade_history", lambda p: called.setdefault("p", p)
    )
    p = tmp_path / "SaveFile_000001_000048.csv"
    _write_csv(p, _NEW_ROWS, "utf-8-sig")
    sbi.import_sbi_sec_csv(p)
    assert called.get("p") == p


# ---- SELL 兼用列検証 -------------------------------------------------------
# 旧形式の 受渡金額/決済損益 列は買付時「支払額」、売却時「受取額(proceeds)」の兼用列。
# 売却 proceeds が 3-way split の Cash leg に正しく渡され、cost basis との差額が
# Capital Gain/Loss に振り分けられることを検証する。
# 実データの SaveFile に売却行が存在しない(NISA 積立のみ)ため合成データで担保する。


def _make_sell_rows(buy_units, buy_settle, sell_units, sell_settle, nisa=" NISA(成) "):
    """旧形式(cp932)の買付→売却 2行 CSV を生成する。"""
    return [
        [],
        ["約定履歴照会 "],
        [],
        ["商品指定", "約定開始年月日", "約定終了年月日", "明細数", "明細指定開始", "明細指定終了"],
        ["すべての商品", "2026年01月01日", "2026年03月01日", "2", "1", "2"],
        [],
        ["（注）明細数はご指定された期間の合計です。"],
        [],
        _OLD_HEADER,
        ["2026/01/10", "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "", "",
         "投信金額買付", "--", nisa, "--", str(buy_units), "10", "--", "--", "2026/01/14", str(buy_settle)],
        ["2026/02/10", "ｉＦｒｅｅＮＥＸＴ　ＦＡＮＧ＋インデックス", "", "",
         "投信金額売却", "--", nisa, "--", str(sell_units), "12", "--", "--", "2026/02/14", str(sell_settle)],
        [],
    ]


def test_sell_splits_balance_to_zero(db, tmp_path):
    """売却を含む 3-way split の貸借が 0 で均衡する（複式健全性）。"""
    rows = _make_sell_rows(buy_units=100, buy_settle=1000, sell_units=100, sell_settle=1200)
    p = tmp_path / "SaveFile_sell_gain.csv"
    _write_csv(p, rows, "cp932")
    sbi.import_sbi_domestic_trade_history(p)

    conn = sqlite3.connect(db)
    total = conn.execute("SELECT COALESCE(SUM(value_num), 0) FROM splits").fetchone()[0]
    conn.close()
    assert total == 0


def test_sell_gain_recorded_in_capital_gain(db, tmp_path):
    """売却益(proceeds > cost basis)が Income/Capital Gain に計上される。"""
    rows = _make_sell_rows(buy_units=100, buy_settle=1000, sell_units=100, sell_settle=1200)
    p = tmp_path / "SaveFile_sell_gain.csv"
    _write_csv(p, rows, "cp932")
    sbi.import_sbi_domestic_trade_history(p)

    conn = sqlite3.connect(db)
    gain = conn.execute(
        """SELECT SUM(s.value_num) FROM splits s
           JOIN accounts a ON a.guid = s.account_guid
           WHERE a.name = 'Capital Gain'"""
    ).fetchone()[0]
    conn.close()
    assert gain == -200  # Income credit = 負値、絶対値 = proceeds(1200) - cost(1000)


def test_sell_loss_recorded_in_capital_loss(db, tmp_path):
    """売却損(proceeds < cost basis)が Expenses/Capital Loss に計上される。"""
    rows = _make_sell_rows(buy_units=100, buy_settle=1000, sell_units=100, sell_settle=800)
    p = tmp_path / "SaveFile_sell_loss.csv"
    _write_csv(p, rows, "cp932")
    sbi.import_sbi_domestic_trade_history(p)

    conn = sqlite3.connect(db)
    loss = conn.execute(
        """SELECT SUM(s.value_num) FROM splits s
           JOIN accounts a ON a.guid = s.account_guid
           WHERE a.name = 'Capital Loss'"""
    ).fetchone()[0]
    conn.close()
    assert loss == 200  # Expense debit = 正値、絶対値 = cost(1000) - proceeds(800)


def test_sell_settle_col_is_proceeds(db, tmp_path):
    """旧形式の 受渡金額/決済損益 列の値が売却 total_amount(proceeds)として記録される。

    この列は買付/売却で同一列を兼用。売却時は受取額(proceeds)が入るため、
    investment_transactions.total_amount に proceeds がそのまま保存されることを確認する。
    """
    proceeds = 1200
    rows = _make_sell_rows(buy_units=100, buy_settle=1000, sell_units=100, sell_settle=proceeds)
    p = tmp_path / "SaveFile_sell_proceeds.csv"
    _write_csv(p, rows, "cp932")
    sbi.import_sbi_domestic_trade_history(p)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT total_amount FROM investment_transactions WHERE type = 'SELL'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == proceeds


def test_reimport_is_idempotent(db, tmp_path):
    """同一ファイルの再取込で取引が重複しない（FITID 重複排除）。"""
    p = tmp_path / "SaveFile_000001_000048.csv"
    _write_csv(p, _NEW_ROWS, "utf-8-sig")
    sbi.import_sbi_domestic_trade_history(p)
    sbi.import_sbi_domestic_trade_history(p)
    assert _inv_count(db) == 3
