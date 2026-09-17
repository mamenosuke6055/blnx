"""明細を取り込めない自分の口座を作り、清算勘定に残った相手レグをそこへ集める。

清算勘定 Assets:Transfer は、自己資金移動の両側が DB にあれば 0 に閉じる。明細を
取り込めない口座(通帳だけの口座・解約済みの口座など)との間の移動は片側しか無いので、
Transfer に残差として溜まり、「本当に閉じるべき分」と見分けがつかなくなる。

そこで明細を持たない ASSET 口座を作り、摘要で特定できる移動の相手レグを Transfer から
移す。これで残差は名前のついた口座に分かれ、Transfer には未知の跨ぎだけが残る。

**この口座の残高は実残高ではない**(帳簿外の入出金が載らない)。ofx_type は NULL の
ままにする。BANK にすると流動性の集計(ofx_type='BANK' の合算)に混ざる。

任意で、ATM 出金のうち Expenses:Uncategorized に残っているものを ATM 引き出しの
費用勘定へ移す(辞書規則が未適用の分の補正)。ATM 出金は費用とみなし、手元現金の
口座は作らない。

対象の選び方:
  --deposit-keyword   入金のうち、相手が Transfer で摘要がこれを含むもの
  --outflow-keyword   出金のうち、相手が Transfer で摘要がこれを含むもの
                      (相手が既に Transfer = 自己振替の判断は済んでいるとみなす。
                       config の transfer_exclude_names に当たるものは除く)
  --atm-keyword       出金のうち、相手が Expenses:Uncategorized で摘要がこれを含むもの

受入条件:
  Transfer + 新口座の合計              変わらない(ASSET 内の付け替えのみ)
  Bank ASSET(ofx_type='BANK')の残高   1 円も動かない
  Expenses 合計                        変わらない(EXPENSE 内の付け替えのみ)
  貸借不一致                           増えない
  再実行時                             対象 0 件(冪等)

  uv run python scripts/fix_untracked_account_legs.py --account NAME \\
      --deposit-keyword KW [--outflow-keyword KW] [--atm-keyword KW]   # dry-run
  (同じ引数に --apply を足して書き込む)
"""
import argparse
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.processing.classify_bank_outflow import load_outflow_keywords  # noqa: E402
from scripts.fix_outflow_transfer_classify import (  # noqa: E402
    account_balance, account_paths, bank_balances, get_db_path, guid_of, imbalanced_count,
)

TRANSFER_ACCOUNT = ("Assets", "Transfer")
BANK_PARENT = ("Assets", "Bank")
EXPENSE_UNCATEGORIZED = ("Expenses", "Uncategorized")
ATM_EXPENSE = ("Expenses", "現金・カード", "ATM引き出し")


def peer_of(conn, split_guid: str, tx_guid: str) -> tuple[str, str] | None:
    """相手レグ(同一取引の他 split)が 1 本なら (account_guid, split_guid)、複数なら None。"""
    rows = conn.execute(
        "SELECT account_guid, guid FROM splits WHERE tx_guid = ? AND guid <> ?",
        (tx_guid, split_guid)).fetchall()
    return rows[0] if len(rows) == 1 else None


def build_plan(conn, paths, transfer_guid, uncat_guid, args) -> tuple[list[dict], list[dict]]:
    """(新口座へ移す split, ATM 引き出しへ移す split) を選ぶ。"""
    exclude_names = load_outflow_keywords()[1]
    all_keywords = (*args.deposit_keyword, *args.outflow_keyword, *args.atm_keyword)
    to_account, to_atm = [], []
    rows = conn.execute(
        "SELECT s.guid, s.tx_guid, s.account_guid, t.post_date, t.description,"
        "       s.value_num*1.0/s.value_denom"
        "  FROM splits s JOIN transactions t ON t.guid = s.tx_guid"
        "  JOIN accounts a ON a.guid = s.account_guid"
        " WHERE a.account_type = 'ASSET' AND a.ofx_type = 'BANK'"
        " ORDER BY t.post_date").fetchall()
    for bank_split, tx, bank_guid, date, desc, amt in rows:
        desc = desc or ""
        peer = peer_of(conn, bank_split, tx)
        if peer is None:
            # 複合仕訳(証券の約定など)。対象の摘要なのに複合なら判断を人に返す
            if any(k in desc for k in all_keywords):
                raise SystemExit(f"想定外: 対象摘要の取引 {tx} が複合仕訳 {date}")
            continue
        peer_acct, peer_split = peer
        item = {"split_guid": peer_split, "date": date, "desc": desc,
                "bank": paths[bank_guid], "amount": amt}
        if peer_acct == transfer_guid:
            is_deposit = amt > 0 and any(k in desc for k in args.deposit_keyword)
            is_outflow = (amt < 0 and any(k in desc for k in args.outflow_keyword)
                          and not any(n in desc for n in exclude_names))
            if is_deposit or is_outflow:
                to_account.append(item)
        elif peer_acct == uncat_guid and amt < 0 and any(k in desc for k in args.atm_keyword):
            to_atm.append(item)
    return to_account, to_atm


def expense_total(conn) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(s.value_num*1.0/s.value_denom), 0) FROM splits s"
        " JOIN accounts a ON a.guid = s.account_guid WHERE a.account_type = 'EXPENSE'"
    ).fetchone()[0]


