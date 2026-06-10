import pandas as pd
import sqlite3
import json
from pathlib import Path
import glob
import hashlib
import uuid
import re
import csv
import io
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime

from py.importers._cost_basis import (
    calc_moving_avg_cost_per_unit,
    get_capital_gain_account_guid,
    get_capital_loss_account_guid,
    build_sell_splits,
)

def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent

def get_config():
    root = get_project_root()
    config_path = root / "config/settings.json"
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_db_path():
    config = get_config()
    return get_project_root() / config.get("db_path", "db/finance.db")

def get_or_create_account_guid(conn: sqlite3.Connection, name_path: list[str], account_type: str, ofx_type: str = None) -> str:
    cursor = conn.cursor()
    parent_guid = None
    
    for i, name in enumerate(name_path):
        cursor.execute("""
            SELECT guid FROM accounts WHERE name = ? AND parent_guid IS ?
        """, (name, parent_guid) if parent_guid else (name, None))
        
        result = cursor.fetchone()
        
        if result:
            guid = result[0]
        else:
            guid = uuid.uuid4().hex
            current_type = account_type if i == len(name_path) - 1 else 'ASSET'
            if i == 0 and name == 'Expenses': current_type = 'EXPENSE'
            if i == 0 and name == 'Income': current_type = 'INCOME'
            
            current_ofx_type = ofx_type if i == len(name_path) - 1 else None

            cursor.execute("""
                INSERT INTO accounts (guid, name, account_type, ofx_type, parent_guid)
                VALUES (?, ?, ?, ?, ?)
            """, (guid, name, current_type, current_ofx_type, parent_guid))
            print(f"Created account: {' > '.join(name_path[:i+1])}")
        
        parent_guid = guid
        
    return parent_guid

def get_currency_guid(conn: sqlite3.Connection, mnemonic: str) -> str:
    cursor = conn.cursor()
    cursor.execute("SELECT guid FROM currencies WHERE mnemonic = ?", (mnemonic,))
    result = cursor.fetchone()
    if result:
        return result[0]
    
    guid = uuid.uuid4().hex
    fraction = 100 
    cursor.execute("INSERT INTO currencies (guid, mnemonic, fraction) VALUES (?, ?, ?)", (guid, mnemonic, fraction))
    print(f"Created currency: {mnemonic}")
    return guid

def parse_sbi_date(date_str):
    """
    Parse dates like '2025/11/18' or '2025年11月26日' or '25/11/28'
    """
    if not isinstance(date_str, str): return None
    date_str = date_str.strip()
    try:
        if '年' in date_str:
            return datetime.strptime(date_str, '%Y年%m月%d日').strftime('%Y-%m-%d')
        elif len(date_str.split('/')[0]) == 2: # YY/MM/DD
             return datetime.strptime(date_str, '%y/%m/%d').strftime('%Y-%m-%d')
        else: # YYYY/MM/DD
            return datetime.strptime(date_str, '%Y/%m/%d').strftime('%Y-%m-%d')
    except ValueError:
        return None

def parse_amount(val):
    if pd.isna(val) or val == '-' or val == '':
        return 0.0
    if isinstance(val, str):
        return float(val.replace(',', ''))
    return float(val)

