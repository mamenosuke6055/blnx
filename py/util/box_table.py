"""sqlite `.mode box` 風の罫線テーブルを描く小さなレンダラ。

日本語ラベル（全角=表示幅2）が混ざっても桁が揃うよう East Asian Width で
セル幅を計算する。外部依存なし（unicodedata 標準ライブラリのみ）。

    print(render_box(["id", "label"], [["1", "家賃"], ["2", "旅行"]]))
    ┌────┬────────┐
    │ id │ label  │
    ├────┼────────┤
    │ 1  │ 家賃   │
    │ 2  │ 旅行   │
    └────┴────────┘
"""

from __future__ import annotations

from unicodedata import east_asian_width


def display_width(s: str) -> int:
    """端末表示幅。全角(W)/全角化(F)は 2、それ以外は 1 として数える。"""
    return sum(2 if east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int, align: str) -> str:
    gap = width - display_width(s)
    if gap <= 0:
        return s
    return " " * gap + s if align == "r" else s + " " * gap


def render_box(
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str] | None = None,
) -> str:
    """sqlite box 風の表を文字列で返す。

    headers: 見出し行。rows: 各行のセル（文字列化済み）。
    aligns: 列ごとの寄せ 'l'/'r'（既定は全て 'l'）。
    """
    ncol = len(headers)
    if aligns is None:
        aligns = ["l"] * ncol

    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max(display_width(headers[i]), *(display_width(r[i]) for r in cells)) if cells
        else display_width(headers[i])
        for i in range(ncol)
    ]

    def border(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def data_row(values: list[str]) -> str:
        return "│" + "│".join(
            " " + _pad(values[i], widths[i], aligns[i]) + " " for i in range(ncol)
        ) + "│"

    lines = [
        border("┌", "┬", "┐"),
        data_row(headers),
        border("├", "┼", "┤"),
    ]
    lines += [data_row(r) for r in cells]
    lines.append(border("└", "┴", "┘"))
    return "\n".join(lines)
