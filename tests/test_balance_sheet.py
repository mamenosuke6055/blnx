"""
compute_balance_sheet のテスト。

sign 規約:
- ASSET / EXPENSE: 正値 = 残高あり (借方残高)
- LIABILITY / EQUITY / INCOME: 負値 = 残高あり (貸方残高)
- compute_balance_sheet は LIABILITY / EQUITY を符号反転して表示するため、
  テストでは表示後の正値で assert する。
"""
import uuid
from datetime import date

from py.analysis.generate_balance_sheet import compute_balance_sheet


def _add_account(conn, name, account_type, placeholder=0):
    guid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO accounts (guid, name, account_type, placeholder) VALUES (?, ?, ?, ?)",
        (guid, name, account_type, placeholder),
    )
    return guid


def _add_tx(conn, post_date, splits, description=''):
    tx_guid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO transactions (guid, post_date, description) VALUES (?, ?, ?)",
        (tx_guid, post_date, description),
    )
    for acc, num, denom in splits:
        conn.execute(
            "INSERT INTO splits "
            "(guid, tx_guid, account_guid, value_num, value_denom, "
            " quantity_num, quantity_denom) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, tx_guid, acc, num, denom, num, denom),
        )
    return tx_guid


# ── 基本構造 ───────────────────────────────────────────────


def test_empty_db_balanced(conn):
    """splits / transactions が無い空 DB でも貸借一致 (どちらも 0)。"""
    bs = compute_balance_sheet(conn, at=date(2026, 5, 15))
    assert bs['total_assets'] == 0
    assert bs['total_liabilities'] == 0
    assert bs['total_equity_with_re'] == 0
    assert bs['balanced'] is True


def test_simple_asset_equity_balanced(conn):
    """ASSET +1000 / EQUITY -1000 で貸借一致。"""
    bank = _add_account(conn, "Bank", "ASSET")
    equity = _add_account(conn, "Equity", "EQUITY")
    _add_tx(conn, "2026-01-01", [(bank, 1000, 1), (equity, -1000, 1)])

    bs = compute_balance_sheet(conn, at=date(2026, 5, 15))
    assert bs['total_assets'] == 1000
    assert bs['total_equity_accounts'] == 1000  # 符号反転表示
    assert bs['retained_earnings'] == 0
    assert bs['total_equity_with_re'] == 1000
    assert bs['balanced'] is True


def test_retained_earnings_from_income_minus_expense(conn):
    """期末閉鎖なしで INCOME / EXPENSE が残っていても RE で吸収して貸借一致。"""
    bank = _add_account(conn, "Bank", "ASSET")
    salary = _add_account(conn, "Salary", "INCOME")
    food = _add_account(conn, "Food", "EXPENSE")
    # 給与 +1000 → bank +1000, INCOME -1000
    _add_tx(conn, "2026-01-25", [(bank, 1000, 1), (salary, -1000, 1)])
    # 食費 -300 → bank -300, EXPENSE +300
    _add_tx(conn, "2026-02-05", [(bank, -300, 1), (food, 300, 1)])

    bs = compute_balance_sheet(conn, at=date(2026, 5, 15))
    assert bs['total_assets'] == 700
    # RE = -INCOME - EXPENSE = -(-1000) - 300 = 700
    assert bs['retained_earnings'] == 700
    assert bs['balanced'] is True


def test_liability_displayed_as_positive(conn):
    """LIABILITY は符号反転して正値で表示される。"""
    bank = _add_account(conn, "Bank", "ASSET")
    card = _add_account(conn, "Credit Card", "LIABILITY")
    # カードで購入: bank はまだ動かず、LIABILITY が貸方計上 (-500)
    food = _add_account(conn, "Food", "EXPENSE")
    _add_tx(conn, "2026-03-10", [(card, -500, 1), (food, 500, 1)])
    # 期首の bank +1000 / Equity -1000
    equity = _add_account(conn, "Equity", "EQUITY")
    _add_tx(conn, "2026-01-01", [(bank, 1000, 1), (equity, -1000, 1)])

    bs = compute_balance_sheet(conn, at=date(2026, 5, 15))
    assert bs['total_assets'] == 1000
    assert bs['total_liabilities'] == 500  # 符号反転後の正値
    # RE = -0 - 500 = -500 (赤字)
    assert bs['retained_earnings'] == -500
    # 純資産 1000 + RE -500 = 500
    assert bs['total_equity_with_re'] == 500
    # 資産 1000 = 負債 500 + 純資産 500
    assert bs['balanced'] is True


# ── as_of (at) ─────────────────────────────────────────────


def test_at_excludes_future_tx(conn):
    """at 以降の tx は集計に含まれない。"""
    bank = _add_account(conn, "Bank", "ASSET")
    equity = _add_account(conn, "Equity", "EQUITY")
    _add_tx(conn, "2026-01-01", [(bank, 1000, 1), (equity, -1000, 1)])
    _add_tx(conn, "2026-04-01", [(bank, 500, 1), (equity, -500, 1)])

    bs_q1 = compute_balance_sheet(conn, at=date(2026, 3, 31))
    bs_q2 = compute_balance_sheet(conn, at=date(2026, 4, 30))

    assert bs_q1['total_assets'] == 1000
    assert bs_q2['total_assets'] == 1500


# ── 除外条件 ───────────────────────────────────────────────


def test_usd_cents_excluded(conn):
    """denom=100 の splits は集計から除外される。"""
    bank = _add_account(conn, "Bank", "ASSET")
    equity = _add_account(conn, "Equity", "EQUITY")
    _add_tx(conn, "2026-01-01", [(bank, 1000, 1), (equity, -1000, 1)])
    # denom=100 (USD cents) は除外されるべき。両側除外しないと貸借崩れる
    other = _add_account(conn, "Other", "ASSET")
    _add_tx(conn, "2026-02-01", [(bank, 2000, 100), (other, -2000, 100)])

    bs = compute_balance_sheet(conn, at=date(2026, 5, 15))
    assert bs['total_assets'] == 1000


def test_placeholder_excluded(conn):
    """placeholder アカウントは集計から除外される (残高があっても)。"""
    real_bank = _add_account(conn, "Real Bank", "ASSET", placeholder=0)
    ph_bank = _add_account(conn, "Placeholder Bank", "ASSET", placeholder=1)
    equity = _add_account(conn, "Equity", "EQUITY")
    _add_tx(conn, "2026-01-01", [(real_bank, 500, 1), (equity, -500, 1)])
    _add_tx(conn, "2026-01-02", [(ph_bank, 999, 1), (equity, -999, 1)])

    bs = compute_balance_sheet(conn, at=date(2026, 5, 15))
    names = [r['name'] for r in bs['assets']]
    assert "Real Bank" in names
    assert "Placeholder Bank" not in names
    assert bs['total_assets'] == 500


def test_zero_balance_accounts_excluded(conn):
    """残高 0 のアカウントは表示から除外される。"""
    bank = _add_account(conn, "Bank", "ASSET")
    equity = _add_account(conn, "Equity", "EQUITY")
    _add_tx(conn, "2026-01-01", [(bank, 1000, 1), (equity, -1000, 1)])
    _add_tx(conn, "2026-02-01", [(bank, -1000, 1), (equity, 1000, 1)])

    bs = compute_balance_sheet(conn, at=date(2026, 5, 15))
    assert bs['assets'] == []
    assert bs['equity_accounts'] == []
