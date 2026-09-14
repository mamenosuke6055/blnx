"""楽天証券 再投資 7 件の二重計上(fitid NULL 側)を除去する。

背景: 技術ノート 289c50d8ac 症状(2)。`ofx_fitid IS NULL` の行は
`SELECT 1 FROM transactions WHERE ofx_fitid = ?` にヒットせず、UNIQUE 制約も
SQLite は NULL を重複扱いしないため、旧 importer が fitid なしで書いた行と
現行 importer が書いた行が同じ事象で併存する。

対象は 2025-11-25 取込の旧版 7 件。同日・同摘要・同額で 2025-11-27 取込の
現行版が併存しており、両者の違いは次のとおり(現行版が正しい):

  旧版(除去): 相手勘定 Assets:Bank:Rakuten Securities(現金)  / quantity_denom NULL
  現行版(残): 相手勘定 Income:Dividend(分配金収益)            / quantity_denom 10000

分配金の再投資は現金を消費しないので、現金を貸方に立てる旧版は会計的に誤り。
旧版は口数の分母も欠落しており(2 口が 20000/10000 でなく 2/NULL)、この 7 件が
DB 全体の quantity_denom NULL のすべてである。

受入条件(2025-11-05 に両ファンドとも全部解約済みなので、残高はゼロが正):
  ノーザン・トラスト(楽天・米ドルMMF)  簿価 ¥61  → ¥0   / 口数 40 → 0
  J-REITオープン(年4回決算型)           簿価 ¥295 → ¥0   / 口数 142 → 0
  quantity_denom が NULL の split       7 件      → 0 件
  fitid NULL の transactions            16 件     → 9 件(期末閉鎖 7 + 手動補正 2)
  Assets:Bank:Rakuten Securities        ¥4,796    → ¥5,152(消費していない現金 ¥356 が戻る)

楽天証券の生 CSV は raw_imports に無い(archive 対象外の経路で取り込まれた)ため
replay では直せない。DELETE + バックアップ + merged_ofx_fitids への記録で行う。

安全策: 既定は dry-run。--apply で初めて書き込み、その前に必ずバックアップを取る。
検証は同一トランザクション内で行い、1 つでも満たさなければ rollback する。

  uv run python scripts/fix_rakuten_reinvest_duplicates.py            # dry-run
  uv run python scripts/fix_rakuten_reinvest_duplicates.py --apply

fd 012b5bf2f683 / 技術ノート 289c50d8ac 症状(2)
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

CASH_ACCOUNT = "Rakuten Securities"   # 旧版が誤って貸方に立てた現金口座
INCOME_ACCOUNT = "Dividend"           # 現行版(正)が貸方に立てる収益口座
EXPECTED_PAIRS = 7


def get_db_path() -> Path:
    config_path = project_root / "config/settings.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return project_root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        return project_root / "db/finance.db"


def amount(num, denom):
    return num / (denom or 1)


def splits_of(conn, tx_guid):
    return conn.execute(
        "SELECT s.guid, a.guid, a.name, a.account_type, s.value_num, s.value_denom,"
        "       s.quantity_num, s.quantity_denom"
        "  FROM splits s JOIN accounts a ON a.guid = s.account_guid"
        " WHERE s.tx_guid = ?", (tx_guid,)).fetchall()


def build_plan(conn) -> list[dict]:
    """除去対象と、その相方(残す側)の対応を作る。1 つでも形が違えば例外で止める。"""
    targets = conn.execute(
        "SELECT guid, post_date, description FROM transactions"
        " WHERE ofx_fitid IS NULL AND description LIKE '再投資%'"
        " ORDER BY post_date").fetchall()

    plan = []
    for guid, date, desc in targets:
        rows = splits_of(conn, guid)
        if len(rows) != 2:
            raise SystemExit(f"想定外: {date} {desc} の split が {len(rows)} 本(2 本のはず)")

        debit = [r for r in rows if r[4] > 0]
        credit = [r for r in rows if r[4] < 0]
        if len(debit) != 1 or len(credit) != 1:
            raise SystemExit(f"想定外: {date} {desc} が単純な 2 本立てでない")
        debit, credit = debit[0], credit[0]

        # 旧版の指紋: 口数の分母が無い + 貸方が現金口座
        if debit[7] is not None:
            raise SystemExit(f"想定外: {date} {desc} の借方に quantity_denom がある(旧版でない)")
        if credit[2] != CASH_ACCOUNT:
            raise SystemExit(f"想定外: {date} {desc} の貸方が {credit[2]}({CASH_ACCOUNT} のはず)")

        value = amount(debit[4], debit[5])
        units = amount(debit[6], debit[7])
        # 貸方(現金)側の口数。旧版はここが NULL なので口数集計には効かない
        cash_units = 0.0 if credit[6] is None else amount(credit[6], credit[7])

        # 相方(残す側)を同日・同摘要・fitid ありで引く
        others = conn.execute(
            "SELECT guid, ofx_fitid FROM transactions"
            " WHERE post_date = ? AND description = ? AND guid <> ? AND ofx_fitid IS NOT NULL",
            (date, desc, guid)).fetchall()
        if len(others) != 1:
            raise SystemExit(f"想定外: {date} {desc} の相方が {len(others)} 件(1 件のはず)")
        keep_guid, keep_fitid = others[0]

        krows = splits_of(conn, keep_guid)
        kdebit = [r for r in krows if r[4] > 0]
        kcredit = [r for r in krows if r[4] < 0]
        if len(kdebit) != 1 or len(kcredit) != 1:
            raise SystemExit(f"想定外: 残す側 {date} {desc} が単純な 2 本立てでない")
        kdebit, kcredit = kdebit[0], kcredit[0]

        if kdebit[1] != debit[1]:
            raise SystemExit(f"想定外: {date} {desc} の借方口座が両者で違う")
        if kcredit[2] != INCOME_ACCOUNT:
            raise SystemExit(f"想定外: 残す側 {date} {desc} の貸方が {kcredit[2]}"
                             f"({INCOME_ACCOUNT} のはず)")
        if abs(amount(kdebit[4], kdebit[5]) - value) > 1e-9:
            raise SystemExit(f"想定外: {date} {desc} の金額が両者で違う")
        if abs(amount(kdebit[6], kdebit[7]) - units) > 1e-9:
            raise SystemExit(f"想定外: {date} {desc} の口数が両者で違う")

        inv = conn.execute(
            "SELECT guid FROM investment_transactions WHERE tx_guid = ?", (guid,)).fetchall()

        plan.append({
            "tx_guid": guid, "date": date, "desc": desc,
            "fund_guid": debit[1], "fund_name": debit[2],
            "value": value, "units": units,
            "cash_guid": credit[1], "cash_units": cash_units,
            "keep_guid": keep_guid, "keep_fitid": keep_fitid,
            "inv_guids": [r[0] for r in inv],
        })

    if len(plan) != EXPECTED_PAIRS:
        raise SystemExit(f"想定外: 対象が {len(plan)} 件(技術ノートの {EXPECTED_PAIRS} 件と違う)")
    return plan


def balances(conn, account_guids) -> dict:
    out = {}
    for g in account_guids:
        row = conn.execute(
            "SELECT COALESCE(ROUND(SUM(s.value_num * 1.0 / s.value_denom), 2), 0),"
            "       COALESCE(SUM(s.quantity_num * 1.0 / COALESCE(NULLIF(s.quantity_denom, 0), 1)), 0)"
            "  FROM splits s WHERE s.account_guid = ?", (g,)).fetchone()
        out[g] = (row[0], row[1])
    return out


def health(conn) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "fitid_null": q("SELECT COUNT(*) FROM transactions WHERE ofx_fitid IS NULL"),
        "qty_denom_null": q("SELECT COUNT(*) FROM splits"
                            " WHERE quantity_num IS NOT NULL AND quantity_denom IS NULL"),
        "orphan_splits": q("SELECT COUNT(*) FROM splits s"
                           " WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.guid = s.tx_guid)"),
        "orphan_inv": q("SELECT COUNT(*) FROM investment_transactions it"
                        " WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.guid = it.tx_guid)"),
        "unbalanced_tx": q("SELECT COUNT(*) FROM (SELECT tx_guid FROM splits"
                           " GROUP BY tx_guid HAVING ABS(SUM(value_num * 1.0 / value_denom)) > 0.005)"),
        "tx_total": q("SELECT COUNT(*) FROM transactions"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="実際に書き込む(既定は dry-run)")
    args = ap.parse_args()

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    print(f"対象DB: {db_path}")

    plan = build_plan(conn)
    accounts = sorted({p["fund_guid"] for p in plan} | {p["cash_guid"] for p in plan})
    before_bal = balances(conn, accounts)
    before = health(conn)

    print(f"\n=== 除去対象 {len(plan)} 件(旧版: 貸方 {CASH_ACCOUNT} / 口数分母なし) ===")
    for p in plan:
        print(f"  {p['date']} ¥{p['value']:>6,.0f} {p['units']:>6,.0f}口  {p['fund_name'][:42]}")
        print(f"      除去 tx={p['tx_guid'][:8]} (投資明細 {len(p['inv_guids'])} 件) "
              f"→ 残す tx={p['keep_guid'][:8]} fitid={p['keep_fitid'][:18]}…")

    # 期待する事後状態を除去分から計算する
    expect_bal = {g: list(v) for g, v in before_bal.items()}
    for p in plan:
        expect_bal[p["fund_guid"]][0] = round(expect_bal[p["fund_guid"]][0] - p["value"], 2)
        expect_bal[p["fund_guid"]][1] = expect_bal[p["fund_guid"]][1] - p["units"]
        expect_bal[p["cash_guid"]][0] = round(expect_bal[p["cash_guid"]][0] + p["value"], 2)
        expect_bal[p["cash_guid"]][1] = expect_bal[p["cash_guid"]][1] - p["cash_units"]

    names = {g: conn.execute("SELECT name FROM accounts WHERE guid = ?", (g,)).fetchone()[0]
             for g in accounts}
    print("\n=== 口座の簿価・口数(修正前 → 期待) ===")
    for g in accounts:
        print(f"  {names[g][:44]:<44} ¥{before_bal[g][0]:>10,.2f} → ¥{expect_bal[g][0]:>10,.2f}"
              f"   口数 {before_bal[g][1]:>8,.0f} → {expect_bal[g][1]:>8,.0f}")

    expect_health = dict(before)
    expect_health["fitid_null"] = before["fitid_null"] - len(plan)
    expect_health["qty_denom_null"] = 0
    expect_health["tx_total"] = before["tx_total"] - len(plan)
    print("\n=== 不変条件(修正前 → 期待) ===")
    for k in before:
        print(f"  {k:<16} {before[k]:>6} → {expect_health[k]:>6}")

    if not args.apply:
        print("\n(dry-run。--apply で書き込みます)")
        return

    backup = db_path.with_name(
        f"{db_path.stem}.bak-reinvestfix-{datetime.now():%Y%m%dT%H%M%SZ}.db")
    shutil.copy2(db_path, backup)
    print(f"\nバックアップ: {backup.name}")

    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute("CREATE TABLE IF NOT EXISTS merged_ofx_fitids (ofx_fitid TEXT PRIMARY KEY,"
                " merged_into_tx_guid TEXT, merged_at TEXT, note TEXT)")
    now = datetime.now().isoformat(timespec="seconds")
    for p in plan:
        # 旧版は fitid を持たないので、墓標の鍵は tx_guid から作る(importer の鍵空間と衝突しない)
        cur.execute("INSERT OR IGNORE INTO merged_ofx_fitids VALUES (?,?,?,?)",
                    (f"NULLTX:{p['tx_guid']}", p["keep_guid"], now,
                     f"fitid NULL の再投資 二重計上を除去(fd 012b5bf2f683) "
                     f"{p['date']} {p['desc']} ¥{p['value']:,.0f}"))
        cur.execute("DELETE FROM investment_transactions WHERE tx_guid = ?", (p["tx_guid"],))
        cur.execute("DELETE FROM splits WHERE tx_guid = ?", (p["tx_guid"],))
        cur.execute("DELETE FROM transactions WHERE guid = ?", (p["tx_guid"],))

    after_bal = balances(conn, accounts)
    after = health(conn)

    failures = []
    for g in accounts:
        if abs(after_bal[g][0] - expect_bal[g][0]) > 0.005:
            failures.append(f"{names[g]} の簿価 {after_bal[g][0]} ≠ 期待 {expect_bal[g][0]}")
        if abs(after_bal[g][1] - expect_bal[g][1]) > 1e-6:
            failures.append(f"{names[g]} の口数 {after_bal[g][1]} ≠ 期待 {expect_bal[g][1]}")
    for k, v in expect_health.items():
        if after[k] != v:
            failures.append(f"{k} = {after[k]} ≠ 期待 {v}")

    print("\n=== 修正後の検証 ===")
    for g in accounts:
        print(f"  {names[g][:44]:<44} ¥{after_bal[g][0]:>10,.2f}   口数 {after_bal[g][1]:>8,.0f}")
    for k in after:
        print(f"  {k:<16} {after[k]:>6}")

    if failures:
        conn.rollback()
        print("\n受入条件を満たさないため rollback しました:")
        for f in failures:
            print(f"  ✗ {f}")
        print(f"DB は修正前のままです(バックアップ {backup.name} も残しています)")
        sys.exit(1)

    conn.commit()
    conn.close()
    print(f"\n✓ 受入条件をすべて満たしたのでコミットしました。復旧用: {backup.name}")


if __name__ == "__main__":
    main()
