"""transactions の不変条件(鍵が必ずあること)を DB 側で強制できているかのテスト。

技術ノート 289c50d8ac「不変条件は 1 つも DB で強制されていない」への回答。
冪等性を 6 つの importer の各自実装に置いたままにすると、1 つが鍵を落としただけで
再取込が二重計上になる(実データで 7 件発生)。ここでは importer の実装によらず
DB が弾くことを確かめる。

  - ofx_fitid は NULL でも空文字でも書けない
  - 同じ ofx_fitid は 2 行書けない
  - fitid_policy は v1 / v2 のみ
  - v2(記帳サービスの単一規則)で書くなら natural_key が必須。natural_key も一意
"""
import sqlite3

import pytest

from py.init.init_db import create_finance_tables


@pytest.fixture
def db():
    c = sqlite3.connect(":memory:")
    create_finance_tables(c)
    c.commit()
    yield c
    c.close()


def _insert(db, guid, fitid="SHA256:k1", policy=None, natural_key=None):
    cols = ["guid", "post_date", "description", "ofx_fitid"]
    vals = [guid, "2026-01-05", "テスト", fitid]
    if policy is not None:
        cols.append("fitid_policy")
        vals.append(policy)
    if natural_key is not None:
        cols.append("natural_key")
        vals.append(natural_key)
    db.execute(f"INSERT INTO transactions ({','.join(cols)})"
               f" VALUES ({','.join('?' * len(vals))})", vals)


def test_fitid_is_required(db):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, "t1", fitid=None)


def test_empty_fitid_is_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, "t1", fitid="")


def test_duplicate_fitid_is_rejected(db):
    _insert(db, "t1", fitid="SHA256:same")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, "t2", fitid="SHA256:same")


def test_policy_defaults_to_v1(db):
    _insert(db, "t1")
    assert db.execute("SELECT fitid_policy, natural_key FROM transactions").fetchone() \
        == ("v1", None)


def test_unknown_policy_is_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, "t1", policy="v3")


def test_v2_requires_natural_key(db):
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, "t1", policy="v2")


def test_v2_with_natural_key_is_accepted(db):
    _insert(db, "t1", policy="v2", natural_key="dneobank|main_jpy|2026-01-05|給与|222000|84261|0")
    assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_duplicate_natural_key_is_rejected(db):
    key = "dneobank|main_jpy|2026-01-05|給与|222000|84261|0"
    _insert(db, "t1", fitid="SHA256:a", policy="v2", natural_key=key)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, "t2", fitid="SHA256:b", policy="v2", natural_key=key)


def test_v1_rows_may_share_null_natural_key(db):
    """移行期: 既存の v1 行は自然キーを持たない。NULL は UNIQUE に抵触しない。"""
    _insert(db, "t1", fitid="SHA256:a")
    _insert(db, "t2", fitid="SHA256:b")
    assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
