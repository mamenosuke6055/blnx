"""transactions に「鍵が必ずある」不変条件を入れる(ofx_fitid NOT NULL + 規則版 + 自然キー)。

背景: 冪等性が DB ではなく 6 つの importer の各自実装に置かれている
(技術ノート 289c50d8ac)。`ofx_fitid` は UNIQUE だが SQLite は NULL を重複扱いしないため、
鍵のない行は何件でも入り、再取込で必ず復活する。測量は
`love docs/Doc_Import_Identity_Simulation.md` 結果 2(後付けの衝突は 2 件まで減った)。

このマイグレーションで入れる制約:

    ofx_fitid TEXT NOT NULL UNIQUE            鍵のない行を物理的に書けなくする
    fitid_policy TEXT NOT NULL DEFAULT 'v1'   fossil の hash policy 相当。
                                              v1 = importer ごとの旧規則(既存行。書き換えない)
                                              v2 = 記帳サービスの単一規則
    natural_key TEXT UNIQUE                   v2 の K=(source,口座,日付,摘要,金額,残高,連番)
    CHECK (fitid_policy <> 'v2' OR natural_key IS NOT NULL)

既存 4,145 行は v1 のまま触らない(全面 replay は可能率 22.4% で不可能、結果 1)。
鍵のない手動仕訳 9 件(期末閉鎖 7 + 手動補正 2)には、既存の `ADJ:` 前例に倣って
決定的な人工キー `ADJ:manual:<日付>:<摘要と金額の sha256 先頭 12 桁>` を振る。
importer が生成する鍵空間(`SHA256:` / `NULLTX:`)とは接頭辞で分かれる。

安全策: 既定は dry-run。--apply で初めて書き込み、その前に必ずバックアップを取る。
検証は同一トランザクション内で行い、1 つでも満たさなければ rollback する。

  uv run python scripts/migrate_transactions_natural_key.py            # dry-run
  uv run python scripts/migrate_transactions_natural_key.py --apply

fd 48df3d88fb53 / 親 49afe4015a2c(記帳サービスへの集約)
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

NEW_SCHEMA = """
CREATE TABLE transactions_new (
    guid TEXT PRIMARY KEY NOT NULL UNIQUE,
    post_date TEXT NOT NULL,
    enter_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description TEXT,
    num TEXT,
    ofx_fitid TEXT NOT NULL UNIQUE,
    fitid_policy TEXT NOT NULL DEFAULT 'v1',
    natural_key TEXT UNIQUE,
    currency_guid TEXT,
    manual_category_guid TEXT,
    ai_category_guid TEXT,
    ai_confirmed_at TEXT,
    CHECK (ofx_fitid <> ''),
    CHECK (fitid_policy IN ('v1', 'v2')),
    CHECK (fitid_policy <> 'v2' OR natural_key IS NOT NULL),
    FOREIGN KEY (currency_guid) REFERENCES currencies(guid),
    FOREIGN KEY (manual_category_guid) REFERENCES accounts(guid),
    FOREIGN KEY (ai_category_guid) REFERENCES accounts(guid)
)
"""

COPY = """
INSERT INTO transactions_new
    (guid, post_date, enter_date, description, num, ofx_fitid, fitid_policy,
     natural_key, currency_guid, manual_category_guid, ai_category_guid, ai_confirmed_at)
SELECT guid, post_date, enter_date, description, num, ofx_fitid, 'v1',
       NULL, currency_guid, manual_category_guid, ai_category_guid, ai_confirmed_at
  FROM transactions
