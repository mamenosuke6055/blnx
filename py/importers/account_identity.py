"""住信SBIネット銀行の口座同定 — CSV が口座を書かないための control artifact 層。

住信SBIの明細CSVの列は `日付 / 内容 / 出金 / 入金 / 残高 / メモ` のみで、どの口座の
明細かを書かない(りそなは口座番号列を持つ)。ファイル名も DL 連番だけ。そこで

  1. 通貨ヘッダ      … `残高(USD)` なら外貨預金。JPY 口座は候補から外れる
  2. 相手口座名の排除 … 内部振替の `内容` は相手口座を通貨込みで名指しする。
                        自分が相手として現れることはない(硬い制約・誤り 0)
  3. 残高連鎖        … 既知口座の (日付,摘要,金額)→残高 と一致し、最古行の始残が
                        既知の残高に接続するか

の 3 つで判定する。**判定できなければ None を返す = 取込拒否(indeterminate)**。
DL 順を信じる案は実データで誤判定 16% だったので採らない(順序は検算できない)。

宣言は fossil の control artifact に倣い append-only。誤りは取消(tag_type=0)を
追記して直し、既存行は書き換えない。

正本: fossil wiki `取込データモデル` / love `docs/Doc_Import_Identity_Simulation.md`
測量: 通貨 + 残高連鎖 で 21 ファイル中 誤判定 0 / 保留 0(ブートストラップ 4 本を除く)
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_registry (
    account_key   TEXT PRIMARY KEY,      -- 論理キー(fitid のスコープにも使う)
    account_path  TEXT NOT NULL,         -- finance.db の勘定パス(JSON list)
    currency      TEXT NOT NULL,         -- JPY / USD
    label         TEXT,                  -- 人が読む名前
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS account_declarations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_sha256   TEXT NOT NULL,          -- 対象 blob(raw_imports.sha256)
    account_key  TEXT NOT NULL,
    tag_type     INTEGER NOT NULL,       -- 1=宣言(単発) / 0=取消
    origin       TEXT NOT NULL,          -- human / chain
    evidence     TEXT,                   -- 判定根拠(通貨・排除・連鎖)
    declared_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_decl_sha ON account_declarations(raw_sha256);
"""

# 住信SBI の口座。目的別口座は増えるので registry は DB 側に置き、これは初期値。
SEED_REGISTRY = [
    ("dneobank_main_jpy", ["Assets", "Bank", "SBI Sumishin Net Bank"], "JPY", "代表口座(円)"),
    ("dneobank_main_usd", ["Assets", "Bank", "SBI Sumishin Foreign Currency - USD"], "USD",
     "代表口座(米ドル)"),
    ("dneobank_hybrid", ["Assets", "Bank", "SBI Sumishin Hybrid Deposit"], "JPY",
     "SBIハイブリッド預金"),
    ("dneobank_mokuteki_seikatsuboei",
     ["Assets", "Bank", "SBI Sumishin Mokutekibetsu - 生活防衛"], "JPY", "目的別口座(生活防衛)"),
]

# `内容` に現れたら「相手がその口座」= 自分はその口座ではない
_COUNTERPARTY_RULES = (
    ("米ドル", "代表口座", "dneobank_main_usd"),   # 「普通　米ドル　代表口座」
    (None, "代表口座", "dneobank_main_jpy"),       # 「普通　円　代表口座」「普通　代表口座」
    (None, "ハイブリッド", "dneobank_hybrid"),
    (None, "生活防衛", "dneobank_mokuteki_seikatsuboei"),
)


def init_identity_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for key, path, cur, label in SEED_REGISTRY:
        conn.execute(
            "INSERT OR IGNORE INTO account_registry (account_key, account_path, currency, label)"
            " VALUES (?, ?, ?, ?)", (key, json.dumps(path, ensure_ascii=False), cur, label))
    conn.commit()


def registry(conn: sqlite3.Connection) -> dict[str, dict]:
    init_identity_tables(conn)
    return {r[0]: {"account_path": json.loads(r[1]), "currency": r[2], "label": r[3]}
            for r in conn.execute(
                "SELECT account_key, account_path, currency, label FROM account_registry")}


