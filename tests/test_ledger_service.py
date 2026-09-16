"""記帳サービス(py/importers/ledger.py)のテスト。

過去に実データで起きた事故を、そのまま再現できないことを確かめる:
  - 取込設定(prefix)で鍵が変わる → 同じ CSV の再取込が二重計上(住信SBI ¥126,721)
  - 鍵を落とす → 冪等チェックをすり抜ける(楽天証券 再投資 7 件)
  - 墓標を見ない → 統合・削除した仕訳が再取込で復活する(従来 dneobank だけが見ていた)
  - 口数の分母を落とす → 保有口数が壊れる
"""
import sqlite3

import pytest

from py.importers import ledger
from py.importers.ledger import Entry, Posting
from py.init.init_db import create_finance_tables


@pytest.fixture
def db():
    c = sqlite3.connect(":memory:")
    create_finance_tables(c)
    c.execute("INSERT INTO accounts (guid, name, account_type) VALUES ('a1','代表口座','ASSET')")
    c.execute("INSERT INTO accounts (guid, name, account_type) VALUES ('a2','食費','EXPENSE')")
    c.commit()
    yield c
    c.close()


def _posting(**over):
    base = dict(
        source="dneobank", account_key="main_jpy", date="2026-05-27",
        description="口座振替　楽天カードサービス", amount=-153802.0, balance=84261.0, seq=3,
        entries=(Entry("a2", 153802), Entry("a1", -153802)),
    )
    base.update(over)
    return Posting(**base)


def _count(db):
    return db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


# --- 鍵の決まり方 -------------------------------------------------------

def test_key_is_determined_by_content_only():
    """同じ業務イベントなら、いつ・どの順で取り込んでも同じ鍵になる。"""
    assert ledger.natural_key(_posting()) == ledger.natural_key(_posting())


def test_key_separates_same_day_same_amount_rows():
    """同日・同摘要・同額の正当な 2 行は、残高と連番で分かれる(過少計上を防ぐ)。"""
    a = _posting(balance=1000.0, seq=1)
    b = _posting(balance=550.0, seq=2)
    assert ledger.natural_key(a) != ledger.natural_key(b)


def test_key_separates_accounts():
    """同じ CSV 様式でも口座が違えば別の鍵(口座混入の再発防止)。"""
    assert ledger.natural_key(_posting(account_key="main_jpy")) != \
        ledger.natural_key(_posting(account_key="hybrid"))


def test_description_whitespace_is_normalized():
    assert ledger.natural_key(_posting(description="振替　 ＳＢＩ証券")) == \
        ledger.natural_key(_posting(description="振替　ＳＢＩ証券"))


# --- 冪等 ---------------------------------------------------------------

def test_second_post_of_same_event_is_skipped(db):
    assert ledger.post(db, _posting()) is not None
    assert ledger.post(db, _posting()) is None
    assert _count(db) == 1


def test_legacy_v1_key_blocks_reposting(db):
    """v1 で既に入っている行は、v2 の鍵が違っても二重計上にしない(移行期の共存)。"""
    db.execute("INSERT INTO transactions (guid, post_date, description, ofx_fitid)"
               " VALUES ('old','2026-05-27','口座振替　楽天カードサービス','SHA256:v1key')")
    assert ledger.post(db, _posting(legacy_fitids=("SHA256:v1key",))) is None
    assert _count(db) == 1


def test_entombed_key_is_not_resurrected(db):
    """統合・削除した鍵は再取込で復活しない(従来 dneobank だけが見ていた墓標)。"""
    key = ledger.fitid_of(ledger.natural_key(_posting()))
    ledger.entomb(db, key, None, "テスト: 除去済み")
    assert ledger.post(db, _posting()) is None
    assert _count(db) == 0


def test_db_rejects_duplicate_natural_key(db):
    """サービスを通さず書いても、DB の UNIQUE が同じ自然キーを弾く。"""
    ledger.post(db, _posting())
    key = ledger.natural_key(_posting())
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO transactions (guid, post_date, description, ofx_fitid,"
                   " fitid_policy, natural_key) VALUES ('x','2026-05-27','別','SHA256:other','v2',?)",
                   (key,))