"""


def get_db_path() -> Path:
    config_path = project_root / "config/settings.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return project_root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        return project_root / "db/finance.db"


def manual_key(conn, guid: str, post_date: str, description: str) -> str:
    """鍵のない手動仕訳に、内容だけから決まる人工キーを振る(取込設定に依存しない)。"""
    amount = conn.execute(
        "SELECT COALESCE(ROUND(SUM(CASE WHEN value_num > 0"
        "        THEN value_num * 1.0 / value_denom ELSE 0 END), 2), 0)"
        "  FROM splits WHERE tx_guid = ?", (guid,)).fetchone()[0]
    digest = hashlib.sha256(f"{description}|{amount}".encode()).hexdigest()[:12]
    return f"ADJ:manual:{post_date}:{digest}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="実際に書き込む(既定は dry-run)")
    args = ap.parse_args()

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    print(f"対象DB: {db_path}")

    before_tx = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    nulls = conn.execute(
        "SELECT guid, post_date, description FROM transactions"
        " WHERE ofx_fitid IS NULL OR ofx_fitid = '' ORDER BY post_date").fetchall()

    print(f"\n=== 鍵のない手動仕訳 {len(nulls)} 件に人工キーを振る ===")
    assigned = {}
    for guid, date, desc in nulls:
        key = manual_key(conn, guid, date, desc)
        assigned[guid] = key
        print(f"  {date}  {desc[:44]:<44} → {key}")

    if len(set(assigned.values())) != len(assigned):
        raise SystemExit("想定外: 人工キーが衝突した(摘要と金額が完全に同じ手動仕訳がある)")
    dup = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE ofx_fitid IN (%s)"
        % ",".join("?" * len(assigned) or "''"), tuple(assigned.values())).fetchone()[0] \
        if assigned else 0
    if dup:
        raise SystemExit(f"想定外: 人工キーが既存の ofx_fitid と衝突した({dup} 件)")

    print("\n=== 入れる制約 ===")
    print("  ofx_fitid    NOT NULL + UNIQUE + CHECK(<> '')")
    print("  fitid_policy NOT NULL DEFAULT 'v1' + CHECK(v1/v2)  既存行はすべて v1")
    print("  natural_key  UNIQUE + CHECK(v2 なら NOT NULL)      既存行はすべて NULL")

    if not args.apply:
        print(f"\n仕訳 {before_tx} 件は件数・内容ともそのまま(鍵の付与と表の作り直しのみ)")
        print("(dry-run。--apply で書き込みます)")
        return

    backup = db_path.with_name(
        f"{db_path.stem}.bak-naturalkey-{datetime.now():%Y%m%dT%H%M%SZ}.db")
    shutil.copy2(db_path, backup)
    print(f"\nバックアップ: {backup.name}")

    cur = conn.cursor()
    cur.execute("BEGIN")
    for guid, key in assigned.items():
        cur.execute("UPDATE transactions SET ofx_fitid = ? WHERE guid = ?", (key, guid))

    # 旧表に張られたインデックス(guid/ofx_fitid の自動索引を除く)を作り直すために覚えておく
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'transactions'"
        "   AND sql IS NOT NULL").fetchall()

    cur.executescript(NEW_SCHEMA)
    cur.execute(COPY)
    cur.execute("DROP TABLE transactions")
    cur.execute("ALTER TABLE transactions_new RENAME TO transactions")
    for (sql,) in idx:
        cur.execute(sql)

    after_tx = cur.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    failures = []
    if after_tx != before_tx:
        failures.append(f"仕訳の件数が {before_tx} → {after_tx} と変わった")
    n_null = cur.execute("SELECT COUNT(*) FROM transactions"
                         " WHERE ofx_fitid IS NULL OR ofx_fitid = ''").fetchone()[0]
    if n_null:
        failures.append(f"鍵のない仕訳が {n_null} 件残っている")
    n_dupkey = cur.execute("SELECT COUNT(*) FROM (SELECT ofx_fitid FROM transactions"
                           " GROUP BY ofx_fitid HAVING COUNT(*) > 1)").fetchone()[0]
    if n_dupkey:
        failures.append(f"鍵が重複している組が {n_dupkey} 件ある")
    orphan = cur.execute("SELECT COUNT(*) FROM splits s WHERE NOT EXISTS"
                         " (SELECT 1 FROM transactions t WHERE t.guid = s.tx_guid)").fetchone()[0]
    if orphan:
        failures.append(f"親のない split が {orphan} 件ある")
    orphan_inv = cur.execute(
        "SELECT COUNT(*) FROM investment_transactions it WHERE NOT EXISTS"
        " (SELECT 1 FROM transactions t WHERE t.guid = it.tx_guid)").fetchone()[0]
    if orphan_inv:
        failures.append(f"親のない投資明細が {orphan_inv} 件ある")
    unbalanced = cur.execute(
        "SELECT COUNT(*) FROM (SELECT tx_guid FROM splits GROUP BY tx_guid"
        " HAVING ABS(SUM(value_num * 1.0 / value_denom)) > 0.005)").fetchone()[0]
    if unbalanced:
        failures.append(f"貸借が合わない仕訳が {unbalanced} 件ある")

    # 制約が本当に効くか、その場で試す(効かない制約を入れても意味がない)
    for label, sql, params in [
        ("鍵なし", "INSERT INTO transactions (guid, post_date, ofx_fitid) VALUES ('_t1','2026-01-01',NULL)", ()),
        ("鍵の重複", "INSERT INTO transactions (guid, post_date, ofx_fitid)"
                     " SELECT '_t2','2026-01-01', ofx_fitid FROM transactions LIMIT 1", ()),
        ("v2 なのに自然キーなし",
         "INSERT INTO transactions (guid, post_date, ofx_fitid, fitid_policy)"
         " VALUES ('_t3','2026-01-01','SHA256:_probe','v2')", ()),
    ]:
        try:
            cur.execute(sql, params)
            failures.append(f"{label} の行が書けてしまった(制約が効いていない)")
            cur.execute("DELETE FROM transactions WHERE guid LIKE '\\_t%' ESCAPE '\\'")
        except sqlite3.IntegrityError:
            pass

    print("\n=== 修正後の検証 ===")
    print(f"  仕訳 {after_tx} 件 / 鍵なし {n_null} 件 / 鍵の重複 {n_dupkey} 組")
    print(f"  親のない split {orphan} 件 / 親のない投資明細 {orphan_inv} 件 / 貸借不一致 {unbalanced} 件")
    print(f"  制約の実地テスト(鍵なし・鍵の重複・v2 で自然キーなし)"
          f" {'すべて拒否された' if not failures else '問題あり'}")

    if failures:
        conn.rollback()
        print("\n受入条件を満たさないため rollback しました:")
        for f in failures:
            print(f"  ✗ {f}")
        print(f"DB は修正前のままです(バックアップ {backup.name} も残しています)")
        sys.exit(1)

    conn.commit()
    conn.execute("PRAGMA foreign_key_check")
    conn.close()
    print(f"\n✓ 受入条件をすべて満たしたのでコミットしました。復旧用: {backup.name}")


if __name__ == "__main__":
    main()
