"""銀行入金 description の分類。

銀行インポーターは従来、すべての入金を無条件で ``Income:Uncategorized`` に
計上していた。このため自己資金移動（本人名義振込・証券スイープ等）が「収入」として
P/L を膨らませる汚染が生じていた。

本モジュールは入金摘要を分類し、付け替え先の勘定を返す:

- ``TRANSFER``  : 本人名義振込・証券自動スイープ・定額自動入金・ATM 現金入金・振込組戻
                  → ``Assets:Transfer``（自己資金移動。ASSET なので P/L から除外される）
- ``SALARY``    : 給与・賞与            → ``Income:Salary``
- ``INTEREST``  : 利息・金利            → ``Income:Interest``

**確実に判定できるキーワードのみ**を分類し、未知の摘要は ``None`` を返す。
呼び出し側（インポーター）は ``None`` を従来どおり ``Income:Uncategorized`` に
フォールバックさせ、未知の将来入金を人間レビューに保留する。

本人名義・給与振込元の名義といった**個人固有のキーワードはコードに置かない**。
リポジトリ管理外の ``config/classify_local.json`` から読み込む（書式は
``config/classify_local.sample.json`` を参照。ファイルが無ければ一般語彙のみで分類する）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IncomeClass:
    """入金の分類結果。``account_path`` は勘定の階層パス。"""

    category: str  # 'TRANSFER' | 'SALARY' | 'INTEREST'
    account_path: tuple[str, ...]
    account_type: str  # 'ASSET' | 'INCOME'


TRANSFER = IncomeClass("TRANSFER", ("Assets", "Transfer"), "ASSET")
SALARY = IncomeClass("SALARY", ("Income", "Salary"), "INCOME")
INTEREST = IncomeClass("INTEREST", ("Income", "Interest"), "INCOME")

Rules = tuple[tuple[tuple[str, ...], IncomeClass], ...]

_LOCAL_CONFIG = Path(__file__).resolve().parents[2] / "config" / "classify_local.json"


def load_personal_keywords(path: Path | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """個人固有キーワード（本人名義・給与振込元名義）を設定ファイルから読む。

    Returns:
        ``(owner_transfer_names, salary_payer_names)``。ファイルが無ければ空タプルの組。
    """
    p = _LOCAL_CONFIG if path is None else path
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ((), ())
    return (
        tuple(data.get("owner_transfer_names", ())),
        tuple(data.get("salary_payer_names", ())),
    )


def build_rules(
    owner_transfer_names: tuple[str, ...] = (),
    salary_payer_names: tuple[str, ...] = (),
) -> Rules:
    """(キーワード群, 分類) のルール表を構築する。順に評価し最初のマッチを返す。

    評価順は重要:
      1. INTEREST を SALARY より先に置く。「給与・賞与・年金受取ボ－ナス金利利息」
         （楽天銀行の優遇金利。実体は利息で金額は数円）は '給与' を含むため。
      2. owner_transfer_names（本人名義）は給与振込元名義と被らない前提で
         TRANSFER を SALARY より先に評価する。

    Args:
        owner_transfer_names: 本人名義振込と判定する名義（全角/半角カナの表記揺れを列挙）。
        salary_payer_names: 給与振込元と判定する名義（「給与」等の語が摘要に無い場合の補完）。
    """
    return (
        # 利息・金利（'給与' を含む優遇金利 description 対策で最優先）
        (("利息", "金利"), INTEREST),
        # 自己資金移動（収入ではない。P/L から外す）
        (
            (
                *owner_transfer_names,  # 本人名義振込（投信現金化の振込名義含む）
                "自動スイ",  # 楽天証券 自動スイープ（証券→銀行）
                "テイガクジドウニユウキン",  # 定額自動入金（他行→当口座）
                "カ－ド入金",  # ATM 現金入金（長音符が半角ハイフン）
                "カード入金",  # 同上（長音符の表記揺れ）
                "組戻",  # 振込組戻（送金失敗の返金）
            ),
            TRANSFER,
        ),
        # 給与・賞与
        (("給与", "賞与", "キユウヨ", *salary_payer_names), SALARY),
    )


_RULES: Rules = build_rules(*load_personal_keywords())


def classify_income(description: str | None, rules: Rules | None = None) -> IncomeClass | None:
    """銀行入金の摘要を分類する。確実なもののみ返し、未知は ``None``。

    Args:
        description: 取引摘要（transactions.description）。
        rules: 分類ルール表。省略時は ``config/classify_local.json`` を反映した
            モジュール既定（:func:`build_rules` で個別に構築して注入も可能）。

    Returns:
        該当する :class:`IncomeClass`。判定できない場合は ``None``。
    """
    if not description:
        return None
    for keywords, klass in _RULES if rules is None else rules:
        if any(kw in description for kw in keywords):
            return klass
    return None