def find_account(conn, paths, name: str) -> str | None:
    want = ":".join((*BANK_PARENT, name))
    hits = [g for g, p in paths.items() if p == want]
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", required=True, help="Assets:Bank 配下に作る口座名")
    ap.add_argument("--account-description", default="明細未取込。相手レグのみを持ち、残高は実残高ではない。",
                    help="新規作成時の accounts.description")
    ap.add_argument("--deposit-keyword", action="append", default=[])
    ap.add_argument("--outflow-keyword", action="append", default=[])
    ap.add_argument("--atm-keyword", action="append", default=[])
    ap.add_argument("--apply", action="store_true", help="実際に書き込む(既定は dry-run)")
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()
    if not (args.deposit_keyword or args.outflow_keyword or args.atm_keyword):
        ap.error("--deposit-keyword / --outflow-keyword / --atm-keyword のいずれかが必要")

    db_path = args.db or get_db_path()
    conn = sqlite3.connect(db_path)
    paths = account_paths(conn)
    transfer_guid = guid_of(conn, TRANSFER_ACCOUNT)
    uncat_guid = guid_of(conn, EXPENSE_UNCATEGORIZED)
    atm_guid = guid_of(conn, ATM_EXPENSE)
    bank_parent_guid = guid_of(conn, BANK_PARENT)
    account_guid = find_account(conn, paths, args.account)
    label = ":".join((*BANK_PARENT, args.account))

    to_acct, to_atm = build_plan(conn, paths, transfer_guid, uncat_guid, args)
    # 相手レグ側の金額は銀行レグの符号反転
    acct_delta = -sum(p["amount"] for p in to_acct)
    atm_delta = -sum(p["amount"] for p in to_atm)

    print(f"DB: {db_path}")
    print(f"{label}: {'既存 ' + account_guid if account_guid else '新規作成する'}\n")
    print(f"[A] Assets:Transfer → {label}: {len(to_acct)} 件")
    for p in to_acct:
        print(f"  {p['date']}  {p['bank']:<24} ¥{p['amount']:>10,.0f}  {p['desc'][:24]}")
    print(f"\n[B] Expenses:Uncategorized → ATM引き出し: {len(to_atm)} 件 ¥{atm_delta:,.0f}")
    for p in to_atm:
        print(f"  {p['date']}  {p['bank']:<24} ¥{p['amount']:>10,.0f}  {p['desc'][:24]}")

    before_transfer = account_balance(conn, transfer_guid)
    before_acct = account_balance(conn, account_guid) if account_guid else 0.0
    before_banks = bank_balances(conn, paths)
    before_expense = expense_total(conn)
    before_imbalanced = imbalanced_count(conn)
    print(f"\n  Assets:Transfer  ¥{before_transfer:>12,.0f} → ¥{before_transfer - acct_delta:>12,.0f}")
    print(f"  {label}  ¥{before_acct:>12,.0f} → ¥{before_acct + acct_delta:>12,.0f}")

    if not to_acct and not to_atm:
        print("\n対象なし。何もしない。")
        return
    if not args.apply:
        print("\n[dry-run] --apply で実行する")
        return

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = db_path.parent / f"finance.bak-untracked-{stamp}.db"
    shutil.copy2(db_path, backup)
    print(f"\nバックアップ: {backup}")

    try:
        conn.execute("BEGIN")
        if account_guid is None:
            account_guid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO accounts (guid, name, account_type, ofx_type, parent_guid,"
                " description, hidden, placeholder) VALUES (?, ?, 'ASSET', NULL, ?, ?, 0, 0)",
                (account_guid, args.account, bank_parent_guid, args.account_description))
        conn.executemany("UPDATE splits SET account_guid = ? WHERE guid = ?",
                         [(account_guid, p["split_guid"]) for p in to_acct])
        conn.executemany("UPDATE splits SET account_guid = ? WHERE guid = ?",
                         [(atm_guid, p["split_guid"]) for p in to_atm])

        paths_after = account_paths(conn)
        after_transfer = account_balance(conn, transfer_guid)
        after_acct = account_balance(conn, account_guid)
        again_acct, again_atm = build_plan(conn, paths_after, transfer_guid, uncat_guid, args)
        checks = [
            ("Transfer + 新口座の合計が変わらない",
             abs((after_transfer + after_acct) - (before_transfer + before_acct)) < 1e-6),
            ("新口座が対象額だけ動く", abs(after_acct - (before_acct + acct_delta)) < 1e-6),
            ("Bank ASSET(ofx_type='BANK')の残高が 1 円も動かない",
             bank_balances(conn, paths_after) == before_banks),
            ("Expenses 合計が変わらない", abs(expense_total(conn) - before_expense) < 1e-6),
            ("貸借不一致が増えていない", imbalanced_count(conn) <= before_imbalanced),
            ("再実行しても対象 0 件", not again_acct and not again_atm),
        ]
        for text, ok in checks:
            print(f"  [{'OK' if ok else 'NG'}] {text}")
        if not all(ok for _, ok in checks):
            conn.execute("ROLLBACK")
            raise SystemExit("受入条件を満たさないので rollback した。DB は変更していない。")
        conn.execute("COMMIT")
        print(f"\n完了: {label} へ {len(to_acct)} 件 / ATM引き出し {len(to_atm)} 件を付け替えた。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
