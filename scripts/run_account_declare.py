"""口座宣言 CLI — CSV に口座が書かれていないファイルについて、人が口座を宣言する。

宣言は control artifact として raw_imports.db に append-only で積む(fossil の tag)。
誤った宣言は --cancel で取消を追記して直す(既存行は書き換えない)。

  blnx declare-account --list                       登録済み口座と宣言の一覧
  blnx declare-account --pending                    宣言が無く判定もできないファイル
  blnx declare-account --file <名前> --account <キー>  宣言する
  blnx declare-account --file <名前> --account <キー> --cancel  取消
"""
import argparse
import sqlite3
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.importers import account_identity as ai, raw_archive


def _resolve(conn, name: str) -> tuple[str, str]:
    rows = conn.execute(
        "SELECT sha256, filename FROM raw_imports WHERE filename=? OR sha256 LIKE ?",
        (name, name + "%")).fetchall()
    if not rows:
        raise SystemExit(f"アーカイブに見つかりません: {name}")
    if len(rows) > 1:
        raise SystemExit(f"複数一致します: {[r[1] for r in rows]}")
    return rows[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="口座と宣言の一覧")
    ap.add_argument("--pending", action="store_true", help="判定できないファイルの一覧")
    ap.add_argument("--file", help="対象ファイル名(または sha256 の先頭)")
    ap.add_argument("--account", help="口座キー(--list で確認)")
    ap.add_argument("--cancel", action="store_true", help="宣言を取消す(追記)")
    ap.add_argument("--note", default=None, help="根拠のメモ")
    a = ap.parse_args()

    conn = raw_archive.open_raw_db()
    ai.init_identity_tables(conn)
    reg = ai.registry(conn)

    if a.list or not (a.file or a.pending):
        print("=== 登録済み口座 ===")
        for k, v in reg.items():
            print(f"  {k:<32} {v['label']:<20} {v['currency']}  {' > '.join(v['account_path'])}")
        eff = ai.effective_declarations(conn)
        print(f"\n=== 有効な宣言 {len(eff)} 件 ===")
        for sha, key in eff.items():
            fn = conn.execute("SELECT filename FROM raw_imports WHERE sha256=?",
                              (sha,)).fetchone()
            origin = conn.execute(
                "SELECT origin FROM account_declarations WHERE raw_sha256=? AND tag_type=1"
                " ORDER BY id DESC LIMIT 1", (sha,)).fetchone()
            print(f"  {sha[:12]}  {(fn[0] if fn else '?'):<36} {key:<32} {origin[0]}")
        return

    if a.pending:
        n = 0
        for sha, fn, blob in conn.execute(
                "SELECT sha256, filename, content FROM raw_imports WHERE source='dneobank'"
                " ORDER BY id"):
            if sha in ai.effective_declarations(conn):
                continue
            parsed = ai.parse_dneobank(ai._decode(bytes(blob)))
            if parsed is None:
                continue
            ident = ai.identify(conn, parsed, exclude_sha=sha)
            if not ident.decided:
                n += 1
                print(f"  {fn:<36} {ident.reason}  候補: {'・'.join(ident.candidates) or '-'}")
        print(f"判定できないファイル: {n} 件" if n else "判定できないファイルはありません。")
        return

    if not a.account:
        raise SystemExit("--account が必要です(--list で口座キーを確認)")
    sha, fn = _resolve(conn, a.file)
    if a.cancel:
        ai.cancel(conn, sha, a.account, evidence=a.note or "手動取消")
        print(f"取消を追記しました: {fn} → {a.account}")
    else:
        ai.declare(conn, sha, a.account, origin="human", evidence=a.note or "人の宣言")
        print(f"宣言しました: {fn} → {reg[a.account]['label']} ({a.account})")
    conn.close()


if __name__ == "__main__":
    main()
