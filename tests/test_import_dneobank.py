"""住信SBIネット銀行 importer の回帰テスト。

最頻の取込経路(archive 48 件中 21 件)でありながらテストが無かった。対象は
口座同定(通貨・相手口座名・残高連鎖)・保留時の取込拒否・冪等性・USD の桁。
"""
import sqlite3

import pytest

from py.init.init_db import create_finance_tables
from py.importers import account_identity as ai
from py.importers.account_identity import AccountUndetermined
from py.importers.import_dneobank import import_dneobank_csv

JPY_HEADER = '"日付","内容","出金金額(円)","入金金額(円)","残高(円)","メモ"\n'
USD_HEADER = '"日付","内容","出金金額(USD)","入金金額(USD)","残高(USD)","メモ"\n'


def _csv(path, header, rows):
    """rows は CSV 掲載順(新しい順)の (日付, 内容, 出金, 入金, 残高)。"""
    body = "".join(f'"{d}","{c}","{o}",{f'"{i}"' if i else ""},"{b}","-"\n'
                   for d, c, o, i, b in rows)
    path.write_text(header + body, encoding="cp932")
    return path


@pytest.fixture
def dbs(tmp_path):
    fin = tmp_path / "finance.db"
    c = sqlite3.connect(fin)
    create_finance_tables(c)
    c.commit()
    c.close()
    raw = tmp_path / "raw.db"
    rc = sqlite3.connect(raw)
    rc.executescript(
        "CREATE TABLE raw_imports (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT,"
        " filename TEXT, sha256 TEXT UNIQUE, content BLOB);")
    ai.init_identity_tables(rc)
    rc.commit()
    rc.close()
    return str(fin), str(raw)


def _archive(raw_db, path, source="dneobank"):
    import hashlib
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    c = sqlite3.connect(raw_db)
    c.execute("INSERT OR IGNORE INTO raw_imports (source, filename, sha256, content)"
              " VALUES (?,?,?,?)", (source, path.name, sha, data))
    c.commit()
    c.close()
    return sha


def _bal(fin_db, name):
    c = sqlite3.connect(fin_db)
    v = c.execute(
        "SELECT COALESCE(SUM(s.value_num*1.0/s.value_denom),0) FROM splits s"
        " JOIN accounts a ON a.guid=s.account_guid WHERE a.name=?", (name,)).fetchone()[0]
    c.close()
    return v


# ------------------------------------------------------------------ 口座同定

def test_usd_file_goes_to_foreign_currency_account(dbs, tmp_path):
    """USD 建てファイルは通貨ヘッダだけで外貨預金と分かる($75 を ¥75 にしない)。"""
    fin, raw = dbs
    p = _csv(tmp_path / "usd.csv", USD_HEADER,
             [("2026/06/11", "振替　ＳＢＩ証券", "75.00", "", "0.00"),
              ("2026/06/11", "普通　円　代表口座", "", "75.00", "75.00")])
    _archive(raw, p)
    import_dneobank_csv(p, db_path=fin, raw_db_path=raw)
    assert _bal(fin, "SBI Sumishin Foreign Currency - USD") == pytest.approx(-75.0 + 75.0)
    c = sqlite3.connect(fin)
    denoms = {r[0] for r in c.execute(
        "SELECT value_denom FROM splits s JOIN accounts a ON a.guid=s.account_guid"
        " WHERE a.name LIKE '%Foreign Currency%'")}
    c.close()
    assert denoms == {100}, "USD は 1/100 単位で持つ(円と同じ分母1にしない)"