# ---------------------------------------------------------------- 生CSVの読み

@dataclass
class Parsed:
    """1 ファイル分。金額は最小単位の整数(円=1倍、USD=セント)で持つ。"""
    currency: str                       # JPY / USD
    denom: int                          # 1 (JPY) / 100 (USD)
    rows: list = field(default_factory=list)     # [(日付, 摘要, 金額, 残高)] CSV掲載順
    counterparties: set = field(default_factory=set)

    @property
    def period(self) -> tuple[str, str]:
        ds = [r[0] for r in self.rows]
        return (min(ds), max(ds))


def _counterparties(desc: str) -> set[str]:
    d = (desc or "").replace("　", " ")
    out = set()
    for need, token, key in _COUNTERPARTY_RULES:
        if token in d and (need is None or need in d):
            out.add(key)
            break
    return out


def parse_dneobank(text: str) -> Parsed | None:
    """住信SBI の明細CSVを読む。通貨は列名(円/USD)で判別する。"""
    rdr = list(csv.DictReader(io.StringIO(text)))
    if not rdr:
        return None
    cols = list(rdr[0].keys())
    currency = "USD" if any("USD" in (c or "") for c in cols) else "JPY"
    unit = "USD" if currency == "USD" else "円"
    denom = 100 if currency == "USD" else 1
    col_out, col_in, col_bal = f"出金金額({unit})", f"入金金額({unit})", f"残高({unit})"
    if col_out not in cols or col_bal not in cols:
        return None

    def minor(s: str | None) -> int:
        s = (s or "").replace(",", "").strip()
        return int(round(float(s) * denom)) if s else 0

    p = Parsed(currency=currency, denom=denom)
    for r in rdr:
        if not r.get("日付"):
            continue
        desc = (r.get("内容") or "").strip()
        out_v, in_v = minor(r.get(col_out)), minor(r.get(col_in))
        p.rows.append((r["日付"].replace("/", "-"), desc,
                       -out_v if out_v else in_v, minor(r.get(col_bal))))
        p.counterparties |= _counterparties(desc)
    return p if p.rows else None


# ------------------------------------------------------------------ 残高連鎖

class _Chain:
    """既知口座の残高知識。ev=(日付,摘要,金額)→残高、open=当日始残。"""

    def __init__(self) -> None:
        self.ev: dict[tuple, int] = {}
        self.close: dict[str, int] = {}
        self.open: dict[str, int] = {}

    def add(self, rows: list[tuple]) -> None:
        for d, desc, amt, bal in rows:          # rows は新しい順
            self.ev[(d, desc, amt)] = bal
            if d not in self.close:             # その日で最初に現れる行 = 当日終残
                self.close[d] = bal
            self.open[d] = bal - amt            # 最後に残るのは当日最古行の直前残高

    def before(self, d: str) -> int | None:
        if d in self.open:
            return self.open[d]
        prev = [x for x in self.close if x < d]
        return self.close[max(prev)] if prev else None

    def score(self, rows: list[tuple]) -> tuple[int, int, bool]:
        """(一致イベント数, 矛盾イベント数, 最古行の始残が接続するか)"""
        agree = sum(1 for d, de, a, b in rows if self.ev.get((d, de, a)) == b)
        conflict = sum(1 for d, de, a, b in rows
                       if (d, de, a) in self.ev and self.ev[(d, de, a)] != b)
        d0, _de, a0, b0 = rows[-1]
        return agree, conflict, self.before(d0) == b0 - a0


def effective_declarations(conn: sqlite3.Connection) -> dict[str, str]:
    """blob sha256 → account_key。取消(tag_type=0)された宣言は落とす。"""
    init_identity_tables(conn)
    eff: dict[str, str] = {}
    for sha, key, tag in conn.execute(
            "SELECT raw_sha256, account_key, tag_type FROM account_declarations ORDER BY id"):
        if tag:
            eff[sha] = key
        elif eff.get(sha) == key:
            eff.pop(sha, None)
    return eff


