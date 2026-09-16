"""BS が釣り合わない 2 つの原因を潰す(迷子 split / placeholder への記帳)。

## 修理 1: 迷子 split

症状: `generate_balance_sheet` が balanced=False / diff=-10,325,196。原因は
2024-12-12 の仕訳「楽天カード Opening Balance（enavi 未取込期間 2019-09〜2024-12-12
の補正）」(ofx_fitid=OB:RAKUTEN_CARD:2024-12-12) の借方 split が、accounts に
存在しない guid を指していること。迷子 split は DB 全体でこの 1 件だけ。

  貸方 ¥-10,324,726  Liabilities:Credit Card:Rakuten Card   ← 正しい
  借方 ¥ 10,324,726  (迷子 29f5b7ebf2e8439ca8d4826e2e00b9e4)

複式の value 合計自体は ¥0 で合っているため、貸借一致のテストも love check も
検出できない。`_compute_account_balances` は accounts と JOIN するので、口座に
紐づかないこの 1 本だけが集計から落ちて BS が合わなくなる。

繋ぎ直す先が Equity:Opening Balances である根拠は 2 つ:
  1. 迷子 guid は Equity:Opening Balances(29f5b7eb01c3435990ad53e18a77b3bb)と
     **先頭 8 桁 29f5b7eb が一致**する。偶然なら 1/4,294,967,296 なので guid の
     取り違え(生成時に先頭を流用した)とみるのが自然。
  2. 同口座の既存 2 件はいずれも「CSV 未収録分の補正」で用途が一致する
     (楽天銀行 CSV未収録取引補正 / SBI外貨積立→SBI証券振替補正)。

## 修理 2: placeholder 口座に付いた split

迷子を繋ぎ直しても BS は diff -470 で残った。原因は別で、2025-08-17 の
カード明細 ¥470 が **親の placeholder 口座 `Expenses:日用品` に直接付いている**
こと(ai_category_guid も同じ placeholder を指す = 分類器が大分類までしか決められ
なかったとき、葉でなく親に落ちる)。`_compute_account_balances` は placeholder=1 を
除外するので、この 1 本も集計から落ちる。

繋ぎ直す先は同じ親の「その他◯◯」葉。これは「大分類だけ決まって小分類が無い」
ときの既存の置き場で、他 6 カテゴリで実際に使われている(その他特別な支出 18 件、
その他通信費 6 件 ほか)。カテゴリ自体は動かさない。

受入条件:
  迷子 split                 1 件 → 0 件
  placeholder 上の split      1 件 → 0 件
  BS の balanced             False → True(diff 0)
  Liabilities:Rakuten Card   ¥-212,210 のまま(動かない)
  Bank ASSET 口座の残高       1 円も動かない
  複式の value 合計           ¥0 のまま
  Equity:Opening Balances    ¥21,936 → ¥10,346,662
  各カテゴリの大分類合計        変わらない(葉へ移すだけ)

冪等: 既に直っている修理は「対象なし」として飛ばす。

安全策: 既定は dry-run。--apply で初めて書き込み、その前に必ずバックアップを取る。
検証は同一トランザクション内で行い、1 つでも満たさなければ rollback する。

  uv run python scripts/fix_orphan_opening_balance_split.py            # dry-run
  uv run python scripts/fix_orphan_opening_balance_split.py --apply

fd bccbd3b2ee56
"""
import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.analysis.generate_balance_sheet import generate_balance_sheet  # noqa: E402

ORPHAN_TARGET = ("Equity", "Opening Balances")


def get_db_path() -> Path:
    config_path = project_root / "config/settings.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return project_root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        return project_root / "db/finance.db"


def account_paths(conn) -> dict[str, str]:
    rows = {g: (n, p) for g, n, p in conn.execute("SELECT guid, name, parent_guid FROM accounts")}

    def path(g):
        parts = []
        while g in rows:
            name, parent = rows[g]
            parts.append(name)
            g = parent
        return ":".join(reversed(parts))

    return {g: path(g) for g in rows}


def guid_of(paths, want):
    hits = [g for g, p in paths.items() if p == want]
    if len(hits) != 1:
        raise SystemExit(f"想定外: 勘定 {want} が {len(hits)} 件(1 件のはず)")
    return hits[0]


def orphan_splits(conn):
    """accounts に存在しない口座を指す split。"""
    return conn.execute(
        "SELECT s.guid, s.account_guid, t.post_date, t.description,"
        "       s.value_num*1.0/s.value_denom"
        "  FROM splits s LEFT JOIN accounts a ON a.guid = s.account_guid"
        "  JOIN transactions t ON t.guid = s.tx_guid"
        " WHERE a.guid IS NULL ORDER BY t.post_date").fetchall()


def placeholder_splits(conn):
    """placeholder(記帳できない親)に直接付いている split。"""
    return conn.execute(
        "SELECT s.guid, s.account_guid, t.post_date, t.description,"
        "       s.value_num*1.0/s.value_denom"
        "  FROM splits s JOIN accounts a ON a.guid = s.account_guid"
        "  JOIN transactions t ON t.guid = s.tx_guid"
        " WHERE COALESCE(a.placeholder, 0) = 1 ORDER BY t.post_date").fetchall()


