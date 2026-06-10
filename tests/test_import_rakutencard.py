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


def test_normalize_etc_roundtrip_stays_distinct():
    # ＥＴＣ往復（経路反転で同額同日）は別キーに保たれる＝正当な複数取引を統合しない
    assert normalize_card_merchant("ＥＴＣカード売上　ｷﾀｲﾝﾀ ﾐﾅﾐｲﾝﾀ") != \
        normalize_card_merchant("ＥＴＣカード売上　ﾐﾅﾐｲﾝﾀ ｷﾀｲﾝﾀ")


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
