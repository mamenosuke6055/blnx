#!/usr/bin/env python3
"""迷子の投資口座を本体へ統合する（Investments 直下 → Rakuten Securities 配下）。

## 何が起きていたか

同じ銘柄が階層 1 段違いで 2 つの口座に割れていた:

    Assets > Investments > <銘柄>                     ← 迷子（時価スナップが付く）
    Assets > Investments > Rakuten Securities > <銘柄>  ← 本体（取引明細が付く）

結果、**簿価と時価が別口座に分かれる**（片肺）。集計が口座名をキーにしている間は
偶然マスクされていたが、guid キーへ移した瞬間に評価額が跳ぶ。

これは `merge_orphan_rakuten_card.py`（fossil 5826f95c93）と同じ型の 3 度目の再発。
迷子を作る経路自体は現行コードには無い（過去バージョンの遺物）が、
`import_rakuten_sec` のスナップ取込が口座を**名前のグローバル検索**で解決するため、
重複が残っている限り誤った guid にスナップが付き続ける。

## 何をするか

1. 迷子 → 本体 の対応を「同名・親が Investments / Rakuten Securities」で確定する
2. 二重計上の事前検証: 同じ (date, account) のスナップ、同じ (tx, account) の split が
   本体側に無いことを確認する
3. splits と asset_snapshots を本体 guid へ付け替える
4. 空になった迷子口座を削除する
5. 貸借一致が壊れていないことを事後検証する

既定は dry-run。--execute で実行し、その前に自動でバックアップを取る。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from py.util.db import get_db_path  # noqa: E402

ORPHAN_PARENT = "Investments"
REAL_PARENT = "Rakuten Securities"


def find_pairs(conn: sqlite3.Connection) -> list[dict]:
    """(迷子, 本体) の対応。同名・親違いで 1 対 1 に決まるものだけを返す。"""
    rows = conn.execute(
        """
        SELECT o.guid, o.name, r.guid
        FROM accounts o
        JOIN accounts po ON o.parent_guid = po.guid
        JOIN accounts r ON r.name = o.name AND r.guid <> o.guid
        JOIN accounts pr ON r.parent_guid = pr.guid
        WHERE po.name = ? AND pr.name = ? AND o.ofx_type = 'INVESTMENT'
        ORDER BY o.name
        """,
        (ORPHAN_PARENT, REAL_PARENT),
    ).fetchall()
    pairs: list[dict] = []
    seen: dict[str, int] = {}
    for orphan, name, real in rows:
        seen[orphan] = seen.get(orphan, 0) + 1
        pairs.append({"orphan": orphan, "name": name, "real": real})
    ambiguous = {g for g, n in seen.items() if n > 1}
    if ambiguous:
        raise SystemExit(
            f"迷子 {len(ambiguous)} 件が複数の本体候補を持つ。手で解決すること: {ambiguous}"
        )
    return pairs


def _counts(conn: sqlite3.Connection, guid: str) -> tuple[int, int]:
    s = conn.execute("SELECT COUNT(*) FROM splits WHERE account_guid = ?", (guid,)).fetchone()[0]
    x = conn.execute(
        "SELECT COUNT(*) FROM asset_snapshots WHERE account_guid = ?", (guid,)
    ).fetchone()[0]
    return int(s), int(x)


def check_collisions(conn: sqlite3.Connection, pair: dict) -> list[str]:
    """付け替えたときに二重計上・主キー衝突を起こさないか事前検証する。"""
    problems: list[str] = []
    dup_snap = conn.execute(
        """
        SELECT o.date FROM asset_snapshots o
        JOIN asset_snapshots r ON r.date = o.date AND r.account_guid = ?
        WHERE o.account_guid = ?
        """,
        (pair["real"], pair["orphan"]),
    ).fetchall()
    if dup_snap:
        problems.append(
            f"同日スナップが本体にもある: {sorted({d for (d,) in dup_snap})}"
        )
    dup_split = conn.execute(
        """
        SELECT o.tx_guid FROM splits o
        JOIN splits r ON r.tx_guid = o.tx_guid AND r.account_guid = ?
        WHERE o.account_guid = ?
        """,
        (pair["real"], pair["orphan"]),
    ).fetchall()
    if dup_split:
        problems.append(f"同一取引の split が本体にもある: {len(dup_split)} 件")
    return problems


def imbalance_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT t.guid FROM transactions t JOIN splits s ON s.tx_guid = t.guid
              WHERE s.value_denom = 1
              GROUP BY t.guid HAVING ABS(SUM(s.value_num * 1.0 / s.value_denom)) > 0.5)
            """
        ).fetchone()[0]
    )


def merge(conn: sqlite3.Connection, pairs: list[dict]) -> dict:
    moved_splits = moved_snaps = deleted = 0
    for p in pairs:
        cur = conn.execute(
            "UPDATE splits SET account_guid = ? WHERE account_guid = ?",
            (p["real"], p["orphan"]),
        )
        moved_splits += cur.rowcount
        cur = conn.execute(
            "UPDATE asset_snapshots SET account_guid = ? WHERE account_guid = ?",
            (p["real"], p["orphan"]),
        )
        moved_snaps += cur.rowcount
        s, x = _counts(conn, p["orphan"])
        if s or x:
            raise RuntimeError(f"迷子 {p['orphan'][:8]} が空になっていない (splits={s}, snaps={x})")
        conn.execute("DELETE FROM accounts WHERE guid = ?", (p["orphan"],))
        deleted += 1
    return {"splits": moved_splits, "snapshots": moved_snaps, "accounts": deleted}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="実際に書き込む（既定は dry-run）")
    ap.add_argument("--db", help="finance.db のパス（既定は設定から解決）")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else Path(get_db_path())
    conn = sqlite3.connect(db_path)

    pairs = find_pairs(conn)
    if not pairs:
        print("迷子の投資口座はありません。")
        return 0

    print(f"迷子 {len(pairs)} 件を検出（{ORPHAN_PARENT} 直下 → {REAL_PARENT} 配下）:\n")
    blocked = False
    for p in pairs:
        s, x = _counts(conn, p["orphan"])
        rs, rx = _counts(conn, p["real"])
        print(f"  {p['name']}")
        print(f"    迷子 {p['orphan'][:8]}  splits={s} snapshots={x}")
        print(f"    本体 {p['real'][:8]}  splits={rs} snapshots={rx}")
        for msg in check_collisions(conn, p):
            print(f"    ✗ {msg}")
            blocked = True
    if blocked:
        print("\n衝突があるため中止します。手で解決してください。")
        return 1

    before = imbalance_count(conn)
    print(f"\n事前の貸借不一致: {before} 件")

    if not args.execute:
        print("\n(dry-run) --execute で実行します。")
        return 0

    backup = db_path.with_name(
        f"{db_path.stem}.bak-{datetime.now():%Y%m%dT%H%M%S}{db_path.suffix}"
    )
    shutil.copy2(db_path, backup)
    print(f"\nバックアップ: {backup}")

    try:
        result = merge(conn, pairs)
        after = imbalance_count(conn)
        if after != before:
            raise RuntimeError(f"貸借不一致が変化した: {before} → {after}")
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"\n失敗のためロールバックしました: {e}")
        return 1

    print(
        f"統合しました: splits {result['splits']} 件 / snapshots {result['snapshots']} 件"
        f" を付け替え、迷子口座 {result['accounts']} 件を削除"
    )
    print(f"事後の貸借不一致: {after} 件（変化なし）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