def ensure_fund_snapshots_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sbi_fund_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            fund_name TEXT NOT NULL,
            account_type TEXT NOT NULL,
            valuation INTEGER NOT NULL,
            prev_year_end_valuation INTEGER,
            sell_amount INTEGER,
            distribution_amount INTEGER,
            purchase_amount INTEGER,
            fee INTEGER,
            total_return_jpy INTEGER,
            total_return_pct REAL,
            UNIQUE(snapshot_date, fund_name, account_type)
        )
    """)


def ensure_asset_snapshots_table(conn: sqlite3.Connection):
    """時価評価スナップショット表。init_db でも作るが、単体実行時の保険として冪等作成。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asset_snapshots (
            guid TEXT PRIMARY KEY NOT NULL UNIQUE,
            date TEXT NOT NULL,
            account_guid TEXT NOT NULL,
            market_value_num INTEGER,
            market_value_denom INTEGER DEFAULT 1,
            book_value_num INTEGER,
            book_value_denom INTEGER DEFAULT 1,
            units_num INTEGER,
            units_denom INTEGER DEFAULT 1,
            currency_guid TEXT,
            FOREIGN KEY (account_guid) REFERENCES accounts(guid),
            FOREIGN KEY (currency_guid) REFERENCES currencies(guid)
        )
    """)


def _read_text(csv_path: Path) -> tuple[str, str]:
    """CSV を読み、(本文, 使用エンコーディング) を返す。
    SBI は旧 cp932 / 新 utf-8-sig が混在するため自動判別する。
    """
    for enc in ('utf-8-sig', 'cp932'):
        try:
            return csv_path.read_text(encoding=enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return csv_path.read_text(encoding='cp932', errors='replace'), 'cp932'


def _normalize_name(s: str) -> str:
    """全角/半角・空白の揺れを吸収した比較用キー。NFKC 正規化 + 全空白除去。"""
    return ''.join(unicodedata.normalize('NFKC', s or '').split())


def _extract_account_type(label: str) -> str | None:
    """セクション見出し（例『投資信託（金額/NISA預り（成長投資枠））』）から口座種別を抽出。
    accounts ツリー上の中間ノード名（NISA成長投資枠 等）に合わせて返す。該当なしは None。
    """
    s = unicodedata.normalize('NFKC', label or '')
    if 'NISA' in s:
        if '成長' in s:
            return 'NISA成長投資枠'
        if 'つみたて' in s or '積立' in s:
            return 'NISAつみたて投資枠'
    if '特定' in s:
        return '特定'
    if '一般' in s:
        return '一般'
    return None


def _normalize_deposit_type(raw: str) -> str:
    """約定履歴の『預り』列を accounts ツリーの中間ノード名へ正規化。新旧両形式対応。

    新形式(utf-8-sig): 'NISA (成長)' / 'NISA (つみたて)'（半角空白入り）
    旧形式(cp932)    : ' NISA(成) ' / ' NISA(つ) '（前後空白・短縮表記）

    旧 ' NISA(成) ' は '成長' を含まないため holdings 用の _extract_account_type では
    取りこぼす。ここは預り列専用に '成'/'つ' の1字で判定して新旧を吸収する。
    未知の値は accounts のノード名としてそのまま使う（口座が分離されるが取込は止めない）。
    """
    s = unicodedata.normalize('NFKC', raw or '').strip()
    if 'NISA' in s:
        if 'つ' in s or '積' in s:   # 'NISA (つみたて)' / ' NISA(つ) '
            return 'NISAつみたて投資枠'
        if '成' in s:                # 'NISA (成長)' / ' NISA(成) '
            return 'NISA成長投資枠'
    if '特定' in s:
        return '特定'
    if '一般' in s:
        return '一般'
    return s


# 銘柄名(fund_name)の正規化エイリアス: 新形式の長い名前 → 旧形式の短い名前。
# SBI 証券の SaveFile_*.csv は新旧で同じ取引でも銘柄名表記が異なる場合がある:
#   旧形式(cp932): 'ニッセイＮＡＳＤＡＱ１００インデ＜購入・換金手数料なし＞'   (切り詰め)
#   新形式(utf-8-sig): 'ニッセイＮＡＳＤＡＱ１００インデックスファンド＜購入・換金手数料なし＞' (フルネーム)
# fitid 生成や account 解決で銘柄名を直接キーに使うため、正規化しないと同じ取引が
# 別 fitid になり**両 CSV 取込時に重複登録**される。既存 DB は旧短表記で account が
# 作成されているため、新→旧 (DB 既存形式) に統一する。
_FUND_NAME_ALIAS: dict[str, str] = {
    "ニッセイＮＡＳＤＡＱ１００インデックスファンド＜購入・換金手数料なし＞":
        "ニッセイＮＡＳＤＡＱ１００インデ＜購入・換金手数料なし＞",
}


def _normalize_fund_name(name: str | None) -> str:
    """銘柄名を正規化する。新旧 SaveFile 形式の表記揺れを吸収。"""
    if name is None:
        return ''
    s = str(name).strip()
    return _FUND_NAME_ALIAS.get(s, s)


def _find_matching_account(
    conn: sqlite3.Connection,
    account_type: str | None,
    fund_name: str,
    threshold: float = 0.85,
) -> tuple[str | None, float]:
    """口座種別配下の INVESTMENT 口座から、ファンド名に最も近い既存口座 guid を返す。

    旧形式の約定履歴が作った口座名は全角・切り詰め（例『ニッセイＮＡＳＤＡＱ１００インデ＜…＞』）
    のことがあるため、NFKC 正規化後にまず完全一致を探し、無ければ difflib 類似度で照合する。
    閾値 0.85 は実測に基づく: 切り詰め⇄フル名は 0.89 で通り、別ファンド同士（例 ＳＢＩ・Ｓ米国
    高配当 ⇄ ＳＢＩ日本高配当 = 0.78）は弾く。閾値未満は (None, best_score)。
    account_type が None のときは全 INVESTMENT 口座を候補にする。
    """
    cur = conn.cursor()
    if account_type:
        cur.execute("""
            SELECT a.guid, a.name FROM accounts a
            JOIN accounts p ON a.parent_guid = p.guid
            WHERE p.name = ? AND a.ofx_type = 'INVESTMENT'
        """, (account_type,))
    else:
        cur.execute("SELECT guid, name FROM accounts WHERE ofx_type = 'INVESTMENT'")

    target = _normalize_name(fund_name)
    candidates = [(guid, _normalize_name(name)) for guid, name in cur.fetchall()]

    # 1) 正規化後の完全一致を最優先（正式名称で取り込まれた口座は確実にヒット）
    for guid, nname in candidates:
        if nname == target:
            return guid, 1.0

    # 2) 完全一致が無ければ最類似 + 閾値判定（切り詰め名の救済）
    best_guid, best_score = None, 0.0
    for guid, nname in candidates:
        score = SequenceMatcher(None, target, nname).ratio()
        if score > best_score:
            best_guid, best_score = guid, score
    if best_score >= threshold:
        return best_guid, best_score
    return None, best_score


def _parse_int_jpy(val) -> int:
    """『+9,343』『800』『--』等を円整数に。空・非数は 0。"""
    if val is None:
        return 0
    s = str(val).replace(',', '').replace('+', '').replace('円', '').strip()
    if s in ('', '-', '--'):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


_HOLDINGS_HEADER_KEY = 'ファンド名'


def import_sbi_holdings_list(csv_path: Path, snapshot_date: str | None = None) -> dict:
    """SBI『保有証券一覧』CSV（SaveFile, cp932, セクション構造）の時価を asset_snapshots へ取り込む。

    各明細の評価額（時価）を、口座種別×ファンド名の正規化マッチで既存の簿価口座へ紐付けて
    asset_snapshots に書く。マッチしない明細は重複口座を作らず unmatched として返す
    （新形式の約定履歴が未取込で簿価口座が無いケース。約定履歴側で口座が作られれば次回マッチする）。
    下流の集計は account_guid 経由でこの時価を参照する。

    snapshot_date 省略時はファイルの更新日時（DL日 ≒ 基準日）を用いる。
    Returns: {'snapshot_date', 'matched', 'skipped', 'unmatched': [...]}
    """
    text, _enc = _read_text(csv_path)
    if snapshot_date is None:
        snapshot_date = datetime.fromtimestamp(csv_path.stat().st_mtime).strftime('%Y-%m-%d')

    conn = sqlite3.connect(get_db_path())
    ensure_asset_snapshots_table(conn)
    jpy_guid = get_currency_guid(conn, 'JPY')
    conn.commit()

    reader = csv.reader(io.StringIO(text))
    current_account_type: str | None = None
    col: dict[str, int] | None = None
    matched = skipped = 0
    unmatched: list[dict] = []

    for row in reader:
        non_empty = [c for c in row if (c or '').strip()]
        if not non_empty:
            col = None  # 空行で明細ブロック終了
            continue
        c0 = row[0].strip()

        # 見出し・小計行（1〜2セル）。口座種別が読めれば更新し、データとしては扱わない。
        if len(non_empty) <= 2:
            at = _extract_account_type(c0)
            if at is not None:
                current_account_type = at
            continue

        # 明細の列ヘッダー
        if c0 == _HOLDINGS_HEADER_KEY:
            col = {name.strip(): i for i, name in enumerate(row)}
            continue

        # データ行
        if not col or '評価額' not in col or 'ファンド名' not in col:
            continue
        fund_name = row[col['ファンド名']].strip() if col['ファンド名'] < len(row) else ''
        if not fund_name:
            continue
        valuation = _parse_int_jpy(row[col['評価額']]) if col['評価額'] < len(row) else 0
        if valuation <= 0:
            continue
        book = None
        if '取得金額' in col and col['取得金額'] < len(row):
            book = _parse_int_jpy(row[col['取得金額']])

        guid, score = _find_matching_account(conn, current_account_type, fund_name)
        if guid is None:
            unmatched.append({
                'account_type': current_account_type,
                'fund_name': fund_name,
                'valuation': valuation,
                'best_score': round(score, 3),
            })
            continue

        if conn.execute(
            "SELECT 1 FROM asset_snapshots WHERE date = ? AND account_guid = ?",
            (snapshot_date, guid),
        ).fetchone():
            skipped += 1
            continue

        conn.execute(
            "INSERT INTO asset_snapshots "
            "(guid, date, account_guid, market_value_num, market_value_denom, "
            " book_value_num, book_value_denom, currency_guid) "
            "VALUES (?, ?, ?, ?, 1, ?, 1, ?)",
            (uuid.uuid4().hex, snapshot_date, guid, valuation, book, jpy_guid),
        )
        matched += 1

    conn.commit()
    conn.close()

    print(f"SBI保有証券一覧 {snapshot_date}: matched={matched}, skipped={skipped}, "
          f"unmatched={len(unmatched)} ({csv_path.name})")
    for u in unmatched:
        print(f"  [未マッチ] {u['account_type']} / {u['fund_name']} "
              f"評価額={u['valuation']:,} (best={u['best_score']})")
    return {'snapshot_date': snapshot_date, 'matched': matched,
            'skipped': skipped, 'unmatched': unmatched}


def import_sbi_fund_list(csv_path: Path):
    """
    Import fund holdings snapshot (fundList*.csv)
    Encoding: UTF-8-BOM
    Filename pattern: fundList{YYYYMMDDHHMMSS}.csv

    注意: これは旧『保有商品一覧』フラット形式（保有状況/前年末評価金額/トータルリターン列）用。
    現行 SBI の『保有証券一覧』(SaveFile, セクション構造) は import_sbi_holdings_list を使う。
    """
    m = re.search(r'fundList(\d{8})', csv_path.name)
    if not m:
        print(f"Cannot extract date from filename: {csv_path.name}")
        return
    snapshot_date = datetime.strptime(m.group(1), '%Y%m%d').strftime('%Y-%m-%d')

    try:
        df = pd.read_csv(csv_path, encoding='utf-8-sig')
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    required = ['保有状況', 'ファンド名', '口座種別', '評価金額', '前年末評価金額',
                '売却金額', '分配金額', '買付金額', '手数料', 'トータルリターン（円）', 'トータルリターン（率）']
    if not all(c in df.columns for c in required):
        print(f"Missing columns in {csv_path.name}. Found: {list(df.columns)}")
        return

    # 保有中のみ対象
    df = df[df['保有状況'] == '保有中']

    def parse_int(val):
        if pd.isna(val): return 0
        return int(str(val).replace(',', '').replace('+', '').replace(' ', '') or 0)

    def parse_pct(val):
        if pd.isna(val): return None
        try:
            return float(str(val).replace('%', '').replace('+', '').replace(' ', ''))
        except ValueError:
            return None

    conn = sqlite3.connect(get_db_path())
    ensure_fund_snapshots_table(conn)

    inserted = 0
    skipped = 0
    for _, row in df.iterrows():
        try:
            conn.execute("""
                INSERT OR IGNORE INTO sbi_fund_snapshots
                (snapshot_date, fund_name, account_type, valuation, prev_year_end_valuation,
                 sell_amount, distribution_amount, purchase_amount, fee, total_return_jpy, total_return_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_date,
                row['ファンド名'].strip(),
                row['口座種別'].strip(),
                parse_int(row['評価金額']),
                parse_int(row['前年末評価金額']),
                parse_int(row['売却金額']),
                parse_int(row['分配金額']),
                parse_int(row['買付金額']),
                parse_int(row['手数料']),
                parse_int(row['トータルリターン（円）']),
                parse_pct(row['トータルリターン（率）']),
            ))
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"Row error: {e}")

    conn.commit()
    conn.close()
    print(f"Fund snapshot {snapshot_date}: inserted={inserted}, skipped={skipped} from {csv_path.name}")


