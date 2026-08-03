"""契約書類・明細の raw store — ダウンロードした生ファイルの content-addressed 保管。

金融機関・インフラ事業者のマイページから落とした CSV / PDF(検針明細、契約内容、
重要事項説明書、約款など)を、**バイト列を一切改変せずに** 1 ファイルの SQLite へ
保管する。finance.db が「解釈済みの取引」を持つのに対し、こちらは「解釈前の原本」
を持つ — 後から解釈をやり直せることが目的。

設計は OSINT raw store(取得した公開データの保管)と同型:

- ``blobs``     = content-addressed の実体。同一内容は 1 回だけ保存(dedup)。
- ``documents`` = 取り込みログ。1 取り込み = 1 行。内容が既存と同一でも必ず追記する
  (「この日に再取得した」こと自体が記録)。

OSINT 側との違いは起点が HTTP fetch ではなく手動ダウンロードである点で、
``http_status`` / ``url`` の代わりに ``original_name`` / ``media_type`` を持つ。

## 規約

- **append-only**: UPDATE / DELETE は発行しない。訂正版が届いた場合も新しい行として
  並べる(どちらが新しいかは acquired_at で判る)。唯一の例外はスキーマ移行
  (ALTER TABLE と既定値の充填)で、これは記録内容の改変ではない。
## raw_imports.db との分担(2026-08-03 確定)

**簿記の計算に使うもの(finance.db への取込対象・取込候補)はここには入れない。**
それらの正本は ``db/raw_imports.db``(``py/importers/raw_archive.py``)で、
アーカイブ → 取込の 2 段階を ``imported_at`` の NULL/非 NULL で表現する
— importer 未対応の CSV も「DL 済み・未取込」として置ける。

この store が持つのは **それ以外の参照資料**: 契約書・約款・重要事項説明書・
検針明細・登記事項証明書など、読むために取っておくが仕訳には入らないもの。

``kind`` 列は分担が固まる前(2026-08-03)に入れた 2 件の ``bookkeeping`` が
残っているため保持しているが、**新規追加は常に ``reference``**。
書き込み側の CLI から ``--kind`` は外してある(読み取りの絞り込みには使える)。
- **fossil/git 管理外**: db/ は .gitignore 済み。氏名・住所・契約番号を含むため
  公開ツリーには決して入らない(コード側は汎用配管なので公開してよい)。
- 元ファイルは削除しない。この store は原本の代替ではなく、原本が散逸したときの
  復元元かつ「いつ何を取得したか」の索引。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path("db/documents.db")

# 用途区分。この store は参照資料専用なので新規は常に reference。bookkeeping は
# raw_imports.db へ分担が移る前(2026-08-03)に入れた 2 件のためだけに残っている。
KIND_BOOKKEEPING = "bookkeeping"
KIND_REFERENCE = "reference"
KINDS = (KIND_BOOKKEEPING, KIND_REFERENCE)

# 拡張子 -> media type。判らないものは application/octet-stream で受ける
# (バイト列さえ残れば後から判定できる — 取り込みを拒否しない方を優先)。
_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".xml": "application/xml",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS blobs (
    sha256      TEXT PRIMARY KEY,
    content     BLOB NOT NULL,
    bytes       INTEGER NOT NULL,
    first_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    acquired_at    TEXT NOT NULL,                        -- 取り込み日時(UTC ISO8601)
    source         TEXT NOT NULL,                        -- 'cde_mypage' 等の取得元識別子
    original_name  TEXT NOT NULL,                        -- ダウンロード時のファイル名
    media_type     TEXT NOT NULL,
    sha256         TEXT NOT NULL REFERENCES blobs(sha256),
    doc_date       TEXT,                                 -- 書類自体の日付/対象期間(任意)
    note           TEXT,
    kind           TEXT NOT NULL DEFAULT 'reference'
                   CHECK (kind IN ('bookkeeping', 'reference'))
);
"""

