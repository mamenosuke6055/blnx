"""取込ゲート: 同一期間・別フォーマットの再取込（2フォーマット二重取込）を検出する。

sha256（完全同一ファイル）や ofx_fitid / card_dedup_key（行・正規化マーチャント）では
捕まえられない「同じ期間を別フォーマット / 確定状態でDLし直したCSV」を、raw_imports の
period 重複で検知して上申する。これが楽天カード Tier-3 二重計上（正規化をすり抜ける
ETC bare↔明細・表記揺れ）の発生源。設計は wiki [Dev_RawImports_Archive] §6。

判定は「上申」型: overlap を検知しても取込自体は止めない（冪等なので既存分は重複せず、
正当な追加取引は取り込まれる）。Tier-3 すり抜けの可能性を人間に気づかせ、必要なら
scripts/cleanup_rakutencard_tier3.py で確認する、という運用。

period を算出できる source のみが対象（現状: rakuten_card, rakuten_bank）。未対応 source は
None を返し、ゲートはスキップ（従来どおり取込）する。対応 source はマップに足すだけで増やせる。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from py.importers.import_rakutencard import guess_encoding
from py.importers import raw_archive


def _period_from_dates(series) -> tuple[str, str] | None:
    d = pd.to_datetime(series, errors="coerce").dropna()
    if d.empty:
        return None
    return d.min().strftime("%Y-%m-%d"), d.max().strftime("%Y-%m-%d")


def _period_rakuten_card(csv_path: Path, enc: str) -> tuple[str, str] | None:
    with open(csv_path, encoding=enc) as f:
        lines = f.readlines()
    hi = next((i for i, l in enumerate(lines)
               if "利用日" in l and "利用店名・商品名" in l), None)
    if hi is None:
        return None
    df = pd.read_csv(csv_path, encoding=enc, header=hi)
    if "利用日" not in df.columns:
        return None
    return _period_from_dates(df["利用日"])


def _period_rakuten_bank(csv_path: Path, enc: str) -> tuple[str, str] | None:
    df = pd.read_csv(csv_path, encoding=enc, header=0, on_bad_lines="skip")
    if "取引日" not in df.columns:
        return None
    # 取引日は YYYYMMDD（int で読まれるため astype(str) + format 明示。importer と同じ）
    dt = pd.to_datetime(df["取引日"].astype(str), format="%Y%m%d", errors="coerce")
    return _period_from_dates(dt)


# source -> period 抽出関数。未登録 source はゲート対象外（None）。
_PERIOD_EXTRACTORS = {
    "rakuten_card": _period_rakuten_card,
    "rakuten_bank": _period_rakuten_bank,
}


def extract_period(csv_path: Path | str, source: str | None) -> tuple[str, str] | None:
    """CSV がカバーする取引日範囲 (start, end) を返す。算出不能なら None。"""
    if source not in _PERIOD_EXTRACTORS:
        return None
    csv_path = Path(csv_path)
    enc = guess_encoding(csv_path)
    if not enc:
        return None
    try:
        return _PERIOD_EXTRACTORS[source](csv_path, enc)
    except Exception:
        return None


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """日付区間 [a_start,a_end] と [b_start,b_end] が重なるか（ISO文字列の辞書順比較）。"""
    return not (a_end < b_start or a_start > b_end)


def find_period_overlaps(
    raw_conn: sqlite3.Connection,
    source: str,
    start: str,
    end: str,
    exclude_id: int | None = None,
) -> list[dict]:
    """同 source で period が [start,end] と重なる既存アーカイブを返す（上申材料）。

    exclude_id で「今アーカイブしたばかりの自分自身」を除外する。period 未充填の
    既存（period_start/end が NULL）は判定対象外。
    """
    out = []
    for r in raw_archive.list_archived(raw_conn, source=source):
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        ps, pe = r["period_start"], r["period_end"]
        if ps and pe and _overlaps(start, end, ps, pe):
            out.append(r)
    return out
