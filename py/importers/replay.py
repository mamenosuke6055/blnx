"""raw_imports.db のアーカイブを現行 importer で再適用するリプレイ。

用途: importer のバグ修正後（例: 楽天証券 peer 修正、楽天カードの正規化キー
強化）に、CSV を再ダウンロードせずアーカイブから finance.db を再生成する。
取込は `ofx_fitid` で冪等なので、未修正分を含めて全アーカイブを流し直しても
重複は増えない。設計は wiki [Dev_RawImports_Archive] §1（リプレイ）準拠。

テスト容易性のため本体ロジックは py/ 配下に置き、scripts/run_replay.py は
薄い CLI ラッパーに留める（inbox_pipeline と同方針）。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from py.importers import inbox_pipeline
from py.importers import raw_archive


def backup_finance_db(db_path: Path) -> Path:
    """リプレイ前に finance.db をタイムスタンプ付きでバックアップする。"""
    db_path = Path(db_path)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db_path.with_name(f"{db_path.name}.bak.before_replay_{ts}")
    shutil.copy2(db_path, bak)
    return bak


def replay_archives(
    raw_conn: sqlite3.Connection,
    db_path: Path,
    source: str | None = None,
    dry_run: bool = False,
    backup: bool = True,
) -> list[dict]:
    """アーカイブ済み生CSVを現行 importer で finance.db に再適用する。

    Args:
        raw_conn: raw_imports.db への接続。
        db_path: 再適用先 finance.db。
        source: 指定するとその source のアーカイブのみリプレイ。
        dry_run: True なら取込せず対象一覧だけ返す（new_tx=None）。
        backup: dry_run でなく対象が1件以上あるとき、最初に finance.db をバックアップ。

    Returns:
        各 record: {id, source, filename, new_tx(int|None), error(str|None)}。
    """
    archived = raw_archive.list_archived(raw_conn, source=source)

    if dry_run:
        return [
            {"id": m["id"], "source": m["source"], "filename": m["filename"],
             "new_tx": None, "error": None}
            for m in archived
        ]

    if backup and archived:
        backup_finance_db(Path(db_path))

    records: list[dict] = []
    for meta in archived:
        rec = {"id": meta["id"], "source": meta["source"],
               "filename": meta["filename"], "new_tx": None, "error": None}
        src = meta["source"]
        if src is None:
            rec["error"] = "source 不明（リプレイ不可）"
            records.append(rec)
            continue
        content = raw_archive.fetch_content(raw_conn, meta["id"])
        if content is None:
            rec["error"] = "content 欠落"
            records.append(rec)
            continue
        try:
            # 元バイト列を元ファイル名で一時ファイルに復元（encoding は importer が自動判定）
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / meta["filename"]
                tmp.write_bytes(content)
                before = inbox_pipeline._count_transactions(db_path)
                inbox_pipeline._import_one(tmp, src, db_path)
                after = inbox_pipeline._count_transactions(db_path)
            rec["new_tx"] = after - before
        except Exception as e:  # importer 単体の失敗で全体を止めない
            rec["error"] = f"取込失敗: {e}"
        records.append(rec)
    return records
