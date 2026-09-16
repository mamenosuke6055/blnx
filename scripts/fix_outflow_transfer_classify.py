"""銀行出金のうち自己資金移動を Expenses:Uncategorized から Assets:Transfer へ遡及する。

背景: 銀行 importer は入金側だけ classify を通し、出金側はカード引落キーワード以外
すべて Expenses:Uncategorized に落としていた(fd fc45d9de3b2a)。このため

  - 自分の別口座への振替・証券への日次積立が「費用」として P/L を膨らませ、
  - 清算勘定 Assets:Transfer が片肺(入金側だけ付け替え済み)になって
    残差が実データで -403,675 まで開いた(fd 4c39128ddc06)。

判定は importer と同じ ``classify_outflow`` を使う(遡及と新規取込で規則が割れない)。

**遡及しないもの**(いずれも本人の判断で費用のまま残す):
  - 奨学金返済 71 件 ¥1,887,209 — 負債の減少だが Liabilities に口座が無く、
    開始残高(未返済残高)を入れずに付け替えると純資産が +¥1,887,209 跳ねる。
  - ATM 現金引出 5 件 ¥25,000 — 手元現金の口座が無く、清算しても閉じない。
  - 同姓の別人への送金 2 件 ¥19,600 — 自己振替ではない(config の
    transfer_exclude_names で除外)。

受入条件:
  Expenses:Uncategorized 借方合計   付け替えた分だけ減る
  Assets:Transfer 残高             同額だけ増える(-403,675 → -9,931)
  Bank ASSET 口座の残高             **1 円も動かない**(VE が読む面を触っていないこと)
  貸借不一致                        0 件のまま
  除外対象(奨学金・ATM・同姓の別人)   1 件も動いていない

安全策: 既定は dry-run。--apply で初めて書き込み、その前に必ずバックアップを取る。
検証は同一トランザクション内で行い、1 つでも満たさなければ rollback する。

  uv run python scripts/fix_outflow_transfer_classify.py            # dry-run
  uv run python scripts/fix_outflow_transfer_classify.py --apply

fd fc45d9de3b2a
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

from py.processing.classify_bank_outflow import classify_outflow  # noqa: E402

EXPENSE_ACCOUNT = ("Expenses", "Uncategorized")
TRANSFER_ACCOUNT = ("Assets", "Transfer")


def get_db_path() -> Path:
    config_path = project_root / "config/settings.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return project_root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        return project_root / "db/finance.db"


def account_paths(conn) -> dict[str, str]:
    rows = {g: (n, p) for g, n, p in conn.execute(
        "SELECT guid, name, parent_guid FROM accounts")}

    def path(g):
        parts = []
        while g in rows:
            name, parent = rows[g]
            parts.append(name)
            g = parent
        return ":".join(reversed(parts))

    return {g: path(g) for g in rows}


def guid_of(conn, path: tuple[str, ...]) -> str:
    paths = account_paths(conn)
    want = ":".join(path)
    hits = [g for g, p in paths.items() if p == want]
    if len(hits) != 1:
        raise SystemExit(f"想定外: 勘定 {want} が {len(hits)} 件(1 件のはず)")
    return hits[0]


def bank_balances(conn, paths) -> dict[str, float]:
    """Bank ASSET 口座の残高(VE の liquid_balance / daily_net_flow が読む面)。"""
    out = {}
    for guid, name in conn.execute(
            "SELECT guid, name FROM accounts WHERE account_type='ASSET' AND ofx_type='BANK'"):
        v = conn.execute(
            "SELECT COALESCE(SUM(value_num*1.0/value_denom), 0) FROM splits"
            " WHERE account_guid = ?", (guid,)).fetchone()[0]
        out[paths[guid]] = v
    return out


def account_balance(conn, guid) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(value_num*1.0/value_denom), 0) FROM splits"
        " WHERE account_guid = ?", (guid,)).fetchone()[0]


def build_plan(conn, expense_guid: str, paths: dict[str, str]) -> list[dict]:
    """付け替える split を選ぶ。

    対象は「Expenses:Uncategorized の借方」かつ「同一取引の相手が Bank ASSET 口座だけ」
    かつ「摘要が自己資金移動と判定される」もの。カード明細(相手が Liabilities)は
    真の支出なので最初から外す。
    """
    plan = []
    rows = conn.execute(
        "SELECT s.guid, s.tx_guid, t.post_date, t.description,"
        "       s.value_num*1.0/s.value_denom AS amount"
        "  FROM splits s JOIN transactions t ON t.guid = s.tx_guid"
        " WHERE s.account_guid = ? AND s.value_num > 0"
        " ORDER BY t.post_date", (expense_guid,)).fetchall()

    for split_guid, tx_guid, date, desc, amt in rows:
        peers = conn.execute(
            "SELECT a.guid, a.account_type FROM splits s"
            "  JOIN accounts a ON a.guid = s.account_guid"
            " WHERE s.tx_guid = ? AND s.account_guid <> ?", (tx_guid, expense_guid)).fetchall()
        if not peers:
            raise SystemExit(f"想定外: {date} {desc} に相手勘定が無い")
        # 銀行口座かどうかは勘定パスで見る。ofx_type は一部の口座で NULL のまま
        # （りそな銀行など。fd で別途追跡）なので判定に使えない。
        if not all(t == "ASSET" and paths[g].startswith("Assets:Bank")
                   for g, t in peers):
            continue    # カード明細など。銀行出金ではない
        if classify_outflow(desc) is None:
            continue
        plan.append({"split_guid": split_guid, "tx_guid": tx_guid,
                     "date": date, "desc": desc, "amount": amt,
                     "peer": paths[peers[0][0]]})
    return plan


def imbalanced_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM (SELECT tx_guid FROM splits GROUP BY tx_guid"
        " HAVING ABS(SUM(value_num*1.0/value_denom)) > 1e-6)").fetchone()[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="実際に書き込む(既定は dry-run)")
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()

    db_path = args.db or get_db_path()
    conn = sqlite3.connect(db_path)
    paths = account_paths(conn)
    expense_guid = guid_of(conn, EXPENSE_ACCOUNT)
    transfer_guid = guid_of(conn, TRANSFER_ACCOUNT)

    plan = build_plan(conn, expense_guid, paths)
    total = sum(p["amount"] for p in plan)

    by_desc: dict[str, list[int, float]] = {}
    for p in plan:
        e = by_desc.setdefault(p["desc"][:34], [0, 0.0])
        e[0] += 1
        e[1] += p["amount"]

    print(f"DB: {db_path}")
    print(f"付け替え対象: {len(plan)} 件 ¥{total:,.0f}\n")
    for d, (n, a) in sorted(by_desc.items(), key=lambda kv: -kv[1][1]):
        print(f"  {n:4d} ¥{a:>10,.0f}  {d}")

    before_expense = account_balance(conn, expense_guid)
    before_transfer = account_balance(conn, transfer_guid)
    before_banks = bank_balances(conn, paths)
    before_imbalanced = imbalanced_count(conn)
    print(f"\n  Expenses:Uncategorized  ¥{before_expense:>12,.0f} → ¥{before_expense-total:>12,.0f}")
    print(f"  Assets:Transfer         ¥{before_transfer:>12,.0f} → ¥{before_transfer+total:>12,.0f}")

    if not plan:
        print("\n対象なし。何もしない。")
        return

    if not args.apply:
        print("\n[dry-run] --apply で実行する")
        return

    stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.parent / f"finance.bak-outflowclassify-{stamp}.db"
    shutil.copy2(db_path, backup)
    print(f"\nバックアップ: {backup}")

    try:
        conn.execute("BEGIN")
        conn.executemany(
            "UPDATE splits SET account_guid = ? WHERE guid = ?",
            [(transfer_guid, p["split_guid"]) for p in plan])

        after_expense = account_balance(conn, expense_guid)
        after_transfer = account_balance(conn, transfer_guid)
        after_banks = bank_balances(conn, paths)
        after_imbalanced = imbalanced_count(conn)
        moved = conn.execute(
            "SELECT COUNT(*) FROM splits WHERE account_guid = ? AND guid IN (%s)"
            % ",".join("?" * len(plan)),
            [transfer_guid] + [p["split_guid"] for p in plan]).fetchone()[0]

        checks = [
            (f"付け替えた split が {len(plan)} 件", moved == len(plan)),
            ("Expenses:Uncategorized が対象額だけ減る",
             abs(after_expense - (before_expense - total)) < 1e-6),
            ("Assets:Transfer が対象額だけ増える",
             abs(after_transfer - (before_transfer + total)) < 1e-6),
            ("Bank ASSET 口座の残高が 1 円も動かない", before_banks == after_banks),
            ("貸借不一致が増えていない", after_imbalanced <= before_imbalanced),
            ("Expenses:Uncategorized に自己資金移動が残っていない",
             not build_plan(conn, expense_guid, paths)),
        ]
        for label, ok in checks:
            print(f"  [{'OK' if ok else 'NG'}] {label}")
        if not all(ok for _, ok in checks):
            conn.execute("ROLLBACK")
            raise SystemExit("受入条件を満たさないので rollback した。DB は変更していない。")
        conn.execute("COMMIT")
        print(f"\n完了: {len(plan)} 件 ¥{total:,.0f} を Assets:Transfer へ付け替えた。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
