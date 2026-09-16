"""記帳サービス: 仕訳を書く唯一の経路。

なぜ集約するか(技術ノート 289c50d8ac): 冪等性が DB ではなく 6 つの importer の
各自実装に置かれ、`ofx_fitid` の生成キーが importer ごとに違っていた。とりわけ
dneobank の `fitid_prefix` のように、鍵が「業務イベントの同一性」ではなく
「取込時の設定」に依存すると、同じ CSV を別の設定で取り込めば別キーになり、
重複排除は原理的にすり抜ける。実測の被害は口座混入 ¥126,721 と再投資の二重計上 7 件。

このモジュールの約束:

  1. 鍵は内容だけから決まる  … `natural_key()` は (source, 口座, 日付, 摘要, 金額,
     残高, ファイル内連番) の決定的関数。呼び出し方(prefix 引数・DL 順・取込順)は
     一切入れない。設計の正本は love `docs/Doc_Import_Identity_Simulation.md` 結果 4
  2. 墓標を必ず見る            … 統合・削除済みの鍵(`merged_ofx_fitids`)は復活させない。
     従来これを見ていたのは dneobank だけだった
  3. 旧鍵とも突き合わせる      … v1 で既に入っている行は `legacy_fitids` で申告させ、
     二重計上にしない(移行期の共存。fossil の hash policy と同じ形)
  4. 借貸が合わない仕訳は書けない … 1 行 1 コミットではなく、1 業務イベントを 1 単位で書く
  5. 口数の分母を必ず持つ      … quantity_denom を落とした行が過去に二重計上を招いた

importer の仕事は「CSV → Posting の列」に縮む。記帳・鍵の生成・冪等の判定はここだけが行う。

fd 49afe4015a2c(集約) / 48df3d88fb53(DB 側の不変条件) / 91a1154a5c10(口座同定)
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass, field

FITID_PREFIX = "SHA256:"
POLICY_V2 = "v2"


@dataclass(frozen=True)
class Entry:
    """仕訳の 1 明細(split)。value は minor 単位の分子と分母で持つ。"""
    account_guid: str
    value_num: int
    value_denom: int = 1
    quantity_num: int | None = None
    quantity_denom: int | None = None
    memo: str | None = None

    def value(self) -> float:
        return self.value_num / self.value_denom


@dataclass(frozen=True)
class Posting:
    """1 業務イベント = ヘッダ 1 + 明細 n。自然キーの材料もここに載せる。

    account_key は「取込元の口座」を表す論理キー(口座同定の結果、または CSV の口座番号)。
    CSV に口座が書かれていない住信SBI では account_identity の判定結果を渡す。
    balance は CSV の残高欄(無ければ None)、seq は **同じ (日付, 摘要, 金額) が
    そのファイルの中で何本目か**(生の行番号ではない)。行番号にすると、期間が重なる
    2 回目のダウンロードで同じ取引の番号がずれて二重計上になる。出現順なら、
    ダウンロード範囲が変わっても同じ取引に同じ番号が付く。`SeqCounter` が数える。
    どちらも同日・同摘要・同額の正当な重複を分離するために鍵に入れる(結果 4)。
    """
    source: str
    account_key: str
    date: str
    description: str
    amount: float
    entries: tuple[Entry, ...]
    balance: float | None = None
    seq: int = 0
    currency_guid: str | None = None
    investment: dict | None = None
    legacy_fitids: tuple[str, ...] = field(default_factory=tuple)


class SeqCounter:
    """同じ (日付, 摘要, 金額) の出現回数を数える。1 ファイル = 1 インスタンス。

    生の行番号を使わないのは、期間が重なる再ダウンロードで番号がずれるため
    (ずれると同じ取引が別の鍵になり、二重計上になる)。
    """

    def __init__(self) -> None:
        self._seen: dict[tuple, int] = {}

    def next(self, date: str, description: str, amount: float) -> int:
        key = (date, " ".join(description.split()), _minor(amount))
        self._seen[key] = self._seen.get(key, 0) + 1
        return self._seen[key]


def _minor(value: float | None) -> str:
    """金額を丸め誤差に強い整数表現にする(1/100 単位)。None は空文字。"""
    if value is None:
        return ""
    return str(int(round(value * 100)))


def natural_key(p: Posting) -> str:
    """内容だけから決まる自然キー K。取込設定・取込順は含めない。"""
    return "|".join([
        p.source,
        p.account_key,
        p.date,
        " ".join(p.description.split()),
        _minor(p.amount),
        _minor(p.balance),
        str(p.seq),
    ])


def fitid_of(key: str) -> str:
    return FITID_PREFIX + hashlib.sha256(key.encode()).hexdigest()


def _known(conn: sqlite3.Connection, fitids: tuple[str, ...]) -> bool:
    """既に記帳済み、または墓標(統合・削除済み)に載っているか。"""
    if not fitids:
        return False
    marks = ",".join("?" * len(fitids))
    row = conn.execute(
        f"SELECT 1 FROM transactions WHERE ofx_fitid IN ({marks}) LIMIT 1", fitids).fetchone()
    if row:
        return True
    try:
        row = conn.execute(
            f"SELECT 1 FROM merged_ofx_fitids WHERE ofx_fitid IN ({marks}) LIMIT 1",
            fitids).fetchone()
    except sqlite3.OperationalError:
        return False   # 墓標の表が無い DB(初期化直後)
    return bool(row)


def validate(p: Posting) -> None:
    """書く前に形を検算する。1 つでも欠ければ書かない(黙って歪んだ仕訳を残さない)。"""
    if len(p.entries) < 2:
        raise ValueError(f"明細が {len(p.entries)} 本: 複式にならない({p.date} {p.description})")
    total = sum(e.value_num / e.value_denom for e in p.entries)
    if abs(total) > 0.005:
        raise ValueError(f"借貸が合わない({total:+.2f}): {p.date} {p.description}")
    for e in p.entries:
        if e.value_denom <= 0:
            raise ValueError(f"value_denom が {e.value_denom}: {p.date} {p.description}")
        if e.quantity_num is not None and not e.quantity_denom:
            raise ValueError(
                f"quantity_denom が無い: {p.date} {p.description}"
                "(分母を落とした行は過去に二重計上を招いた)")


def _validate_accounts(conn: sqlite3.Connection, p: Posting) -> None:
    """記帳先の口座が実在し、かつ葉であることを確かめる。

    splits.account_guid には FOREIGN KEY が宣言されているが、SQLite は
    `PRAGMA foreign_keys = ON` を各接続で立てないと強制しない。実データで
    存在しない口座を指す split が 1 件見つかり(楽天カード Opening Balance の
    借方、¥10,324,726)、BS がその額だけ合わなくなっていた。複式の合計は 0 で
    合うので貸借一致の検算をすり抜ける — ここで明示的に見る(fd bccbd3b2ee56)。

    placeholder(記帳できない親)への記帳も同じ症状を起こす。BS の集計は
    placeholder を除外するため、親に直接付いた split は静かに落ちる。
    """
    for e in p.entries:
        row = conn.execute(
            "SELECT COALESCE(placeholder, 0) FROM accounts WHERE guid = ?",
            (e.account_guid,)).fetchone()
        if row is None:
            raise ValueError(
                f"存在しない口座への記帳({e.account_guid}): {p.date} {p.description}")
        if row[0]:
            raise ValueError(
                f"placeholder への記帳({e.account_guid}): {p.date} {p.description}"
                "(親でなく葉の勘定を指すこと)")


def post(conn: sqlite3.Connection, p: Posting) -> str | None:
    """記帳する。既に入っていれば何もせず None を返す(冪等)。

    呼び出し側は commit を管理する(1 ファイル = 1 トランザクションにできる)。
    """
    validate(p)
    _validate_accounts(conn, p)
    key = natural_key(p)
    fitid = fitid_of(key)

    if _known(conn, (fitid,) + tuple(p.legacy_fitids)):
        return None

    tx_guid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO transactions (guid, post_date, description, ofx_fitid,"
        " fitid_policy, natural_key, currency_guid) VALUES (?,?,?,?,?,?,?)",
        (tx_guid, p.date, p.description, fitid, POLICY_V2, key, p.currency_guid))

    for e in p.entries:
        conn.execute(
            "INSERT INTO splits (guid, tx_guid, account_guid, memo, value_num, value_denom,"
            " quantity_num, quantity_denom) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, tx_guid, e.account_guid, e.memo, e.value_num, e.value_denom,
             e.quantity_num, e.quantity_denom))

    if p.investment:
        inv = p.investment
        conn.execute(
            "INSERT INTO investment_transactions (guid, tx_guid, security_guid, type, units,"
            " unit_price, commission, total_amount, currency_guid, trade_date, settle_date, memo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, tx_guid, inv["security_guid"], inv["type"], inv["units"],
             inv.get("unit_price", 0.0), inv.get("commission", 0.0), inv["total_amount"],
             p.currency_guid, inv.get("trade_date", p.date), inv.get("settle_date"),
             inv.get("memo")))

    return tx_guid


def entomb(conn: sqlite3.Connection, fitid: str, merged_into: str | None, note: str) -> None:
    """統合・削除した鍵を墓標に記録する(再取込で復活させないため)。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS merged_ofx_fitids (ofx_fitid TEXT PRIMARY KEY,"
        " merged_into_tx_guid TEXT, merged_at TEXT, note TEXT)")
    conn.execute(
        "INSERT OR IGNORE INTO merged_ofx_fitids (ofx_fitid, merged_into_tx_guid, merged_at, note)"
        " VALUES (?,?,datetime('now'),?)", (fitid, merged_into, note))
