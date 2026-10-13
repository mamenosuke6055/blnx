import csv as csvmod
import sqlite3

from py.init.init_db import create_finance_tables
from py.importers.import_rakutencard import (
    card_dedup_key,
    import_rakuten_card_csv,
    normalize_card_merchant,
)

# enavi 実フォーマット相当の最小ヘッダ（importer は '利用日' と '利用店名・商品名' で
# ヘッダ行を検出し、'利用金額' を金額に使う）。
HEADER = ["利用日", "利用店名・商品名", "利用者", "支払方法", "利用金額"]


def _write_enavi(path, rows):
    """rows: list of (date 'YYYY/MM/DD', description, amount)。"""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csvmod.writer(f)
        w.writerow(HEADER)
        for date, desc, amt in rows:
            w.writerow([date, desc, "本人", "1回払い", amt])


def _make_db(tmp_path):
    db = tmp_path / "finance.db"
    c = sqlite3.connect(db)
    create_finance_tables(c)
    c.commit()
    c.close()
    return db


def _tx_count(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        c.close()


# --- 正規化の単体テスト -------------------------------------------------

def test_normalize_brand_domestic_visa():
    assert normalize_card_merchant("ＶＩＳＡ国内利用　VS ﾍﾟｲﾍﾟｲ*ｻﾝﾌﾟﾙﾎﾃﾙ") == \
        normalize_card_merchant("ﾍﾟｲﾍﾟｲ*ｻﾝﾌﾟﾙﾎﾃﾙ")


def test_normalize_brand_domestic_jcb():
    assert normalize_card_merchant("ＪＣＢ国内利用　QP  ｾﾌﾞﾝ-ｲﾚﾌﾞﾝ") == \
        normalize_card_merchant("ｾﾌﾞﾝ-ｲﾚﾌﾞﾝ")


def test_normalize_brand_overseas_with_country_suffix():
    # ブランド海外利用（接頭辞）と生マーチャント（利用国XX 接尾辞）が一致
    assert normalize_card_merchant("ＶＩＳＡ海外利用　SAMPLEGAMES.COM 12345") == \
        normalize_card_merchant("SAMPLEGAMES.COM 12345利用国US")


def test_normalize_trailing_import_code():
    # 末尾の取込コード数字（2個以上の空白に続く）を除去して一致
    assert normalize_card_merchant("楽天キャッシュ　チャージ　        123456") == \
        normalize_card_merchant("楽天キャッシュ　チャージ")
    assert normalize_card_merchant("サンプルストア　        654321") == \
        normalize_card_merchant("サンプルストア")


def test_normalize_drops_detail_after_fullwidth_space():
    # 版によって全角空白より後ろの詳細が付く/消える/書き換わる決済は同じキー
    assert normalize_card_merchant("ＥＴＣカード売上") == \
        normalize_card_merchant("ＥＴＣカード売上　ｷﾀｲﾝﾀ ﾐﾅﾐｲﾝﾀ")
    assert normalize_card_merchant("サンプル光　ご利用料金") == \
        normalize_card_merchant("サンプル光　ご利用料金　ｻﾝﾌﾟﾙﾋｶﾘ ｺﾞﾘﾖｳﾘｮｳｷ")
    assert normalize_card_merchant("サンプル生命保険料　０８月分") == \
        normalize_card_merchant("サンプル生命保険料　08ｶﾞﾂﾌﾞﾝ")


def test_normalize_keeps_ascii_space_in_merchant():
    # ASCII 空白は店名の一部。PAYPAL 経由の別店舗を同じキーにしない
    assert normalize_card_merchant("PAYPAL *SHOPA") != normalize_card_merchant("PAYPAL *SHOPB")
    assert normalize_card_merchant("ﾔﾏﾔ ﾓﾊﾞﾗﾃﾝ") != normalize_card_merchant("ﾔﾏﾔ ｷﾀﾃﾝ")


def test_dedup_key_distinct_merchants_same_amount():
    # 同日同額でも別マーチャントは別キー（同額偶然衝突の誤統合を防ぐ）
    assert card_dedup_key("2025-01-18", 1000, "ﾓﾊﾞｲﾙｽｲｶ") != \
        card_dedup_key("2025-01-18", 1000, "ｵﾌｲｼﾔﾙｸﾞﾂｽﾞｽﾄｱ")


# --- インポート結合テスト -----------------------------------------------

def test_import_dedup_two_formats(tmp_path):
    # 生マーチャント形式とブランド明細形式の同一決済 → 1 件に収束
    db = _make_db(tmp_path)
    raw = tmp_path / "enavi_raw.csv"
    brand = tmp_path / "enavi_brand.csv"
    _write_enavi(raw, [("2025/06/23", "ﾍﾟｲﾍﾟｲ*ｻﾝﾌﾟﾙﾎﾃﾙ", "50000")])
    _write_enavi(brand, [("2025/06/23", "ＶＩＳＡ国内利用　VS ﾍﾟｲﾍﾟｲ*ｻﾝﾌﾟﾙﾎﾃﾙ", "50000")])

    import_rakuten_card_csv(raw, str(db))
    import_rakuten_card_csv(brand, str(db))

    assert _tx_count(db) == 1


def test_import_dedup_two_formats_reverse_order(tmp_path):
    # 取込順が逆（ブランド→生）でも 1 件
    db = _make_db(tmp_path)
    raw = tmp_path / "enavi_raw.csv"
    brand = tmp_path / "enavi_brand.csv"
    _write_enavi(raw, [("2025/06/06", "SAMPLEGAMES.COM 12345利用国US", "5980")])
    _write_enavi(brand, [("2025/06/06", "ＶＩＳＡ海外利用　SAMPLEGAMES.COM 12345", "5980")])

    import_rakuten_card_csv(brand, str(db))
    import_rakuten_card_csv(raw, str(db))

    assert _tx_count(db) == 1


def test_import_idempotent(tmp_path):
    db = _make_db(tmp_path)
    csvp = tmp_path / "enavi.csv"
    _write_enavi(csvp, [("2025/06/12", "ﾔﾏﾀﾞﾃﾞﾝｷ", "12000")])
    import_rakuten_card_csv(csvp, str(db))
    import_rakuten_card_csv(csvp, str(db))
    assert _tx_count(db) == 1


def test_import_keeps_distinct_same_amount(tmp_path):
    # 同日同額でも別マーチャントは 2 件として保持
    db = _make_db(tmp_path)
    csvp = tmp_path / "enavi.csv"
    _write_enavi(csvp, [
        ("2025/01/18", "ﾓﾊﾞｲﾙｽｲｶ", "1000"),
        ("2025/01/18", "ｵﾌｲｼﾔﾙｸﾞﾂｽﾞｽﾄｱ", "1000"),
    ])
    import_rakuten_card_csv(csvp, str(db))
    assert _tx_count(db) == 2


def test_import_keeps_etc_roundtrip(tmp_path):
    # ＥＴＣ往復（同日同額・経路反転）は 2 件として保持
    db = _make_db(tmp_path)
    csvp = tmp_path / "enavi.csv"
    _write_enavi(csvp, [
        ("2026/03/16", "ＥＴＣカード売上　ｷﾀｲﾝﾀ ﾐﾅﾐｲﾝﾀ", "90"),
        ("2026/03/16", "ＥＴＣカード売上　ﾐﾅﾐｲﾝﾀ ｷﾀｲﾝﾀ", "90"),
    ])
    import_rakuten_card_csv(csvp, str(db))
    assert _tx_count(db) == 2


def test_import_creates_liability_account_type(tmp_path):
    # 素の DB への取込で、実取引が付く末端口座が LIABILITY で作成される
    # （かつて末端が PLACEHOLDER になり BS の負債集計から漏れるバグがあった）
    db = _make_db(tmp_path)
    csvp = tmp_path / "enavi.csv"
    _write_enavi(csvp, [("2025/06/01", "ﾏﾂﾔ", "1180")])
    import_rakuten_card_csv(csvp, str(db))

    c = sqlite3.connect(db)
    try:
        t = c.execute(
            "SELECT account_type FROM accounts WHERE name='Rakuten Card'"
        ).fetchone()[0]
        assert t == "LIABILITY"
    finally:
        c.close()


def test_import_creates_balanced_splits(tmp_path):
    db = _make_db(tmp_path)
    csvp = tmp_path / "enavi.csv"
    _write_enavi(csvp, [("2025/06/01", "ﾏﾂﾔ", "1180")])
    import_rakuten_card_csv(csvp, str(db))

    c = sqlite3.connect(db)
    try:
        for (tx,) in c.execute("SELECT guid FROM transactions").fetchall():
            s = c.execute("SELECT SUM(value_num) FROM splits WHERE tx_guid=?", (tx,)).fetchone()[0]
            assert s == 0  # 借貸ゼロサム
    finally:
        c.close()


def test_import_keeps_two_identical_purchases_in_one_file(tmp_path):
    """同日・同額・同店の**正当な 2 件**は両方残る(旧実装は 1 件に畳んでいた)。

    速報版と確定版の二重取込を防ぐ正規化キーは「件数」で突合するので、
    1 つのファイルに 2 本ある決済は 2 本入り、同じファイルを再取込しても増えない。
    """
    db = _make_db(tmp_path)
    csvp = tmp_path / "enavi.csv"
    _write_enavi(csvp, [
        ("2025/06/12", "ｾﾌﾞﾝｲﾚﾌﾞﾝ", "500"),
        ("2025/06/12", "ｾﾌﾞﾝｲﾚﾌﾞﾝ", "500"),
    ])
    import_rakuten_card_csv(csvp, str(db))
    assert _tx_count(db) == 2

    import_rakuten_card_csv(csvp, str(db))
    assert _tx_count(db) == 2


def test_confirmed_format_does_not_duplicate_two_identical_purchases(tmp_path):
    """速報版で 2 件入っている決済が、確定版(表記ゆれ)で増えない。"""
    db = _make_db(tmp_path)
    early = tmp_path / "enavi_early.csv"
    _write_enavi(early, [
        ("2025/06/12", "ｾﾌﾞﾝｲﾚﾌﾞﾝ", "500"),
        ("2025/06/12", "ｾﾌﾞﾝｲﾚﾌﾞﾝ", "500"),
    ])
    import_rakuten_card_csv(early, str(db))

    confirmed = tmp_path / "enavi_confirmed.csv"
    _write_enavi(confirmed, [
        ("2025/06/12", "ＶＩＳＡ国内利用　VS ｾﾌﾞﾝｲﾚﾌﾞﾝ", "500"),
        ("2025/06/12", "ＶＩＳＡ国内利用　VS ｾﾌﾞﾝｲﾚﾌﾞﾝ", "500"),
    ])
    import_rakuten_card_csv(confirmed, str(db))
    assert _tx_count(db) == 2


def test_detail_variants_across_files_do_not_duplicate(tmp_path):
    """速報版(詳細なし)の後に確定版(全角空白の後ろに経路・品目)を取り込んでも増えない。

    往復(同日同額 2 本)はどちらの版にも 2 本あり、件数で突合されて 2 本のまま。
    """
    db = _make_db(tmp_path)
    early = tmp_path / "enavi_early.csv"
    _write_enavi(early, [
        ("2025/04/02", "ＥＴＣカード売上", "300"),
        ("2025/04/02", "ＥＴＣカード売上", "300"),
        ("2025/04/05", "サンプル光　ご利用料金", "4000"),
        ("2025/04/08", "サンプル生命保険料　０８月分", "5000"),
    ])
    import_rakuten_card_csv(early, str(db))
    assert _tx_count(db) == 4

    confirmed = tmp_path / "enavi_confirmed.csv"
    _write_enavi(confirmed, [
        ("2025/04/02", "ＥＴＣカード売上　ｷﾀｲﾝﾀ ﾐﾅﾐｲﾝﾀ", "300"),
        ("2025/04/02", "ＥＴＣカード売上　ﾐﾅﾐｲﾝﾀ ｷﾀｲﾝﾀ", "300"),
        ("2025/04/05", "サンプル光　ご利用料金　ｻﾝﾌﾟﾙﾋｶﾘ ｺﾞﾘﾖｳﾘｮｳｷ", "4000"),
        ("2025/04/08", "サンプル生命保険料　08ｶﾞﾂﾌﾞﾝ", "5000"),
    ])
    import_rakuten_card_csv(confirmed, str(db))
    assert _tx_count(db) == 4

    # 逆順(確定版 → 速報版)でも同じ
    (tmp_path / "rev").mkdir()
    db2 = _make_db(tmp_path / "rev")
    import_rakuten_card_csv(confirmed, str(db2))
    import_rakuten_card_csv(early, str(db2))
    assert _tx_count(db2) == 4


def test_recovers_second_identical_purchase_collapsed_by_v1(tmp_path):
    """v1 が同日・同額・同摘要の 2 本を 1 件に畳んでいた DB に再取込すると、2 本目が補われる。"""
    import hashlib
    db = _make_db(tmp_path)
    csvp = tmp_path / "enavi.csv"
    _write_enavi(csvp, [("2025/04/01", "ＥＴＣカード売上", "500")])
    import_rakuten_card_csv(csvp, str(db))
    # 既存行を v1 の鍵・方式に書き換えて、v1 時代の状態を再現する
    v1 = "SHA256:" + hashlib.sha256("RAKUTENCARD:2025-04-01:500:ＥＴＣカード売上".encode()).hexdigest()
    c = sqlite3.connect(db)
    c.execute("UPDATE transactions SET ofx_fitid=?, fitid_policy='v1', natural_key=NULL", (v1,))
    c.commit()
    c.close()

    _write_enavi(csvp, [
        ("2025/04/01", "ＥＴＣカード売上", "500"),
        ("2025/04/01", "ＥＴＣカード売上", "500"),
    ])
    import_rakuten_card_csv(csvp, str(db))
    assert _tx_count(db) == 2
    import_rakuten_card_csv(csvp, str(db))
    assert _tx_count(db) == 2