def test_counterparty_name_excludes_own_account(dbs, tmp_path):
    """内容が相手口座を名指しする → 自分はその口座ではない(排除法)。"""
    fin, raw = dbs
    p = _csv(tmp_path / "a.csv", JPY_HEADER,
             [("2026/04/23", "普通　円　代表口座", "5,038", "", "0"),
              ("2026/04/19", "ＳＢＩハイブリッド預金", "5,000", "", "5,038")])
    rc = sqlite3.connect(raw)
    parsed = ai.parse_dneobank(p.read_text(encoding="cp932"))
    ident = ai.identify(rc, parsed)
    rc.close()
    # 代表(円)とハイブリッドが相手として現れる → 残るのは目的別だけ
    assert ident.account_key == "dneobank_mokuteki_seikatsuboei"
    assert "相手口座名" in ident.reason


def test_balance_chain_identifies_second_file(dbs, tmp_path):
    """1 本目を人が宣言 → 2 本目は残高連鎖で自動同定される。"""
    fin, raw = dbs
    first = _csv(tmp_path / "1.csv", JPY_HEADER,
                 [("2026/06/04", "ＡＴＭ　セブン銀行", "5,000", "", "95,000"),
                  ("2026/06/01", "給与＊テスト", "", "100,000", "100,000")])
    sha1 = _archive(raw, first)
    rc = sqlite3.connect(raw)
    ai.declare(rc, sha1, "dneobank_main_jpy", origin="human", evidence="初回宣言")
    rc.close()
    import_dneobank_csv(first, db_path=fin, raw_db_path=raw, account_key="dneobank_main_jpy")
    second = _csv(tmp_path / "2.csv", JPY_HEADER,
                  [("2026/06/10", "口座振替　テスト", "1,000", "", "94,000"),
                   ("2026/06/04", "ＡＴＭ　セブン銀行", "5,000", "", "95,000")])
    _archive(raw, second)
    import_dneobank_csv(second, db_path=fin, raw_db_path=raw)   # 宣言なし → 同定させる
    # 期間の重なる 6/04 行は再取込されない(冪等)。100,000 - 5,000 - 1,000
    assert _bal(fin, "SBI Sumishin Net Bank") == pytest.approx(94_000.0)


def test_undetermined_is_rejected_not_guessed(dbs, tmp_path):
    """判定できないファイルは代表口座へ流し込まず取込拒否する。"""
    fin, raw = dbs
    p = _csv(tmp_path / "orphan.csv", JPY_HEADER,
             [("2026/07/01", "振替　ＳＢＩ証券", "435", "", "12,123")])
    _archive(raw, p)
    with pytest.raises(AccountUndetermined):
        import_dneobank_csv(p, db_path=fin, raw_db_path=raw)
    assert _bal(fin, "SBI Sumishin Net Bank") == 0, "拒否したのに記帳されている"


def test_declaration_cancel_is_append_only(dbs, tmp_path):
    """宣言の取消は上書きでなく追記(fossil の cancel タグ)。"""
    fin, raw = dbs
    p = _csv(tmp_path / "x.csv", JPY_HEADER, [("2026/06/01", "利息", "", "3", "100")])
    sha = _archive(raw, p)
    rc = sqlite3.connect(raw)
    ai.declare(rc, sha, "dneobank_main_jpy")
    assert ai.effective_declarations(rc)[sha] == "dneobank_main_jpy"
    ai.cancel(rc, sha, "dneobank_main_jpy", evidence="誤り")
    assert sha not in ai.effective_declarations(rc)
    n = rc.execute("SELECT COUNT(*) FROM account_declarations WHERE raw_sha256=?",
                   (sha,)).fetchone()[0]
    rc.close()
    assert n == 2, "取消で行が消えている(append-only でない)"


# -------------------------------------------------------------------- 冪等性

def test_reimport_is_idempotent(dbs, tmp_path):
    fin, raw = dbs
    first = _csv(tmp_path / "1.csv", JPY_HEADER,
                 [("2026/06/01", "給与＊テスト", "", "100,000", "100,000")])
    sha1 = _archive(raw, first)
    rc = sqlite3.connect(raw)
    ai.declare(rc, sha1, "dneobank_main_jpy", origin="human")
    rc.close()
    import_dneobank_csv(first, db_path=fin, raw_db_path=raw, account_key="dneobank_main_jpy")
    import_dneobank_csv(first, db_path=fin, raw_db_path=raw, account_key="dneobank_main_jpy")
    c = sqlite3.connect(fin)
    n = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    c.close()
    assert n == 1


