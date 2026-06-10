import pytest

from py.analysis.exchange_rate_service import JPY_GUID, USD_GUID, _FALLBACK_RATE, get_usd_jpy_rate


def _ins(conn, dt, rate):
    conn.execute(
        "INSERT INTO exchange_rates (date, from_currency_guid, to_currency_guid, rate) "
        "VALUES (?, ?, ?, ?)",
        (dt, USD_GUID, JPY_GUID, rate),
    )
    conn.commit()


def test_fallback_when_empty(conn):
    assert get_usd_jpy_rate(conn) == pytest.approx(_FALLBACK_RATE)


def test_returns_inserted_rate(conn):
    _ins(conn, "2026-05-27", 149.5)
    assert get_usd_jpy_rate(conn, "2026-05-27") == pytest.approx(149.5)


def test_uses_latest_before_at_date(conn):
    _ins(conn, "2026-05-01", 148.0)
    _ins(conn, "2026-05-15", 151.0)
    _ins(conn, "2026-05-27", 149.0)
    assert get_usd_jpy_rate(conn, "2026-05-20") == pytest.approx(151.0)


def test_fallback_when_no_rate_before_date(conn):
    _ins(conn, "2026-05-27", 149.5)
    assert get_usd_jpy_rate(conn, "2026-01-01") == pytest.approx(_FALLBACK_RATE)


def test_at_date_none_picks_latest(conn):
    _ins(conn, "2020-01-01", 109.0)
    assert get_usd_jpy_rate(conn) == pytest.approx(109.0)
