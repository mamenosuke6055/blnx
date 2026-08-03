"""契約書類・明細の生ファイルを documents.db へ保管する CLI。

マイページから落とした CSV / PDF(検針明細・契約内容・重要事項説明書・約款など)を
バイト列のまま content-addressed で保管する。設計は py/documents/store.py を参照。

ここに入れるのは**参照資料**(契約書・約款・重要事項説明書・検針明細・登記事項
証明書など、読むために取っておくが仕訳には入らないもの)。**簿記の計算に使う
CSV は db/raw_imports.db が正本**なので、そちらへアーカイブする(importer 未対応の
ものも「DL 済み・未取込」として置ける)。

    blnx docs add data/PlatformDownload.csv \\
        --source cde_mypage --doc-date 2023-08/2026-06 --note "検針明細 35ヶ月分"
    blnx docs list
    blnx docs list --source nuro_mypage --json
    blnx docs export 3 /tmp/restored.csv
"""

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.documents import store  # noqa: E402


def _print_stats(conn) -> None:
    s = store.stats(conn)
    legacy = s[store.KIND_BOOKKEEPING]
    note = f"(うち簿記 {legacy} = raw_imports.db へ移管済みの旧分)" if legacy else ""
    print(f"\n合計 {s['documents']} 件{note}"
          f" / 実体 {s['blobs']} 個 / {s['bytes']:,} bytes")


def _cmd_add(conn, args) -> int:
    added = 0
    for path in args.paths:
        if not path.exists():
            print(f"× 見つかりません: {path}", file=sys.stderr)
            continue
        doc_id, sha, is_new = store.add_file(
            conn, path, source=args.source, doc_date=args.doc_date, note=args.note,
        )
        state = "新規" if is_new else "既存と同一内容"
        print(f"✓ #{doc_id} {path.name}  {sha[:12]}  "
              f"({state}, {path.stat().st_size:,} bytes)")
        added += 1
    print(f"\n{added} 件取込。", end="")
    _print_stats(conn)
    return 0


def _cmd_list(conn, args) -> int:
    docs = store.list_documents(conn, source=args.source, kind=args.kind, limit=args.limit)
    if args.json:
        print(json.dumps([d.__dict__ for d in docs], ensure_ascii=False, indent=2))
        return 0
    if not docs:
        print("(該当する書類はありません)")
        return 0
    print(f"{'id':>4}  {'取込日時':<20} {'区分':<11} {'source':<14} {'sha':<12} "
          f"{'bytes':>9}  ファイル名")
    print("-" * 108)
    for d in docs:
        print(f"{d.id:>4}  {d.acquired_at:<20} {d.kind:<11} {d.source:<14} "
              f"{d.sha256[:12]:<12} {d.bytes:>9,}  {d.original_name}")
        if d.doc_date or d.note:
            detail = "  ".join(x for x in (d.doc_date, d.note) if x)
            print(f"      └ {detail}")
    _print_stats(conn)
    return 0


def _cmd_export(conn, args) -> int:
    content = store.get_content(conn, args.ref)
    args.dest.parent.mkdir(parents=True, exist_ok=True)
    args.dest.write_bytes(content)
    print(f"✓ {args.dest} へ書き出しました({len(content):,} bytes)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=store.DEFAULT_DB_PATH)
    sub = ap.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="ファイルを取り込む")
    p_add.add_argument("paths", type=Path, nargs="+")
    p_add.add_argument("--source", required=True,
                       help="取得元識別子(例: cde_mypage / nuro_mypage / sbi_sec)")
    p_add.add_argument("--doc-date", default=None, help="書類自体の日付・対象期間")
    p_add.add_argument("--note", default=None)
    p_add.set_defaults(func=_cmd_add)

    p_list = sub.add_parser("list", help="保管済みの一覧")
    p_list.add_argument("--source", default=None)
    p_list.add_argument("--kind", default=None, choices=store.KINDS)
    p_list.add_argument("--limit", type=int, default=None)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_exp = sub.add_parser("export", help="実体をファイルへ書き出す")
    p_exp.add_argument("ref", help="documents.id または sha256(前方一致可)")
    p_exp.add_argument("dest", type=Path)
    p_exp.set_defaults(func=_cmd_export)

    args = ap.parse_args()
    conn = store.connect(args.db)
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