def test_same_day_same_amount_rows_are_distinct(dbs, tmp_path):
    """同日・同摘要・同額でも別イベントならファイル内連番で区別する。"""
    fin, raw = dbs
    p = _csv(tmp_path / "dup.csv", JPY_HEADER,
             [("2026/06/03", "振替　ＳＢＩ証券", "500", "", "9,000"),
              ("2026/06/03", "振替　ＳＢＩ証券", "500", "", "9,500")])
    _archive(raw, p)
    import_dneobank_csv(p, db_path=fin, raw_db_path=raw, account_key="dneobank_hybrid")
    c = sqlite3.connect(fin)
    n = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    c.close()
    assert n == 2, "同額同摘要の 2 行が 1 件に潰れている"


# ------------------------------------------------------ 出金側の分類(fd fc45d9de3b2a)

def _peer_of(fin_db, description):
    """description の取引で、銀行口座でない側の勘定パスを返す。"""
    c = sqlite3.connect(fin_db)
    rows = c.execute(
        "SELECT a.name, p.name FROM splits s"
        " JOIN transactions t ON t.guid = s.tx_guid"
        " JOIN accounts a ON a.guid = s.account_guid"
        " LEFT JOIN accounts p ON p.guid = a.parent_guid"
        " WHERE t.description = ?", (description,)).fetchall()
    c.close()
    return {f"{parent}:{name}" for name, parent in rows}


def test_outflow_self_move_goes_to_transfer(dbs, tmp_path):
    """証券への振替は費用ではない。Expenses:Uncategorized に落とさない。

    入金側だけが Assets:Transfer へ付け替えられていたため清算勘定が片肺になり、
    実データで残差が -403,675 まで開いていた(fd fc45d9de3b2a / 4c39128ddc06)。
    """
    fin, raw = dbs
    p = _csv(tmp_path / "out.csv", JPY_HEADER,
             [("2026/06/03", "振替　ＳＢＩ証券", "435", "", "9,565")])
    _archive(raw, p)
    import_dneobank_csv(p, db_path=fin, raw_db_path=raw, account_key="dneobank_hybrid")
    peers = _peer_of(fin, "振替　ＳＢＩ証券")
    assert "Assets:Transfer" in peers
    assert "Expenses:Uncategorized" not in peers


def test_outflow_unknown_stays_uncategorized(dbs, tmp_path):
    """判定できない出金は従来どおり費用に保留する(人間レビュー用)。"""
    fin, raw = dbs
    p = _csv(tmp_path / "out2.csv", JPY_HEADER,
             [("2026/06/03", "口座振替　テストデンリヨク", "3,000", "", "7,000")])
    _archive(raw, p)
    import_dneobank_csv(p, db_path=fin, raw_db_path=raw, account_key="dneobank_hybrid")
    assert "Expenses:Uncategorized" in _peer_of(fin, "口座振替　テストデンリヨク")


def test_outflow_card_payment_still_wins(dbs, tmp_path):
    """カード引落は負債の減少。自己振替判定より先に評価される。"""
    fin, raw = dbs
    p = _csv(tmp_path / "out3.csv", JPY_HEADER,
             [("2026/06/27", "楽天カードサービス", "180,000", "", "20,000")])
    _archive(raw, p)
    import_dneobank_csv(p, db_path=fin, raw_db_path=raw, account_key="dneobank_main_jpy")
    peers = _peer_of(fin, "楽天カードサービス")
    assert any("Rakuten Card" in x for x in peers)
    assert "Expenses:Uncategorized" not in peers
