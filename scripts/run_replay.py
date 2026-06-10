"""アーカイブからのリプレイ CLI（薄いラッパー）。本体は py/importers/replay.py。

raw_imports.db の生CSVを現行 importer で finance.db に再適用する。importer 修正後に
再ダウンロードせず帳簿を再生成する用途。冪等（ofx_fitid）前提なので流し直しても
重複は増えない。

使い方:
  uv run python scripts/run_replay.py --dry-run            # 対象アーカイブ一覧（取込なし）
  uv run python scripts/run_replay.py                      # 全アーカイブをリプレイ（前にバックアップ）
  uv run python scripts/run_replay.py --source rakuten_card
  uv run python scripts/run_replay.py --no-backup
"""
import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.importers import raw_archive, replay


def get_db_path() -> Path:
    config_path = project_root / "config/settings.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return project_root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        return project_root / "db/finance.db"


def main():
    ap = argparse.ArgumentParser(description="raw_imports.db のアーカイブを現行 importer で再適用する")
    ap.add_argument("--source", default=None, help="この source のアーカイブのみリプレイ")
    ap.add_argument("--dry-run", action="store_true", help="取込せず対象一覧のみ表示")
    ap.add_argument("--no-backup", action="store_true", help="リプレイ前の finance.db バックアップを省略")
    args = ap.parse_args()

    db_path = get_db_path()
    print(f"リプレイ先データベース: {db_path}")

    raw_conn = raw_archive.open_raw_db()
    try:
        records = replay.replay_archives(
            raw_conn, db_path, source=args.source,
            dry_run=args.dry_run, backup=not args.no_backup,
        )
    finally:
        raw_conn.close()

    if not records:
        print("リプレイ対象のアーカイブがありません。"
              "（raw_imports.db が空の場合は先に scripts/run_import.py で取り込んでください）")
        return

    if args.dry_run:
        print(f"\n=== リプレイ対象 {len(records)} 件（ドライラン・取込なし）===")
        for r in records:
            print(f"  [{r['id']:>4}] {r['source'] or '不明':<14} {r['filename']}")
        return

    ok = [r for r in records if r["error"] is None]
    ng = [r for r in records if r["error"] is not None]
    total_new = sum(r["new_tx"] for r in ok)
    print(f"\n=== リプレイ完了: {len(ok)} 件 / 新規 {total_new} 件"
          + (f" / 失敗 {len(ng)} 件" if ng else "") + " ===")
    for r in ok:
        print(f"  [{r['id']:>4}] {r['source']:<14} 新規 {r['new_tx']:>4} 件  {r['filename']}")
    for r in ng:
        print(f"  [{r['id']:>4}] {r['source'] or '不明':<14} 失敗: {r['error']}  {r['filename']}")


if __name__ == "__main__":
    main()