# --- 形の検算 -----------------------------------------------------------

def test_unbalanced_posting_is_refused(db):
    with pytest.raises(ValueError, match="借貸が合わない"):
        ledger.post(db, _posting(entries=(Entry("a2", 153802), Entry("a1", -150000))))
    assert _count(db) == 0


def test_single_entry_posting_is_refused(db):
    with pytest.raises(ValueError, match="複式にならない"):
        ledger.post(db, _posting(entries=(Entry("a2", 0),)))


def test_quantity_without_denominator_is_refused(db):
    with pytest.raises(ValueError, match="quantity_denom"):
        ledger.post(db, _posting(entries=(
            Entry("a2", 147, quantity_num=74, quantity_denom=None),
            Entry("a1", -147))))


# --- 書かれるもの -------------------------------------------------------

def test_posted_row_records_policy_and_key(db):
    ledger.post(db, _posting())
    row = db.execute("SELECT ofx_fitid, fitid_policy, natural_key FROM transactions").fetchone()
    assert row[1] == "v2"
    assert row[2] == ledger.natural_key(_posting())
    assert row[0] == ledger.fitid_of(row[2])


def test_investment_row_is_written_with_the_transaction(db):
    db.execute("INSERT INTO accounts (guid, name, account_type) VALUES ('f1','ファンド','ASSET')")
    ledger.post(db, _posting(
        entries=(Entry("f1", 147, quantity_num=740000, quantity_denom=10000), Entry("a2", -147)),
        investment={"security_guid": "f1", "type": "REINVEST", "units": 74.0,
                    "unit_price": 1986.0, "total_amount": 147.0}))
    assert db.execute("SELECT type, units FROM investment_transactions").fetchone() \
        == ("REINVEST", 74.0)


# --- 集約が崩れていないかの見張り -----------------------------------------

def test_no_importer_writes_transactions_directly():
    """importer が transactions に直接 INSERT していないこと。

    冪等性が各 importer に散らばっていたことが二重計上の構造的原因だった
    (技術ノート 289c50d8ac)。記帳経路が再び分岐したらここで落ちる。
    """
    import pathlib

    importers = pathlib.Path(__file__).resolve().parent.parent / "py" / "importers"
    offenders = [
        f.name for f in importers.glob("*.py")
        if f.name != "ledger.py" and "INSERT INTO transactions" in f.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"記帳サービスを通さず書いている importer: {offenders}"


# --- 記帳先の口座(fd bccbd3b2ee56) ---------------------------------------

def test_missing_account_is_rejected(db):
    """存在しない口座への記帳は書かせない。

    splits.account_guid には FOREIGN KEY が宣言されているが SQLite は
    PRAGMA foreign_keys = ON を立てないと強制しない。実データで 1 件すり抜け、
    BS が ¥10,325,196 合わなくなっていた。複式の合計は 0 で合うので
    貸借一致の検算では捕まらない。
    """
    with pytest.raises(ValueError, match="存在しない口座"):
        ledger.post(db, _posting(entries=(Entry("no_such_account", 100), Entry("a1", -100))))
    assert _count(db) == 0, "拒否した記帳の transactions が残っている"


def test_placeholder_account_is_rejected(db):
    """placeholder(記帳できない親)への記帳も書かせない。

    BS の集計は placeholder を除外するので、親に直接付いた split は静かに落ちる
    (実データで ¥470)。
    """
    db.execute("INSERT INTO accounts (guid, name, account_type, placeholder)"
               " VALUES ('p1','日用品','EXPENSE',1)")
    with pytest.raises(ValueError, match="placeholder"):
        ledger.post(db, _posting(entries=(Entry("p1", 100), Entry("a1", -100))))
    assert _count(db) == 0


def test_leaf_account_is_accepted(db):
    """placeholder=0 の葉なら通る(上の 2 件が広すぎないことの裏返し)。"""
    db.execute("INSERT INTO accounts (guid, name, account_type, placeholder)"
               " VALUES ('p2','その他日用品','EXPENSE',0)")
    assert ledger.post(db, _posting(entries=(Entry("p2", 100), Entry("a1", -100)))) is not None
