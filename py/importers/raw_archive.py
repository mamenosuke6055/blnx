"""raw_imports.db: ダウンロード済みCSVの生アーカイブ。

設計は fossil wiki [Dev_RawImports_Archive] を正本とする。役割:
  - 同一ファイルの再ダウンロードを sha256 で冪等にスキップする
  - importer 修正後に再DLせずアーカイブからリプレイできるよう生バイト列を保持する
  - 取込の来歴/監査を永続記録する

finance.db とは別DB(db/raw_imports.db)に置き、本体の複式簿記DBを生BLOBで
肥大化させない。文字コードは無加工(cp932等そのまま)で BLOB 保存する。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_imports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT,           -- detect_source の判定キー (rakuten_bank 等)
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL UNIQUE,  -- 同一ファイルの重複DLを弾く鍵
    period_start  TEXT,           -- CSVがカバーする取引日範囲 (取込時に充填)
    period_end    TEXT,
    downloaded_at TEXT,
    imported_at   TEXT,           -- importer 適用日時 (取込時に充填)
    row_count     INTEGER,        -- CSV行数(概算: 改行数)
    new_rows      INTEGER,        -- finance.db に新規取込された件数 (取込時に充填)
    content       BLOB NOT NULL,  -- 生バイト列(元エンコーディングのまま)
    archived_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_raw_imports_source ON raw_imports(source);
"""


@dataclass
class ArchiveResult:
    sha256: str
    is_duplicate: bool   # True なら既存と同一内容でスキップ
    row_id: int
    filename: str
    source: str | None


def get_raw_db_path() -> Path:
    """config/settings.json の raw_imports_db_path、無ければ db/raw_imports.db。"""
    config_path = PROJECT_ROOT / "config/settings.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return PROJECT_ROOT / settings.get("raw_imports_db_path", "db/raw_imports.db")
    except FileNotFoundError:
        return PROJECT_ROOT / "db/raw_imports.db"


def init_raw_imports_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def open_raw_db(db_path: Path | None = None) -> sqlite3.Connection:
    """スキーマを保証した raw_imports.db 接続を返す。"""
    conn = sqlite3.connect(db_path or get_raw_db_path())
    init_raw_imports_db(conn)
    return conn


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_bytes(
    conn: sqlite3.Connection,
    content: bytes,
    filename: str,
    source: str | None = None,
    downloaded_at: str | None = None,
    row_count: int | None = None,
) -> ArchiveResult:
    """生バイト列をアーカイブする。同一 sha256 が既にあればスキップ(冪等)。"""
    digest = _sha256(content)
    cur = conn.cursor()
    cur.execute("SELECT id, source FROM raw_imports WHERE sha256 = ?", (digest,))
    existing = cur.fetchone()
    if existing:
        return ArchiveResult(digest, True, existing[0], filename, existing[1])

    cur.execute(
        """
        INSERT INTO raw_imports (source, filename, sha256, downloaded_at, row_count, content)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source, filename, digest, downloaded_at, row_count, content),
    )
    conn.commit()
    return ArchiveResult(digest, False, cur.lastrowid, filename, source)


def archive_file(
    conn: sqlite3.Connection,
    path: Path,
    source: str | None = None,
    downloaded_at: str | None = None,
) -> ArchiveResult:
    """ファイルを読み込みアーカイブする。downloaded_at 省略時はファイル更新時刻。"""
    path = Path(path)
    content = path.read_bytes()
    row_count = content.count(b"\n")
    if downloaded_at is None:
        downloaded_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    return archive_bytes(
        conn, content, path.name, source=source, downloaded_at=downloaded_at, row_count=row_count
    )


def fetch_content(conn: sqlite3.Connection, row_id: int) -> bytes | None:
    """アーカイブ済み生バイト列を取り出す(リプレイ用)。"""
    cur = conn.cursor()
    cur.execute("SELECT content FROM raw_imports WHERE id = ?", (row_id,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def list_archived(conn: sqlite3.Connection, source: str | None = None) -> list[dict]:
    """アーカイブ一覧(content を除くメタ)を返す。source 指定で絞り込み。"""
    cur = conn.cursor()
    cols = "id, source, filename, sha256, period_start, period_end, downloaded_at, imported_at, row_count, new_rows, archived_at"
    if source is None:
        cur.execute(f"SELECT {cols} FROM raw_imports ORDER BY id")
    else:
        cur.execute(f"SELECT {cols} FROM raw_imports WHERE source = ? ORDER BY id", (source,))
    keys = [c.strip() for c in cols.split(",")]
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def update_import_meta(
    conn: sqlite3.Connection,
    row_id: int,
    imported_at: str | None = None,
    new_rows: int | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> None:
    """取込パイプライン(inbox CLI)が、適用結果のメタを後から充填する。"""
    if imported_at is None:
        imported_at = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE raw_imports
        SET imported_at = ?, new_rows = ?, period_start = ?, period_end = ?
        WHERE id = ?
        """,
        (imported_at, new_rows, period_start, period_end, row_id),
    )
    conn.commit()
