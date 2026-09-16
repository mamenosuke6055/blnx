"""銀行出金 description の分類。

出金側は長らく無分類だった。カード引落キーワードに当たらない出金はすべて
``Expenses:Uncategorized`` に落ち、自己資金移動（自分の別口座への振替・証券への
日次積立）が「費用」として P/L を膨らませていた。同時に、入金側だけが
``Assets:Transfer`` へ付け替えられていたため清算勘定が片肺になり、残差が
実データで -403,675 まで開いていた（fd fc45d9de3b2a / 4c39128ddc06）。

**入金側 :func:`classify_bank_income.classify_income` のルールをそのまま流用しては
いけない。** ``owner_transfer_names`` は姓の一致で判定するため、同姓の別人への
送金を自己振替と誤判定する（実データで 2 件 ¥19,600）。入金側なら「収入でない
ものを収入から外す」向きの誤りで害は小さいが、出金側では **真の支出を自己振替に
化けさせて隠す** 向きになる。そのため除外リスト ``transfer_exclude_names`` を
別に持ち、名義ルールより先に評価する。

判定できない摘要は ``None`` を返し、呼び出し側（インポーター）は従来どおり
``Expenses:Uncategorized`` にフォールバックさせて人間レビューに保留する。

個人固有のキーワード（本人名義・目的別口座の呼び名・同姓の別人）はコードに置かず、
リポジトリ管理外の ``config/classify_local.json`` から読む。
"""
from __future__ import annotations

from .classify_bank_income import TRANSFER, IncomeClass, _LOCAL_CONFIG, load_personal_keywords

import json
from pathlib import Path

# 銀行商品・口座種別の呼称（個人名は含まない = コードに置いてよい）。
# 住信SBI の CSV は振替先を摘要に書くため、これで自己口座間の移動が判定できる。
_SELF_MOVE_KEYWORDS: tuple[str, ...] = (
    "振替　ＳＢＩ証券",        # 住信SBI → SBI証券（日次積立の振替）
    "ＳＢＩハイブリッド預金",        # 代表口座 ⇄ ハイブリッド預金
    "普通　代表口座",
    "普通　円　代表口座",
    "普通　米ドル　代表口座",
    "積立　米ドル",
)

OutflowClass = IncomeClass  # 返す形は入金側と同じ（勘定パスと account_type）

Rules = tuple[tuple[tuple[str, ...], OutflowClass | None], ...]


def load_outflow_keywords(path: Path | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """出金側だけが使う個人固有キーワードを設定ファイルから読む。

    Returns:
        ``(self_account_names, transfer_exclude_names)``。
        ``self_account_names``   = 目的別口座など本人が付けた口座の呼び名。
        ``transfer_exclude_names`` = 本人と姓が同じ別人（自己振替と判定させない）。
        ファイルが無ければ空タプルの組。
    """
    p = _LOCAL_CONFIG if path is None else path
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ((), ())
    return (
        tuple(data.get("self_account_names", ())),
        tuple(data.get("transfer_exclude_names", ())),
    )


def build_rules(
    owner_transfer_names: tuple[str, ...] = (),
    self_account_names: tuple[str, ...] = (),
    transfer_exclude_names: tuple[str, ...] = (),
) -> Rules:
    """(キーワード群, 分類) のルール表を構築する。順に評価し最初のマッチを返す。

    評価順は重要: ``transfer_exclude_names`` を先頭に置き、``None``（= 費用のまま）へ
    倒す。後続の ``owner_transfer_names`` は姓一致なので、同姓の別人をここで
    止めないと自己振替に化ける。
    """
    return (
        # 同姓の別人（費用のまま。名義ルールより先に評価すること）
        (tuple(transfer_exclude_names), None),
        # 自己口座間の移動
        (
            (
                *_SELF_MOVE_KEYWORDS,
                *self_account_names,     # 目的別口座の呼び名
                *owner_transfer_names,   # 本人名義への送金（ことら送金・他行振込）
            ),
            TRANSFER,
        ),
    )


_RULES: Rules = build_rules(
    load_personal_keywords()[0],
    *load_outflow_keywords(),
)


def classify_outflow(description: str | None, rules: Rules | None = None) -> OutflowClass | None:
    """銀行出金の摘要を分類する。自己資金移動のみ返し、判定できなければ ``None``。

    Args:
        description: 取引摘要（transactions.description）。
        rules: 分類ルール表。省略時は ``config/classify_local.json`` を反映した
            モジュール既定（:func:`build_rules` で個別に構築して注入も可能）。

    Returns:
        自己資金移動なら :data:`classify_bank_income.TRANSFER`。それ以外は ``None``。
    """
    if not description:
        return None
    for keywords, klass in _RULES if rules is None else rules:
        if keywords and any(kw in description for kw in keywords):
            return klass
    return None
