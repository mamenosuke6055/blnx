"""取込ウォーターマーク(py/analysis/import_watermark.py)のテスト。"""

import sqlite3

from py.analysis.import_watermark import compute_watermarks, render_table


def _add_account(conn: sqlite3.Connection, guid: str, name: str,
                 account_type: str = "ASSET", ofx_type: str | None = "BANK") -> None:
    conn.execute(
        "INSERT INTO accounts (guid, name, account_type, ofx_type) VALUES (?, ?, ?, ?)",
        (guid, name, account_type, ofx_type),
    )


def _add_tx(conn: sqlite3.Connection, tx_guid: str, post_date: str, enter_date: str,
            account_guid: str, value_num: int) -> None:
    conn.execute(
        "INSERT INTO transactions (guid, post_date, enter_date, description) VALUES (?, ?, ?, ?)",
        (tx_guid, post_date, enter_date, "test"),
    )
    conn.execute(
        "INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom) VALUES (?, ?, ?, ?, 1)",
        (tx_guid + "_s", tx_guid, account_guid, value_num),
    )


def test_watermark_picks_latest_post_and_import(conn):
    # 稼働 source(楽天カード)で「最後の記録日/取込/次回DL目安」を検証
    _add_account(conn, "rc", "Rakuten Card")
    _add_tx(conn, "t1", "2026-05-30", "2026-06-04 10:00:00", "rc", -1000)
    _add_tx(conn, "t2", "2026-05-31", "2026-06-04 18:52:37", "rc", -2000)
    conn.commit()

    wms = {w.source: w for w in compute_watermarks(conn)}
    rc = wms["rakuten_card"]
    assert rc.last_post_date == "2026-05-31"          # 最後の記録日
    assert rc.last_import_at == "2026-06-04 18:52:37"  # 最後の取込実行
    assert rc.tx_count == 2
    assert rc.next_download_from == "2026-06-01"        # 翌日
    assert rc.dormant is False


def test_dormant_source_has_no_download_suggestion(conn):
    # 休眠口座(楽天銀行)は記録は見えるが「次回DL目安」を出さない(追わない)
    _add_account(conn, "rb", "Rakuten Bank")
    _add_tx(conn, "t1", "2026-03-31", "2026-05-18 18:52:37", "rb", -2000)
    conn.commit()

    wms = {w.source: w for w in compute_watermarks(conn)}
    rb = wms["rakuten_bank"]
    assert rb.dormant is True
    assert rb.last_post_date == "2026-03-31"   # 記録は残る
    assert rb.next_download_from is None        # が、DL目安は出さない
    assert wms["rakuten_sec"].dormant is True   # 楽天証券も休眠
    assert wms["resona_bank"].dormant is False  # りそなは稼働


def test_watermark_empty_source_is_none(conn):
    # 口座も取引も無ければ None / 0 を返す(クラッシュしない)
    wms = {w.source: w for w in compute_watermarks(conn)}
    rb = wms["rakuten_bank"]
    assert rb.last_post_date is None
    assert rb.next_download_from is None
    assert rb.tx_count == 0


def test_watermark_investment_bucket_uses_ofx_type(conn):
    # 投資商品バケットは口座名でなく ofx_type='INVESTMENT' で集約する
    _add_account(conn, "fund1", "eMAXIS Slim 米国株式(S&P500)",
                 account_type="ASSET", ofx_type="INVESTMENT")
    _add_tx(conn, "t1", "2026-06-01", "2026-06-04 06:29:00", "fund1", 50000)
    conn.commit()

    wms = {w.source: w for w in compute_watermarks(conn)}
    inv = wms["sbi_sec"]
    assert inv.last_post_date == "2026-06-01"
    assert inv.tx_count == 1


def test_dneobank_groups_multiple_accounts(conn):
    # 住信SBIネット銀行は複数口座をまとめ、最も新しい記録日を採用する
    _add_account(conn, "a1", "SBI Sumishin Net Bank")
    _add_account(conn, "a2", "SBI Sumishin Hybrid Deposit")
    _add_tx(conn, "t1", "2026-06-02", "2026-06-04 07:00:00", "a1", 100)
    _add_tx(conn, "t2", "2026-06-04", "2026-06-04 07:57:00", "a2", 200)
    conn.commit()

    wms = {w.source: w for w in compute_watermarks(conn)}
    assert wms["dneobank"].last_post_date == "2026-06-04"
    assert wms["dneobank"].tx_count == 2


def test_render_table_contains_headers_and_labels(conn):
    out = render_table(compute_watermarks(conn))
    assert "源泉" in out
    assert "次回DL目安" in out
    assert "楽天銀行" in out


def test_render_table_shows_status_column(conn):
    # 状態列があり、休眠(楽天銀行/楽天証券)と稼働の両方が表示される
    out = render_table(compute_watermarks(conn))
    assert "状態" in out
    assert "休眠" in out   # 休眠 source が存在する
    assert "稼働" in out   # 稼働 source も存在する