# インデックスは移行(ALTER TABLE)の後に張る — 後から足した列を参照するものが
# あるため、旧スキーマの DB では列が生えるまで CREATE INDEX が通らない。
_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source, acquired_at);
CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(kind, acquired_at);
"""

# 既存 DB へ後から足した列(追加順)。ALTER TABLE ADD COLUMN は既定値で既存行を
# 埋めるだけなので、append-only 規約(記録内容を書き換えない)には抵触しない。
_MIGRATIONS = (
    ("kind",
     "ALTER TABLE documents ADD COLUMN kind TEXT NOT NULL DEFAULT 'reference'"
     " CHECK (kind IN ('bookkeeping', 'reference'))"),
)


@dataclass(frozen=True)
class Document:
    id: int
    acquired_at: str
    source: str
    original_name: str
    media_type: str
    sha256: str
    bytes: int
    doc_date: str | None
    note: str | None
    kind: str


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    with conn:
        for column, ddl in _MIGRATIONS:
            if column not in existing:
                conn.execute(ddl)


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_TABLES)
    _migrate(conn)
    conn.executescript(_SCHEMA_INDEXES)
    return conn


def guess_media_type(name: str) -> str:
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_bytes(
    conn: sqlite3.Connection,
    content: bytes,
    *,
    source: str,
    original_name: str,
    kind: str = KIND_REFERENCE,
    media_type: str | None = None,
    doc_date: str | None = None,
    note: str | None = None,
    acquired_at: str | None = None,
) -> tuple[int, str, bool]:
    """バイト列を 1 件取り込む。

    Returns:
        (documents.id, sha256, blob が新規だったか)
    """
    if kind not in KINDS:
        raise ValueError(f"kind は {KINDS} のいずれか: {kind!r}")
    sha = hashlib.sha256(content).hexdigest()
    now = acquired_at or _now_iso()
    with conn:
        existing = conn.execute(
            "SELECT 1 FROM blobs WHERE sha256 = ?", (sha,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO blobs(sha256, content, bytes, first_seen) VALUES (?, ?, ?, ?)",
                (sha, content, len(content), now),
            )
        cur = conn.execute(
            "INSERT INTO documents(acquired_at, source, original_name, media_type,"
            " sha256, doc_date, note, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, source, original_name,
             media_type or guess_media_type(original_name), sha, doc_date, note, kind),
        )
    return int(cur.lastrowid), sha, existing is None


def add_file(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    source: str,
    kind: str = KIND_REFERENCE,
    doc_date: str | None = None,
    note: str | None = None,
    original_name: str | None = None,
) -> tuple[int, str, bool]:
    """ファイルを 1 件取り込む(バイト列は改変しない)。"""
    path = Path(path)
    return add_bytes(
        conn,
        path.read_bytes(),
        source=source,
        kind=kind,
        original_name=original_name or path.name,
        doc_date=doc_date,
        note=note,
    )


def list_documents(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    kind: str | None = None,
    limit: int | None = None,
) -> list[Document]:
    sql = (
        "SELECT d.id, d.acquired_at, d.source, d.original_name, d.media_type,"
        " d.sha256, b.bytes, d.doc_date, d.note, d.kind"
        " FROM documents d JOIN blobs b ON b.sha256 = d.sha256"
    )
    params: list[object] = []
    where = []
    if source is not None:
        where.append("d.source = ?")
        params.append(source)
    if kind is not None:
        where.append("d.kind = ?")
        params.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.acquired_at DESC, d.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [Document(*row) for row in conn.execute(sql, params)]


def get_content(conn: sqlite3.Connection, ref: str | int) -> bytes:
    """documents.id または sha256(前方一致可)で実体を取り出す。"""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute(
            "SELECT b.content FROM documents d JOIN blobs b ON b.sha256 = d.sha256"
            " WHERE d.id = ?",
            (int(ref),),
        ).fetchone()
    else:
        rows = conn.execute(
            "SELECT content FROM blobs WHERE sha256 LIKE ?", (f"{ref}%",)
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"sha256 の前方一致が複数あります: {ref}")
        row = rows[0] if rows else None
    if row is None:
        raise KeyError(f"該当する書類がありません: {ref}")
    return row[0]


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    docs, blobs, total = conn.execute(
        "SELECT (SELECT COUNT(*) FROM documents),"
        " (SELECT COUNT(*) FROM blobs),"
        " (SELECT COALESCE(SUM(bytes), 0) FROM blobs)"
    ).fetchone()
    result = {"documents": docs, "blobs": blobs, "bytes": total}
    for kind in KINDS:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE kind = ?", (kind,)
        ).fetchone()
        result[kind] = n
    return result