def known_chains(conn: sqlite3.Connection, exclude_sha: str | None = None) -> dict[str, _Chain]:
    """宣言済み blob を対象期間の古い順に流し込んで、口座ごとの残高知識を作る。

    新しい期間から先に入れると既知残高との間に穴が空いて接続に失敗するため、
    順序は必ず「対象期間の古い順」(測量 B')。
    """
    eff = effective_declarations(conn)
    parsed: list[tuple[str, str, Parsed]] = []
    for sha, key in eff.items():
        if sha == exclude_sha:
            continue
        row = conn.execute("SELECT content FROM raw_imports WHERE sha256=?", (sha,)).fetchone()
        if not row:
            continue
        p = parse_dneobank(_decode(bytes(row[0])))
        if p:
            parsed.append((p.period[0], key, p))
    chains: dict[str, _Chain] = {}
    for _lo, key, p in sorted(parsed, key=lambda x: x[0]):
        chains.setdefault(key, _Chain()).add(p.rows)
    return chains


def _decode(blob: bytes) -> str:
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return blob.decode("cp932", "replace")


# -------------------------------------------------------------------- 判定

class AccountUndetermined(Exception):
    """口座を判定できない。取込を拒否する(推測して記帳しない)。"""

    def __init__(self, reason: str, candidates: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.candidates = candidates or []


@dataclass
class Identification:
    account_key: str | None      # None = 保留(取込拒否)
    reason: str
    candidates: list[str] = field(default_factory=list)

    @property
    def decided(self) -> bool:
        return self.account_key is not None


def identify(conn: sqlite3.Connection, parsed: Parsed,
             exclude_sha: str | None = None) -> Identification:
    """通貨 → 相手口座名の排除 → 残高連鎖 の順に候補を絞る。絞れなければ保留。"""
    reg = registry(conn)
    cands = {k for k, v in reg.items() if v["currency"] == parsed.currency}
    if not cands:
        return Identification(None, f"{parsed.currency} の口座が registry にない")
    used = []
    if len(cands) > 1:
        used.append("通貨")
    after_excl = cands - parsed.counterparties
    if after_excl and after_excl != cands:
        used.append("相手口座名")
        cands = after_excl
    if len(cands) == 1:
        return Identification(next(iter(cands)), "+".join(used) or "registry", sorted(cands))

    chains = known_chains(conn, exclude_sha=exclude_sha)
    hits = []
    for key in sorted(cands):
        ch = chains.get(key)
        if ch is None:
            continue
        agree, conflict, connects = ch.score(parsed.rows)
        if conflict == 0 and (agree > 0 or connects):
            hits.append((key, agree, connects))
    if len(hits) == 1:
        key, agree, connects = hits[0]
        used.append(f"残高連鎖(一致{agree}/接続{'○' if connects else '×'})")
        return Identification(key, "+".join(used), sorted(cands))
    if not hits:
        return Identification(None, "残高連鎖が接続しない(未宣言の新口座か期間の穴)", sorted(cands))
    return Identification(None, f"候補が絞れない({'/'.join(h[0] for h in hits)})", sorted(cands))


# ------------------------------------------------------------------ 宣言 API

def declare(conn: sqlite3.Connection, raw_sha256: str, account_key: str,
            origin: str = "human", evidence: str | None = None) -> None:
    init_identity_tables(conn)
    if account_key not in registry(conn):
        raise ValueError(f"未登録の口座キー: {account_key}")
    conn.execute(
        "INSERT INTO account_declarations (raw_sha256, account_key, tag_type, origin, evidence)"
        " VALUES (?, ?, 1, ?, ?)", (raw_sha256, account_key, origin, evidence))
    conn.commit()


def cancel(conn: sqlite3.Connection, raw_sha256: str, account_key: str,
           evidence: str | None = None) -> None:
    """取消も追記。既存行は書き換えない(fossil の cancel タグ)。"""
    init_identity_tables(conn)
    conn.execute(
        "INSERT INTO account_declarations (raw_sha256, account_key, tag_type, origin, evidence)"
        " VALUES (?, ?, 0, 'human', ?)", (raw_sha256, account_key, evidence))
    conn.commit()