def import_sbi_domestic_trade_history(csv_path: Path):
    """国内/NISA 約定履歴 (SaveFile*) を取り込む。新旧両形式対応。

    新形式(2026-05 DL, utf-8-sig): 末尾列『受渡金額』、預り『NISA (成長)』、銘柄フルネーム。
    旧形式(cp932)               : 末尾列『受渡金額/決済損益』、預り『 NISA(成) 』、銘柄切り詰め。

    両者で (a)エンコーディング (b)ヘッダー行位置 (c)末尾列名 (d)預り表記 が異なる。
    (a) は _read_text の自動判別、(b) は動的ヘッダー検出（旧 skiprows=8 固定は新形式で崩れる）、
    (c) は列名の新旧両対応、(d) は _normalize_deposit_type で吸収する。
    Columns: 約定日,銘柄,銘柄コード,市場,取引,期限,預り,課税,約定数量,約定単価,手数料/諸経費等,税額,受渡日,{受渡金額|受渡金額/決済損益}
    """
    text, _enc = _read_text(csv_path)
    lines = text.splitlines()
    # 動的ヘッダー検出: 旧形式の skiprows=8 固定は新形式のヘッダー位置変更で崩れるため、
    # 明細ヘッダーを内容で探す（2行目の『約定開始年月日』等は '銘柄'+'預り' を持たず除外される）。
    header_row = next(
        (i for i, ln in enumerate(lines) if '約定日' in ln and '銘柄' in ln and '預り' in ln),
        -1,
    )
    if header_row == -1:
        print(f"ヘッダー行が見つかりません: {csv_path.name}")
        return

    try:
        df = pd.read_csv(io.StringIO(text), skiprows=header_row, header=0)
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    # 末尾の金額列は新旧で列名が違う（新『受渡金額』/旧『受渡金額/決済損益』）。先に解決する。
    settle_col = next((c for c in ('受渡金額/決済損益', '受渡金額') if c in df.columns), None)
    required = ['約定日', '銘柄', '取引', '預り', '約定数量', '約定単価', '受渡日']
    if settle_col is None or not all(c in df.columns for c in required):
        print(f"Missing columns in {csv_path.name}. Found: {list(df.columns)}")
        return

    # 空行を除去
    df = df.dropna(subset=['約定日'])

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()

    jpy_guid = get_currency_guid(conn, 'JPY')
    cash_account = get_or_create_account_guid(
        conn, ['Assets', 'Bank', 'SBI Securities', 'JPY_clearing'], 'ASSET', 'BANK'
    )
    conn.commit()

    new_tx_count = 0

    for _, row in df.iterrows():
        trade_date = parse_sbi_date(str(row['約定日']))
        settle_date = parse_sbi_date(str(row['受渡日']))
        if not trade_date:
            continue

        # 新旧 SaveFile 形式で銘柄名表記が異なる銘柄(NASDAQ100 等)を統一。
        # 正規化しないと同じ取引が別 fitid になり重複登録される。
        fund_name = _normalize_fund_name(row['銘柄'])
        tx_type_raw = str(row['取引']).strip()
        account_type_raw = str(row['預り']).strip()
        units = parse_amount(row['約定数量'])
        unit_price = parse_amount(row['約定単価'])
        settlement = parse_amount(row[settle_col])

        account_type = _normalize_deposit_type(account_type_raw)
        security_account = get_or_create_account_guid(
            conn, ['Assets', 'Investments', 'SBI Securities', account_type, fund_name], 'ASSET', 'INVESTMENT'
        )

        # FITID
        raw_str = f"SBISEC_DOM:{trade_date}:{fund_name}:{tx_type_raw}:{units}:{settlement}"
        fitid = 'SHA256:' + hashlib.sha256(raw_str.encode()).hexdigest()
        cursor.execute("SELECT 1 FROM transactions WHERE ofx_fitid = ?", (fitid,))
        if cursor.fetchone():
            continue

        # 取引種別判別を INSERT 前に行う。買付/売却以外（例: 分配金再投資）は
        # ここでスキップする。以前は INSERT 後に conn.rollback() で巻き戻す実装だったが、
        # commit はループ後の1回のみのため、Unknown 1件で**それまで保存した全 tx も
        # 巻き戻す**バグがあった (fd 0280bc08 で発覚: 217件CSV取込で 217件中分配金再投資2件が原因で
        # 投信買付の取込が大量に消失していた)。
        if '買付' not in tx_type_raw and '売却' not in tx_type_raw:
            print(f"Unknown tx type: {tx_type_raw}")
            continue

        tx_guid = uuid.uuid4().hex
        desc = f"{tx_type_raw} {fund_name}"
        cursor.execute("""
            INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
            VALUES (?, ?, ?, ?, ?)
        """, (tx_guid, trade_date, desc, fitid, jpy_guid))

        if '買付' in tx_type_raw:
            # Debit: Security (increase), Credit: Cash (decrease)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 1, ?, 10000)
            """, (uuid.uuid4().hex, tx_guid, security_account, int(settlement), int(units * 10000)))
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 1, ?, 1)
            """, (uuid.uuid4().hex, tx_guid, cash_account, -int(settlement), -int(settlement)))
            inv_type = 'BUY'
        elif '売却' in tx_type_raw:  # H1 修正: 3-way (Cash + Asset@cost + P/L)
            cost_per_unit, _ = calc_moving_avg_cost_per_unit(conn, security_account, trade_date)
            cap_gain = get_capital_gain_account_guid(conn)
            cap_loss = get_capital_loss_account_guid(conn)
            build_sell_splits(
                cursor, tx_guid,
                cash_account_guid=cash_account,
                security_account_guid=security_account,
                capital_gain_account_guid=cap_gain,
                capital_loss_account_guid=cap_loss,
                proceeds=settlement,
                units=units,
                cost_per_unit=cost_per_unit,
                value_denom=1,
                asset_qty_denom=10000,
            )
            inv_type = 'SELL'

        inv_tx_guid = uuid.uuid4().hex
        cursor.execute("""
            INSERT INTO investment_transactions (guid, tx_guid, security_guid, type, units, unit_price, total_amount, currency_guid, trade_date, settle_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (inv_tx_guid, tx_guid, security_account, inv_type, units, unit_price, settlement, jpy_guid, trade_date, settle_date))

        new_tx_count += 1

    conn.commit()
    conn.close()
    print(f"Imported {new_tx_count} domestic trades from {csv_path.name}")


def import_sbi_sec_csv(csv_path: Path):
    print(f"Importing {csv_path.name}...")

    # SBI のエクスポートはファイル名が SaveFile* 等で曖昧なため、まず内容1行目で種別判別する。
    text, _enc = _read_text(csv_path)
    head = '\n'.join(text.splitlines()[:3])

    if '保有証券一覧' in head:
        import_sbi_holdings_list(csv_path)
        return
    if '約定履歴' in head:
        # 『約定履歴』『約定履歴照会』いずれも、新旧両形式を import_sbi_domestic_trade_history が吸収する。
        import_sbi_domestic_trade_history(csv_path)
        return
    if '円貨入出金明細' in head:
        import_sbi_jpy_deposit_withdrawal(csv_path)
        return

    # 以降は従来のファイル名ベース判別（旧来のファイル群）
    name = csv_path.name.lower()
    if "detailinquiry" in name:
        import_sbi_jpy_deposit_withdrawal(csv_path)
    elif "nyushukkin" in name:
        import_sbi_deposit_withdrawal(csv_path)
    elif "yakujo" in name:
        import_sbi_trade_history(csv_path)
    elif "fundlist" in name:
        import_sbi_fund_list(csv_path)
    elif re.search(r'savefile_\d+_\d+', name):
        import_sbi_domestic_trade_history(csv_path)
    else:
        print(f"Unknown file type: {csv_path.name}")

def import_sbi_deposit_withdrawal(csv_path: Path):
    """
    Import Foreign Currency Deposit/Withdrawal (nyushukkin)
    Encoding: UTF-8-SIG
    """
    try:
        # Dynamically find header row
        header_row = -1
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            for i, line in enumerate(f):
                if '入出金日' in line and '区分' in line:
                    header_row = i
                    print(f"Found header at row {i}: {line.strip()}")
                    break
        
        if header_row == -1:
            print(f"Header not found in {csv_path.name}")
            return

        # Use skiprows to accurately target the header line
        df = pd.read_csv(csv_path, encoding='utf-8-sig', skiprows=header_row, header=0)
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    # Check columns
    required = ['入出金日', '区分', '通貨', '摘要', '出金額', '入金額']
    if not all(col in df.columns for col in required):
        print(f"Missing columns in {csv_path.name}. Found: {df.columns}")
        return

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    sbi_usd_cash_account = get_or_create_account_guid(conn, ['Assets', 'Bank', 'SBI Securities', 'USD'], 'ASSET', 'BANK')
    sbi_net_bank_account = get_or_create_account_guid(conn, ['Assets', 'Bank', 'SBI Sumishin Net Bank'], 'ASSET', 'BANK')
    # Use a generic transfer account if the description doesn't match known banks
    transfer_account = get_or_create_account_guid(conn, ['Assets', 'Transfer'], 'ASSET') 
    
    usd_guid = get_currency_guid(conn, 'USD')
    
    conn.commit()

    new_tx_count = 0
    
    for _, row in df.iterrows():
        date = parse_sbi_date(row['入出金日'])
        if not date: continue
        
        desc = row['摘要']
        currency = row['通貨'] # 米ドル
        in_amount = parse_amount(row['入金額'])
        out_amount = parse_amount(row['出金額'])
        
        # Determine Amount and Peer Account
        amount = 0.0
        peer_account_guid = transfer_account
        
        if '住信SBIネット銀行' in desc:
            peer_account_guid = sbi_net_bank_account
        
        if in_amount > 0:
            amount = in_amount
            # Deposit into SBI Sec USD
        elif out_amount > 0:
            amount = -out_amount
            # Withdraw from SBI Sec USD
        else:
            continue

        # Generate FITID
        # D/W dont have unique IDs in CSV. Use hash of fields.
        raw_str = f"SBISEC_DW:{date}:{desc}:{amount}:{currency}"
        fitid = 'SHA256:' + hashlib.sha256(raw_str.encode()).hexdigest()
        
        # Check duplicate
        cursor.execute("SELECT 1 FROM transactions WHERE ofx_fitid = ?", (fitid,))
        if cursor.fetchone():
            continue

        # Insert Transaction
        tx_guid = uuid.uuid4().hex
        cursor.execute("""
            INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
            VALUES (?, ?, ?, ?, ?)
        """, (tx_guid, date, desc, fitid, usd_guid))
        
        # Split 1: SBI Sec USD (Main)
        split1_guid = uuid.uuid4().hex
        cursor.execute("""
            INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
            VALUES (?, ?, ?, ?, 100, ?, 100)
        """, (split1_guid, tx_guid, sbi_usd_cash_account, int(amount * 100), int(amount * 100)))

        # Split 2: Peer (Bank or Transfer)
        split2_guid = uuid.uuid4().hex
        cursor.execute("""
            INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
            VALUES (?, ?, ?, ?, 100, ?, 100)
        """, (split2_guid, tx_guid, peer_account_guid, int(-amount * 100), int(-amount * 100)))

        new_tx_count += 1
    
    conn.commit()
    conn.close()
    print(f"Imported {new_tx_count} transactions from {csv_path.name}")


def import_sbi_trade_history(csv_path: Path):
    """
    Import Trade History (yakujo)
    Encoding: cp932
    """
    try:
        # Header at line 2 (index 2)
        df = pd.read_csv(csv_path, encoding='cp932', header=2)
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return

    # Columns: 国内約定日,通貨,銘柄名,取引,預り区分,約定数量,約定単価,国内受渡日,受渡金額
    # Note: '取引' values: '買付', '再投資', '売却' (Guessing '売却' for Sell)
    
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    sbi_usd_cash_account = get_or_create_account_guid(conn, ['Assets', 'Bank', 'SBI Securities', 'USD'], 'ASSET', 'BANK')
    income_div_account = get_or_create_account_guid(conn, ['Income', 'Dividend', 'USD'], 'INCOME')
    
    usd_guid = get_currency_guid(conn, 'USD')
    
    conn.commit()
    
    new_tx_count = 0

    for _, row in df.iterrows():
        trade_date = parse_sbi_date(row['国内約定日'])
        settle_date = parse_sbi_date(row['国内受渡日'])
        if not trade_date: continue
        
        security_name = row['銘柄名']
        tx_type = row['取引']
        units = parse_amount(row['約定数量'])
        unit_price = parse_amount(row['約定単価'])
        total_amount = parse_amount(row['受渡金額']) # Usually total value
        currency = row['通貨']
        
        # Determine Security Account
        # Use full name or extract code if possible. 
        # Example: "ブラックロック・スーパー・マネー・マーケット・ファンド（米ドル） X0934000"
        security_account = get_or_create_account_guid(
            conn, ['Assets', 'Investments', 'SBI Securities', security_name], 'ASSET', 'INVESTMENT'
        )

        # Generate FITID
        raw_str = f"SBISEC_TRADE:{trade_date}:{security_name}:{tx_type}:{units}:{total_amount}"
        fitid = 'SHA256:' + hashlib.sha256(raw_str.encode()).hexdigest()
        
        cursor.execute("SELECT 1 FROM transactions WHERE ofx_fitid = ?", (fitid,))
        if cursor.fetchone():
            continue

        # Insert Transaction
        tx_guid = uuid.uuid4().hex
        desc = f"{tx_type} {security_name}"
        cursor.execute("""
            INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid)
            VALUES (?, ?, ?, ?, ?)
        """, (tx_guid, trade_date, desc, fitid, usd_guid))
        
        # Determine logic based on type
        # '再投資' (Reinvest) -> Income:Dividend -> Security
        # '買付' (Buy) -> Bank/Cash -> Security
        # '売却' (Sell) -> Security -> Bank/Cash
        
        split_asset_guid = uuid.uuid4().hex
        split_funding_guid = uuid.uuid4().hex
        
        if tx_type == '再投資':
            # Debit Security (Increase)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 100, ?, 10000)
            """, (split_asset_guid, tx_guid, security_account, int(total_amount * 100), int(units * 10000)))
            
            # Credit Income (Increase in Income = Credit)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 100, ?, 100)
            """, (split_funding_guid, tx_guid, income_div_account, int(-total_amount * 100), int(-total_amount * 100)))
            
            # Investment Transaction Record
            inv_tx_guid = uuid.uuid4().hex
            cursor.execute("""
                INSERT INTO investment_transactions (guid, tx_guid, security_guid, type, units, unit_price, total_amount, currency_guid, trade_date, settle_date)
                VALUES (?, ?, ?, 'REINVEST', ?, ?, ?, ?, ?, ?)
            """, (inv_tx_guid, tx_guid, security_account, units, unit_price, total_amount, usd_guid, trade_date, settle_date))

        elif tx_type == '買付':
            # Debit Security (Increase)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 100, ?, 10000)
            """, (split_asset_guid, tx_guid, security_account, int(total_amount * 100), int(units * 10000)))

            # Credit Bank (Decrease)
            cursor.execute("""
                INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom)
                VALUES (?, ?, ?, ?, 100, ?, 100)
            """, (split_funding_guid, tx_guid, sbi_usd_cash_account, int(-total_amount * 100), int(-total_amount * 100)))

            # Investment Transaction Record
            inv_tx_guid = uuid.uuid4().hex
            cursor.execute("""
                INSERT INTO investment_transactions (guid, tx_guid, security_guid, type, units, unit_price, total_amount, currency_guid, trade_date, settle_date)
                VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?)
            """, (inv_tx_guid, tx_guid, security_account, units, unit_price, total_amount, usd_guid, trade_date, settle_date))

        elif '売却' in tx_type or '売付' in tx_type:  # H2 修正: 元 importer に欠落していた sell 分岐
            # H1 修正: 3-way (Cash + Asset@cost + P/L)、value_denom=100 (USD cents)
            cost_per_unit, _ = calc_moving_avg_cost_per_unit(conn, security_account, trade_date)
            cap_gain = get_capital_gain_account_guid(conn)
            cap_loss = get_capital_loss_account_guid(conn)
            build_sell_splits(
                cursor, tx_guid,
                cash_account_guid=sbi_usd_cash_account,
                security_account_guid=security_account,
                capital_gain_account_guid=cap_gain,
                capital_loss_account_guid=cap_loss,
                proceeds=total_amount,
                units=units,
                cost_per_unit=cost_per_unit,
                value_denom=100,
                asset_qty_denom=10000,
            )
            inv_tx_guid = uuid.uuid4().hex
            cursor.execute("""
                INSERT INTO investment_transactions (guid, tx_guid, security_guid, type, units, unit_price, total_amount, currency_guid, trade_date, settle_date)
                VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?)
            """, (inv_tx_guid, tx_guid, security_account, units, unit_price, total_amount, usd_guid, trade_date, settle_date))

        else:
            print(f"  Skipped unknown tx type: {tx_type}")
            cursor.execute("DELETE FROM transactions WHERE guid = ?", (tx_guid,))
            continue

        new_tx_count += 1
    
    conn.commit()
    conn.close()
    print(f"Imported {new_tx_count} trades from {csv_path.name}")


