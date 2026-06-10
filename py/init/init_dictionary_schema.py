
import sqlite3
from pathlib import Path

DB_PATH = Path("db/dictionary.db")

DICTIONARY_SCHEMA = """
PRAGMA foreign_keys = ON;

-- ==========================================
-- 手動カテゴリ辞書
-- ==========================================
CREATE TABLE IF NOT EXISTS category_dict_manual (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    main_category TEXT NOT NULL,
    sub_category TEXT NOT NULL,
    UNIQUE(main_category, sub_category)
);

-- ==========================================
-- description → category_id の対応辞書
-- ==========================================
CREATE TABLE IF NOT EXISTS category_dict (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL UNIQUE,
    category_id INTEGER NOT NULL,
    FOREIGN KEY (category_id) REFERENCES category_dict_manual(id)
);
"""


def create_dictionary_tables(conn):
    """dictionary.db 用の全テーブルを `conn` に作成する。
    `conn` は本番DBでも :memory: でも構わない。commit は呼び出し側の責務。
    """
    conn.executescript(DICTIONARY_SCHEMA)


def init_dictionary_db():
    DB_PATH.parent.mkdir(exist_ok=True, parents=True)
    conn = sqlite3.connect(DB_PATH)
    create_dictionary_tables(conn)
    conn.commit()
    conn.close()
    print(f"✅ dictionary.db を初期化しました → {DB_PATH}")

if __name__ == "__main__":
    init_dictionary_db()
