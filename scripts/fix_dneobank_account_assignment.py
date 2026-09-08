"""住信SBI の口座混入・二重計上・USD の円建て記帳を、宣言済みアーカイブに基づいて遡及修正する。

背景: `import_dneobank_csv(csv_file)` が口座を渡されず、全 CSV が代表口座固定で
取り込まれていた(fd 91a1154a5c10)。さらに USD 建てファイルは 残高(円) 列が無いため
全行ゼロにパースされ、$75 が ¥75 として代表口座に記帳されていた。

対象は raw_imports.db に生CSVがあり口座宣言が済んでいる期間(2026-04-19〜09-04)のみ。
それ以前は生CSVが無いので触らない(genesis 封印の対象)。

修正は 4 段:
  (1) 口座の付け替え  … 宣言済み blob が示す口座へ splits.account_guid を移す
  (2) 二重計上の除去  … 生CSV に裏付けのない重複 tx を消す(archive 以前の import が
                        別の fitid 式を使っていたため、重なり期間で同じ事象が 2 回入った)
  (3) 通貨スケール    … USD を 1/100 単位で持ち直し、通貨も USD にする
  (4) 境界の残高調整  … archive 開始時点で「銀行が主張する残高」と DB がズレている分を
                        Equity へ明示的に記帳する(archive 以前の誤差。生CSVが無く直せない)

受入条件: 修正後の各口座残高が、生CSV の残高欄(銀行の主張)と一致すること。

安全策: 既定は dry-run。--apply で初めて書き込み、その前に必ずバックアップを取る。

  uv run python scripts/fix_dneobank_account_assignment.py            # dry-run
  uv run python scripts/fix_dneobank_account_assignment.py --apply
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.importers import account_identity as ai, raw_archive

# 旧 importer が使っていた口座群(混入先候補)
LEGACY_BANK_NAMES = ("SBI Sumishin Net Bank", "SBI Sumishin Hybrid Deposit",
                     "SBI Sumishin Mokutekibetsu - 生活防衛")


def v1_fitid(date, desc, amt_minor, bal_minor, denom):
    """旧実装の式を再現する(口座を含まない)。pandas 経由の数値表現に合わせる。"""
    def num(v):
        x = v / denom
        return int(x) if x == int(x) else x
    w = num(-amt_minor if amt_minor < 0 else 0)
    d = num(amt_minor if amt_minor > 0 else 0)
    raw = f"DNEOBANK:{date}:{desc}:{w}:{d}:{num(bal_minor)}"
    return "SHA256:" + hashlib.sha256(raw.encode()).hexdigest()


def get_db_path() -> Path:
    cfg = project_root / "config/settings.json"
    try:
        return project_root / json.loads(cfg.read_text(encoding="utf-8")).get(
            "db_path", "db/finance.db")
    except FileNotFoundError:
        return project_root / "db/finance.db"


def build_expected(raw_conn) -> dict:
    """宣言済み blob から fitid → 期待される口座/金額を作る。"""
    reg = ai.registry(raw_conn)
    eff = ai.effective_declarations(raw_conn)
    expected, collisions = {}, Counter()
    for sha, key in eff.items():
        row = raw_conn.execute("SELECT content FROM raw_imports WHERE sha256=?",
                               (sha,)).fetchone()
        if not row:
            continue
        parsed = ai.parse_dneobank(ai._decode(bytes(row[0])))
        if parsed is None:
            continue
        for date, desc, amt, bal in parsed.rows:
            if amt == 0:
                continue
            fid = v1_fitid(date, desc, amt, bal, parsed.denom)
            want = {"account_key": key, "path": reg[key]["account_path"],
                    "currency": reg[key]["currency"], "denom": parsed.denom,
                    "amount": amt, "date": date, "desc": desc}
            if fid in expected and expected[fid]["account_key"] != key:
                collisions[fid] += 1        # 口座を含まない v1 キーの衝突
            expected[fid] = want
    return expected, collisions


def account_guid(conn, path, create=False):
    import uuid
    guid, parent = None, None
    for name in path:
        row = conn.execute(
            "SELECT guid FROM accounts WHERE name=? AND (parent_guid IS ? OR parent_guid=?)",
            (name, parent, parent)).fetchone()
        if row is None:
            if not create:
                return None
            guid = uuid.uuid4().hex
            conn.execute("INSERT INTO accounts (guid, name, account_type, ofx_type, parent_guid)"
                         " VALUES (?,?,?,?,?)", (guid, name, 'ASSET', 'BANK', parent))
        else:
            guid = row[0]
        parent = guid
    return guid


def bank_truth(raw_conn) -> dict:
    """生CSV の残高欄から、口座ごとの (archive開始日, 銀行の始残, 最終残高) を出す。"""
    reg = ai.registry(raw_conn)
    eff = ai.effective_declarations(raw_conn)
    per = defaultdict(list)
    for sha, key in eff.items():
        row = raw_conn.execute("SELECT content FROM raw_imports WHERE sha256=?",
                               (sha,)).fetchone()
        if not row:
            continue
        p = ai.parse_dneobank(ai._decode(bytes(row[0])))
        if p:
            per[key].append(p)
    out = {}
    for key, ps in per.items():
        denom = ps[0].denom
        first = min(p.period[0] for p in ps)
        oldest = min((p.rows[-1] for p in ps if p.period[0] == first), key=lambda r: r[0])
        last_p = max(ps, key=lambda p: p.period[1])
        out[key] = {
            "start": first,
            "open": (oldest[3] - oldest[2]) / denom,     # 最古行の直前残高
            "final": last_p.rows[0][3] / denom,          # 最新行の残高
            "path": reg[key]["account_path"], "denom": denom,
            "currency": reg[key]["currency"],
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="実際に書き込む(既定は dry-run)")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    db_path = Path(a.db) if a.db else get_db_path()

    raw_conn = raw_archive.open_raw_db()
    expected, collisions = build_expected(raw_conn)
    reg = ai.registry(raw_conn)
    raw_conn.close()
    print(f"宣言済みアーカイブから期待値 {len(expected)} 行を構築"
          + (f" / v1 キーの衝突 {len(collisions)} 件" if collisions else ""))

    conn = sqlite3.connect(db_path)
    raw_conn = raw_archive.open_raw_db()
    truth = bank_truth(raw_conn)
    raw_conn.close()

    bank_guids = {}
    for name in LEGACY_BANK_NAMES:
        r = conn.execute("SELECT guid FROM accounts WHERE name=?", (name,)).fetchone()
        if r:
            bank_guids[r[0]] = name
    target_guids = {}
    for key, v in reg.items():
        g = account_guid(conn, v["account_path"], create=a.apply)
        if g:
            target_guids[key] = g
            bank_guids.setdefault(g, v["account_path"][-1])

    # ---- (1)(3) 付け替えとスケール ----
    plan_move, plan_scale, missing, ok = [], [], [], 0
    backed_tx = set()                      # 生CSVに裏付けのある tx
    for fid, want in expected.items():
        rows = conn.execute(
            "SELECT s.guid, s.account_guid, s.value_num, s.value_denom, t.guid"
            " FROM transactions t JOIN splits s ON s.tx_guid=t.guid"
            " WHERE t.ofx_fitid=?", (fid,)).fetchall()
        bank_side = [r for r in rows if r[1] in bank_guids]
        if not bank_side:
            missing.append(want)
            continue
        tgt = target_guids.get(want["account_key"])
        for sguid, aguid, vnum, vden, txguid in bank_side:
            backed_tx.add(txguid)
            if tgt is not None and aguid != tgt:
                plan_move.append((sguid, aguid, tgt, want, txguid))
            if vden != want["denom"]:
                plan_scale.append((sguid, txguid, vnum, vden, want))
            if (tgt is None or aguid == tgt) and vden == want["denom"]:
                ok += 1

    # ---- (0) 円口座に立っている非JPY(denom≠1)の split を外貨口座へ ----
    # 円建て口座が USD 建ての split を持つのは通貨の取り違え(SBI証券 importer 由来)。
    plan_currency_move = []
    usd_target = target_guids.get("dneobank_main_usd")
    if usd_target:
        jpy_guids = [g for g, n in bank_guids.items() if n != "SBI Sumishin Foreign Currency - USD"]
        for g in jpy_guids:
            for sguid, txguid, vnum, vden, date, desc in conn.execute(
                    "SELECT s.guid, t.guid, s.value_num, s.value_denom, t.post_date, t.description"
                    " FROM splits s JOIN transactions t ON t.guid=s.tx_guid"
                    " WHERE s.account_guid=? AND s.value_denom<>1", (g,)):
                plan_currency_move.append((sguid, txguid, vnum, vden, date, desc))

    # ---- (2) 二重計上: 裏付けのある tx と同じ(口座,日付,金額)で fitid が違うもの ----
    plan_dup = []
    for sguid, _a, tgt, want, txguid in plan_move + [
            (None, None, target_guids.get(w["account_key"]), w, None)
            for w in []]:
        pass
    for fid, want in expected.items():
        tgt = target_guids.get(want["account_key"])
        if tgt is None:
            continue
        amt = want["amount"] / want["denom"]
        for txg, other_fid in conn.execute(
                "SELECT t.guid, t.ofx_fitid FROM transactions t JOIN splits s ON s.tx_guid=t.guid"
                " WHERE t.post_date=? AND s.account_guid=?"
                " AND ABS(s.value_num*1.0/s.value_denom - ?) < 0.005", (want["date"], tgt, amt)):
            if other_fid not in expected and txg not in backed_tx:
                plan_dup.append((txg, other_fid, want))
    # 付け替え先での重複も見る(移動後に同じ口座へ並ぶもの)
    for sguid, _aguid, tgt, want, txguid in plan_move:
        amt = want["amount"] / want["denom"]
        for txg, other_fid in conn.execute(
                "SELECT t.guid, t.ofx_fitid FROM transactions t JOIN splits s ON s.tx_guid=t.guid"
                " WHERE t.post_date=? AND s.account_guid=?"
                " AND ABS(s.value_num*1.0/s.value_denom - ?) < 0.005", (want["date"], tgt, amt)):
            if other_fid not in expected and txg not in backed_tx:
                plan_dup.append((txg, other_fid, want))
    # 通貨移動で外貨口座へ移る split も、移動後に重複するなら除去対象
    for sguid, txguid, vnum, vden, date, desc in plan_currency_move:
        v = vnum / vden
        for fid, want in expected.items():
            if (want["account_key"] == "dneobank_main_usd" and want["date"] == date
                    and abs(want["amount"] / want["denom"] - v) < 0.005
                    and txguid not in backed_tx):
                other = conn.execute("SELECT ofx_fitid FROM transactions WHERE guid=?",
                                     (txguid,)).fetchone()
                plan_dup.append((txguid, other[0] if other else None, want))
                break

    seen_dup, dedup = set(), []
    for txg, fid, want in plan_dup:
        if txg not in seen_dup:
            seen_dup.add(txg)
            dedup.append((txg, fid, want))
    plan_dup = dedup

    print(f"\n=== 計画 ===")
    print(f"  (0) 非JPY split を外貨口座へ : {len(plan_currency_move)}")
    print(f"  (1) 口座を付け替える   : {len(plan_move)}")
    print(f"  (2) 二重計上を除去する : {len(plan_dup)} tx")
    print(f"  (3) 通貨スケールを直す : {len(plan_scale)}")
    print(f"      そのままでよい     : {ok}   / finance.db に無い行: {len(missing)}")

    by = Counter((bank_guids.get(m[1], "?"), m[3]["account_key"]) for m in plan_move)
    if by:
        print("\n  付け替えの内訳 (現在 → 正しい口座):")
        for (src, dst), n in by.most_common():
            print(f"    {src:<38} → {dst:<32} {n:>4} 件")
    if plan_dup:
        print("\n  除去する重複 (生CSVに裏付けが無い側):")
        for txg, fid, want in plan_dup[:12]:
            print(f"    {want['date']} {want['desc'][:22]:<24} {want['amount']/want['denom']:>+10,.2f}"
                  f"  {want['account_key']:<32} {(fid or '')[:18]}…")
        if len(plan_dup) > 12:
            print(f"    ... 他 {len(plan_dup) - 12} 件")

    if not a.apply:
        print("\n(dry-run。--apply で書き込みます)")
        conn.close()
        return

    backup = db_path.with_name(f"{db_path.stem}.bak-accountfix-"
                               f"{datetime.now():%Y%m%dT%H%M%S}{db_path.suffix}")
    shutil.copy2(db_path, backup)
    print(f"\nバックアップ: {backup.name}")

    cur = conn.cursor()
    for sguid, _tx, _vn, _vd, _d, _de in plan_currency_move:
        cur.execute("UPDATE splits SET account_guid=? WHERE guid=?", (usd_target, sguid))
    for sguid, _aguid, tgt, _want, _tx in plan_move:
        cur.execute("UPDATE splits SET account_guid=? WHERE guid=?", (tgt, sguid))
    usd_guid = conn.execute("SELECT guid FROM currencies WHERE mnemonic='USD'").fetchone()
    for sguid, txguid, vnum, vden, want in plan_scale:
        # 相手勘定側も同じ分母に揃える。片側だけ直すと貸借チェック
        # (denom=1 の split だけを合計する)が壊れる。
        for g, n, d in conn.execute(
                "SELECT guid, value_num, value_denom FROM splits WHERE tx_guid=?",
                (txguid,)).fetchall():
            if d == want["denom"]:
                continue
            cur.execute("UPDATE splits SET value_num=?, value_denom=?, quantity_num=?,"
                        " quantity_denom=? WHERE guid=?",
                        (n * want["denom"], want["denom"], n * want["denom"],
                         want["denom"], g))
        if want["currency"] == "USD" and usd_guid:
            cur.execute("UPDATE transactions SET currency_guid=? WHERE guid=?",
                        (usd_guid[0], txguid))
    # 削除した fitid は復活防止のため記録する(既存の作法)
    cur.execute("CREATE TABLE IF NOT EXISTS merged_ofx_fitids (ofx_fitid TEXT PRIMARY KEY,"
                " merged_into_tx_guid TEXT, merged_at TEXT, note TEXT)")
    for txg, fid, want in plan_dup:
        if fid:
            cur.execute("INSERT OR IGNORE INTO merged_ofx_fitids VALUES (?,?,?,?)",
                        (fid, None, datetime.now().isoformat(timespec="seconds"),
                         f"口座同定の遡及修正で除去(生CSVに裏付けなし) {want['date']} {want['desc']}"))
        cur.execute("DELETE FROM splits WHERE tx_guid=?", (txg,))
        cur.execute("DELETE FROM transactions WHERE guid=?", (txg,))

    # ---- (4) 境界の残高調整 ----
    adj_parent = account_guid(conn, ["Equity"], create=True)
    conn.execute("UPDATE accounts SET account_type='EQUITY' WHERE guid=?", (adj_parent,))
    adj_guid = account_guid(conn, ["Equity", "Opening Balance Adjustment"], create=True)
    conn.execute("UPDATE accounts SET account_type='EQUITY', ofx_type=NULL WHERE guid=?",
                 (adj_guid,))
    import uuid as _uuid
    print("\n=== (4) archive 境界の残高調整(archive 以前の誤差) ===")
    for key, t in sorted(truth.items()):
        tgt = target_guids.get(key)
        if tgt is None:
            continue
        db_open = conn.execute(
            "SELECT COALESCE(SUM(s.value_num*1.0/s.value_denom),0) FROM splits s"
            " JOIN transactions t ON t.guid=s.tx_guid WHERE s.account_guid=? AND t.post_date < ?",
            (tgt, t["start"])).fetchone()[0]
        delta = round(t["open"] - db_open, 2)
        if abs(delta) < 0.005:
            print(f"  {key:<32} 調整なし(銀行の始残 {t['open']:,.2f} と一致)")
            continue
        adj_denom = max(t["denom"], 100)          # 円でも 1/100 で持ち、丸め残りを出さない
        num = int(round(delta * adj_denom))
        txg = _uuid.uuid4().hex
        cur_guid = conn.execute("SELECT guid FROM currencies WHERE mnemonic=?",
                                (t["currency"],)).fetchone()[0]
        cur.execute("INSERT INTO transactions (guid, post_date, description, ofx_fitid,"
                    " currency_guid) VALUES (?,?,?,?,?)",
                    (txg, t["start"], f"開始残高調整(archive 以前の誤差) {key}",
                     f"ADJ:{key}:{t['start']}", cur_guid))
        for g, sign in ((tgt, 1), (adj_guid, -1)):
            cur.execute("INSERT INTO splits (guid, tx_guid, account_guid, value_num, value_denom,"
                        " quantity_num, quantity_denom) VALUES (?,?,?,?,?,?,?)",
                        (_uuid.uuid4().hex, txg, g, sign * num, adj_denom,
                         sign * num, adj_denom))
        print(f"  {key:<32} {db_open:>12,.2f} → 銀行の始残 {t['open']:>12,.2f}"
              f"  調整 {delta:>+12,.2f}")
    conn.commit()

    # ---- 受入条件: 最終残高が銀行の主張と一致するか ----
    print("\n=== 検証: 修正後の残高 vs 銀行の主張(生CSVの残高欄) ===")
    all_ok = True
    for key, t in sorted(truth.items()):
        tgt = target_guids.get(key)
        if tgt is None:
            continue
        got = conn.execute(
            "SELECT COALESCE(SUM(s.value_num*1.0/s.value_denom),0) FROM splits s"
            " JOIN transactions t ON t.guid=s.tx_guid WHERE s.account_guid=?"
            " AND t.post_date <= ?", (tgt, t["final_date"] if "final_date" in t else "9999")
        ).fetchone()[0]
        mark = "OK" if abs(got - t["final"]) < 0.005 else "ズレ"
        all_ok &= mark == "OK"
        print(f"  {key:<32} DB {got:>14,.2f}   銀行 {t['final']:>14,.2f}   {mark}")
    imbalance = conn.execute(
        "SELECT COUNT(*) FROM (SELECT tx_guid FROM splits GROUP BY tx_guid"
        " HAVING ABS(SUM(value_num*1.0/value_denom)) > 0.005)").fetchone()[0]
    print(f"\n  貸借が釣り合わない仕訳: {imbalance} 件"
          + ("  ← 要調査" if imbalance else "  (試算表 OK)"))
    if not all_ok:
        print("  ※ 残高が一致しない口座があります。バックアップから戻せます: "
              + backup.name)
    conn.close()


if __name__ == "__main__":
    main()