def import_sbi_jpy_deposit_withdrawal(csv_path: Path):
    """SBI証券 円貨入出金明細 (DetailInquiry_*.csv, UTF-8 BOM) を JPY_clearing へ取り込む。

    「SBIハイブリッド預金より/へ自動振替」はすでに realign_hybrid_to_sbi_sec_jpy で
    JPY_clearing に接続済みのためスキップする。それ以外の入出金（即時入金・定時定額買付
    金・源泉徴収など）のみ取り込む。
    """
    text, _enc = _read_text(csv_path)
    lines = text.splitlines()

    # ヘッダー行を探す
    header_row = -1
    for i, line in enumerate(lines):
        if '入出金日' in line and '摘要' in line and '出金額' in line:
            header_row = i
            break
    if header_row == -1:
        print(f"Header not found in {csv_path.name}")
        return

    df = pd.read_csv(io.StringIO(text), skiprows=header_row, header=0)
    required = ['入出金日', '摘要', '出金額', '入金額']
    if not all(c in df.columns for c in required):
        print(f"Missing columns in {csv_path.name}. Found: {list(df.columns)}")
        return

    # Hybrid 振替は realign 済みのためスキップ
    SKIP_PATTERNS = ['ハイブリッド預金より', 'ハイブリッド預金へ']

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    jpy_clearing = get_or_create_account_guid(
        conn, ['Assets', 'Bank', 'SBI Securities', 'JPY_clearing'], 'ASSET', 'BANK'
    )
    transfer_in  = get_or_create_account_guid(conn, ['Assets', 'Transfer'], 'ASSET')
    rakuten_bank = get_or_create_account_guid(
        conn, ['Assets', 'Bank', 'Rakuten Bank'], 'ASSET', 'BANK'
    )
    tax_expense  = get_or_create_account_guid(
        conn, ['Expenses', 'Tax', 'Capital Gains Tax'], 'EXPENSE'
    )
    income_unc   = get_or_create_account_guid(conn, ['Income', 'Uncategorized'], 'INCOME')
    expense_unc  = get_or_create_account_guid(conn, ['Expenses', 'Uncategorized'], 'EXPENSE')
    jpy_guid     = get_currency_guid(conn, 'JPY')
    conn.commit()

    new_count = 0
    for _, row in df.iterrows():
        date = parse_sbi_date(row['入出金日'])
        if not date:
            continue
        desc = str(row['摘要']).strip()
        if any(p in desc for p in SKIP_PATTERNS):
            continue

        in_amt  = parse_amount(row['入金額'])
        out_amt = parse_amount(row['出金額'])
        if in_amt == 0 and out_amt == 0:
            continue

        amount = in_amt if in_amt > 0 else -out_amt

        raw = f"SBISEC_JPY_DW:{date}:{desc}:{in_amt}:{out_amt}"
        fitid = 'SHA256:' + hashlib.sha256(raw.encode()).hexdigest()
        cursor.execute("SELECT 1 FROM transactions WHERE ofx_fitid=?", (fitid,))
        if cursor.fetchone():
            continue

        # 対向口座の決定
        if '楽天銀行' in desc:
            peer = rakuten_bank
        elif '源泉徴収' in desc or '譲渡益税' in desc:
            peer = tax_expense
        elif amount > 0:
            peer = transfer_in
        else:
            peer = expense_unc

        tx_guid = uuid.uuid4().hex
        cursor.execute(
            "INSERT INTO transactions (guid, post_date, description, ofx_fitid, currency_guid) VALUES (?,?,?,?,?)",
            (tx_guid, date, desc, fitid, jpy_guid)
        )
        cursor.execute(
            "INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom) VALUES (?,?,?,?,1,?,1)",
            (uuid.uuid4().hex, tx_guid, jpy_clearing, amount, amount)
        )
        peer_amount = -amount
        if peer == tax_expense:
            peer_amount = out_amt  # 費用は正値
        cursor.execute(
            "INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom, quantity_num, quantity_denom) VALUES (?,?,?,?,1,?,1)",
            (uuid.uuid4().hex, tx_guid, peer, peer_amount, peer_amount)
        )
        new_count += 1

    conn.commit()
    conn.close()
    print(f"SBI円貨入出金: {new_count}件取込 from {csv_path.name}")


if __name__ == '__main__':
    project_root = get_project_root()
    sbi_dir = project_root / "data/raw/sbi_sec"
    
    csv_files = glob.glob(str(sbi_dir / "*.csv"))
    if not csv_files:
        print("No CSV files found in data/raw/sbi_sec")
    else:
        for f in csv_files:
            import_sbi_sec_csv(Path(f))