def leaf_for(conn, paths, parent_guid: str) -> str:
    """大分類だけ決まった記帳の置き場 = 同じ親の「その他◯◯」葉。"""
    name = paths[parent_guid].rsplit(":", 1)[-1]
    want = f"{paths[parent_guid]}:その他{name}"
    return guid_of(paths, want)


def balance_of(conn, guid) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(value_num*1.0/value_denom), 0) FROM splits"
        " WHERE account_guid = ?", (guid,)).fetchone()[0]


def subtree_total(conn, paths, root_path: str) -> float:
    guids = [g for g, p in paths.items() if p == root_path or p.startswith(root_path + ":")]
    return sum(balance_of(conn, g) for g in guids)


def bank_balances(conn, paths) -> dict[str, float]:
    return {paths[g]: balance_of(conn, g) for (g,) in conn.execute(
        "SELECT guid FROM accounts WHERE account_type='ASSET' AND ofx_type='BANK'")}


def double_entry_sum(conn) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(value_num*1.0/value_denom), 0) FROM splits").fetchone()[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="実際に書き込む(既定は dry-run)")
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()

    db_path = args.db or get_db_path()
    conn = sqlite3.connect(db_path)
    paths = account_paths(conn)
    bs_before = generate_balance_sheet(str(db_path))
    print(f"DB: {db_path}")
    print(f"BS: balanced={bs_before['balanced']} diff={bs_before['diff']:,}\n")

    moves: list[tuple[str, str, str]] = []   # (split_guid, 移動先 guid, 説明)

    # --- 修理 1: 迷子 split ---
    found = orphan_splits(conn)
    print(f"修理 1 迷子 split: {len(found)} 件")
    if found:
        target = guid_of(paths, ":".join(ORPHAN_TARGET))
        for sg, ag, date, desc, amt in found:
            if ag[:8] != target[:8]:
                raise SystemExit(
                    f"想定外: 迷子 guid の先頭 8 桁が {':'.join(ORPHAN_TARGET)} と一致しない"
                    f"({ag[:8]} vs {target[:8]})。取り違えの根拠が崩れているので手で見ること")
            print(f"   {date} ¥{amt:>13,.0f}  {desc[:40]}")
            print(f"      → {':'.join(ORPHAN_TARGET)}")
            moves.append((sg, target, f"迷子 {ag}"))

    # --- 修理 2: placeholder への記帳 ---
    found = placeholder_splits(conn)
    print(f"\n修理 2 placeholder への記帳: {len(found)} 件")
    for sg, ag, date, desc, amt in found:
        leaf = leaf_for(conn, paths, ag)
        print(f"   {date} ¥{amt:>13,.0f}  {desc[:40]}")
        print(f"      {paths[ag]} → {paths[leaf]}")
        moves.append((sg, leaf, f"placeholder {paths[ag]}"))

    if not moves:
        print("\n対象なし。何もしない。")
        return

    before_banks = bank_balances(conn, paths)
    before_sum = double_entry_sum(conn)
    before_subtrees = {paths[ag].split(":")[0] + ":" + paths[ag].split(":")[1]: None
                       for _, ag, _, _, _ in placeholder_splits(conn)}
    before_subtrees = {k: subtree_total(conn, paths, k) for k in before_subtrees}

    if not args.apply:
        print("\n[dry-run] --apply で実行する")
        return

    stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.parent / f"finance.bak-bsrepair-{stamp}.db"
    shutil.copy2(db_path, backup)
    print(f"\nバックアップ: {backup}")

    try:
        conn.execute("BEGIN")
        conn.executemany("UPDATE splits SET account_guid = ? WHERE guid = ?",
                         [(t, sg) for sg, t, _ in moves])
        checks = [
            ("迷子 split が 0 件", not orphan_splits(conn)),
            ("placeholder への記帳が 0 件", not placeholder_splits(conn)),
            ("Bank ASSET 口座の残高が 1 円も動かない", before_banks == bank_balances(conn, paths)),
            ("複式の value 合計が変わらない",
             abs(double_entry_sum(conn) - before_sum) < 1e-6),
            ("大分類の合計が変わらない(葉へ移すだけ)",
             all(abs(subtree_total(conn, paths, k) - v) < 1e-6
                 for k, v in before_subtrees.items())),
        ]
        for label, ok in checks:
            print(f"  [{'OK' if ok else 'NG'}] {label}")
        if not all(ok for _, ok in checks):
            conn.execute("ROLLBACK")
            raise SystemExit("受入条件を満たさないので rollback した。DB は変更していない。")
        conn.execute("COMMIT")
    finally:
        conn.close()

    bs_after = generate_balance_sheet(str(db_path))   # 自分で開くので接続を閉じてから
    ok = bs_after["balanced"] and bs_after["diff"] == 0
    print(f"  [{'OK' if ok else 'NG'}] BS が釣り合う "
          f"(balanced={bs_after['balanced']} diff={bs_after['diff']:,})")
    if not ok:
        raise SystemExit(f"BS がまだ釣り合わない。バックアップ {backup} から戻して原因を調べること。")
    print(f"\n完了: split {len(moves)} 本を繋ぎ直した。")


if __name__ == "__main__":
    main()
