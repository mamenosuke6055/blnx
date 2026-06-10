"""取込ウォーターマーク: 金融機関(source)ごとに「最後に取り込んだ記録の日付」と
「最後に取込を実行した日時」を finance.db から導出する。

目的(痛み①): CSV を再ダウンロードする際に「どの source をどの日付以降から
落とせばよいか」が分からない問題を解消する。新規ストレージは不要で、既存の
transactions(post_date / enter_date)から計算できる。

source キーは detect_source.py の判定キーに揃える(取込パイプラインと同じ語彙)。
post_date = 取引の実日付 → MAX が「最後の記録日」。enter_date = 取込実行日時。
次回DL目安 = 最後の記録日の翌日(銀行は重複DLしても ofx_fitid で冪等なので
余裕を持たせて当日を含めて落としてもよい)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from py.util.box_table import render_box


# (表示ラベル, source キー, 口座セレクタ)。
# 口座セレクタは {"names": [...]} か {"ofx_type": "INVESTMENT"} のどちらか。
# 現金/カード/証券キャッシュは口座名が source に 1:1 対応し、日付範囲DLの判断に有効。
# 投資商品(投信/株)は楽天証券と SBI証券で口座名が共有されるため source 別に割れず、
# ofx_type でまとめた参考バケットとして 1 行に集約する(保有/約定はスナップショットDLが主)。
SOURCE_ACCOUNTS: list[tuple[str, str, dict]] = [
    ("楽天銀行",            "rakuten_bank", {"names": ["Rakuten Bank"]}),
    ("楽天カード",          "rakuten_card", {"names": ["Rakuten Card"]}),
    ("楽天証券(入出金)",    "rakuten_sec",  {"names": ["Rakuten Securities"]}),
    ("住信SBIネット銀行",   "dneobank",     {"names": [
        "SBI Sumishin Net Bank",
        "SBI Sumishin Hybrid Deposit",
        "SBI Sumishin Mokutekibetsu - 生活防衛",
    ]}),
    ("りそな銀行",          "resona_bank",  {"names": ["Resona Bank"]}),
    ("投資商品(楽天/SBI)",  "sbi_sec",      {"ofx_type": "INVESTMENT"}),
]


# 休眠 source: 主口座切替(2025-12)で保持のみ・お金の出入りなし。これらの "遅れ" は
# バックログでなく休眠なので追わない(次回DL目安を出さない)。memory project_account_status 参照。
# 口座を再度切り替えたらこの集合を見直す。
DORMANT_SOURCES = {"rakuten_bank", "rakuten_sec"}


@dataclass
class Watermark:
    label: str
    source: str
    last_post_date: str | None      # YYYY-MM-DD（最後の記録日）
    last_import_at: str | None      # YYYY-MM-DD HH:MM:SS（最後の取込実行）
    tx_count: int
    next_download_from: str | None  # 次回DL目安（最後の記録日の翌日）
    dormant: bool = False           # 休眠口座(切替で保持のみ)。True なら追わない

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "source": self.source,
            "last_post_date": self.last_post_date,
            "last_import_at": self.last_import_at,
            "tx_count": self.tx_count,
            "next_download_from": self.next_download_from,
            "dormant": self.dormant,
        }


def _next_day(iso_date: str | None) -> str | None:
    if not iso_date:
        return None
    return (date.fromisoformat(iso_date) + timedelta(days=1)).isoformat()


def _query_one(conn: sqlite3.Connection, selector: dict) -> tuple[str | None, str | None, int]:
    cur = conn.cursor()
    if "names" in selector:
        names = selector["names"]
        placeholders = ",".join("?" * len(names))
        cur.execute(
            f"""
            SELECT MAX(t.post_date), MAX(t.enter_date), COUNT(DISTINCT t.guid)
            FROM transactions t
            JOIN splits s ON s.tx_guid = t.guid
            JOIN accounts a ON a.guid = s.account_guid
            WHERE a.name IN ({placeholders})
            """,
            names,
        )
    elif "ofx_type" in selector:
        cur.execute(
            """
            SELECT MAX(t.post_date), MAX(t.enter_date), COUNT(DISTINCT t.guid)
            FROM transactions t
            JOIN splits s ON s.tx_guid = t.guid
            JOIN accounts a ON a.guid = s.account_guid
            WHERE a.account_type = 'ASSET' AND a.ofx_type = ?
            """,
            (selector["ofx_type"],),
        )
    else:
        raise ValueError(f"未知の口座セレクタ: {selector}")
    last_post, last_import, count = cur.fetchone()
    return last_post, last_import, int(count or 0)


def compute_watermarks(conn: sqlite3.Connection) -> list[Watermark]:
    """SOURCE_ACCOUNTS の各 source について取込ウォーターマークを計算する。"""
    rows: list[Watermark] = []
    for label, source, selector in SOURCE_ACCOUNTS:
        last_post, last_import, count = _query_one(conn, selector)
        dormant = source in DORMANT_SOURCES
        rows.append(
            Watermark(
                label=label,
                source=source,
                last_post_date=last_post,
                last_import_at=last_import,
                tx_count=count,
                # 休眠口座には次回DL目安を出さない(追う対象でない)
                next_download_from=None if dormant else _next_day(last_post),
                dormant=dormant,
            )
        )
    return rows


def render_table(rows: list[Watermark]) -> str:
    """人間が読む sqlite box 風の表に整形して返す。稼働を上・休眠を下に並べる。"""
    headers = ["源泉", "状態", "最終記録日", "次回DL目安", "最終取込", "件数"]
    aligns = ["l", "l", "l", "l", "l", "r"]
    ordered = [r for r in rows if not r.dormant] + [r for r in rows if r.dormant]
    box_rows = [
        [r.label, "休眠" if r.dormant else "稼働",
         r.last_post_date or "—", r.next_download_from or "—",
         (r.last_import_at or "—")[:16], str(r.tx_count)]
        for r in ordered
    ]
    return render_box(headers, box_rows, aligns)
