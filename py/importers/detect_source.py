from pathlib import Path
import re


_FILENAME_PATTERNS = [
    # 具体的なパターンを先にチェックして誤判定を防ぐ
    (r'assetbalance',          'asset_balance'),   # 資産残高
    (r'enavi\d+',              'rakuten_card'),    # 楽天カード e-NAVI
    (r'withdrawallist',        'rakuten_sec'),     # 楽天証券 入出金
    (r'adjusthistory\(jp\)',   'rakuten_sec'),     # 楽天証券 調整履歴
    (r'tradehistory\(invst\)', 'rakuten_sec'),     # 楽天証券 投信取引
    (r'tradehistory\(jp\)',    'rakuten_sec'),     # 楽天証券 国内株
    (r'^rb-',                  'rakuten_bank'),    # 楽天銀行
    (r'nyushukinmeisai',       'dneobank'),        # 住信SBIネット銀行（先にチェック）
    (r'nyushukkin',            'sbi_sec'),         # SBI証券 入出金
    (r'yakujo\d+',             'sbi_sec'),         # SBI証券 約定
    (r'fundlist\d+',           'sbi_sec'),         # SBI証券 ファンド
    (r'savefile',              'sbi_sec'),          # SBI証券 SaveFile（番号付き/なし/スペース入り含む）
]


def _read_lines(csv_path: Path, n: int = 30) -> list[str]:
    for enc in ['utf-8-sig', 'utf-8', 'cp932', 'euc-jp', 'sjis']:
        try:
            with open(csv_path, 'r', encoding=enc) as f:
                return [f.readline() for _ in range(n)]
        except (UnicodeDecodeError, ValueError):
            continue
    return []


def detect_source(csv_path: Path) -> str | None:
    """
    CSVファイルのソース種別を判定する。
    ファイル名パターンで判定し、一致しなければ先頭30行の内容で判定する。
    判定できない場合は None を返す。
    """
    name_lower = csv_path.name.lower()

    for pattern, source in _FILENAME_PATTERNS:
        if re.search(pattern, name_lower):
            return source

    # コンテンツベースのフォールバック
    lines = _read_lines(csv_path)
    content = ''.join(lines)

    # りそな銀行（全銀協系21列）。ファイル名がタイムスタンプ数字のみで名前判定不可のため内容で判定。
    if 'レコード区分' in content and '取扱日付' in content:
        return 'resona_bank'
    if '取引日' in content and '入出金(円)' in content:
        return 'rakuten_bank'
    if '利用日' in content and '利用店名・商品名' in content:
        return 'rakuten_card'
    if any('日付' in l and ('出金金額' in l or '入金金額' in l) for l in lines):
        return 'dneobank'
    if '入出金日' in content and '入金額[円]' in content:
        return 'rakuten_sec'
    # SBI証券: ファイル名パターン外のバリアント救済（保有証券一覧/約定履歴/円貨入出金明細等）
    if any(kw in content for kw in ('保有証券一覧', '約定履歴', '円貨入出金明細', '円貨操作履歴', '外貨入出金明細', '外貨操作履歴')):
        return 'sbi_sec'

    return None
