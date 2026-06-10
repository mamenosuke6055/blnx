"""手動分類 → 辞書 のフィードバックループ。

ユーザーが手動で確定した分類（finance.db: ``transactions.manual_category_guid``）を
辞書（dictionary.db: ``category_dict`` / ``category_dict_manual``）に還元する。

これにより、来月以降に同じ摘要の新規取引が来ると
``link_categories_by_dictionary.py`` で自動分類され、
``migrate_dictionary_to_training_data.py`` 経由で ML 教師データにも回る。
「辞書 → 自動分類」「辞書 → ML 教師データ」の先の経路は既存であり、
本モジュールが唯一欠けていた「手動分類 → 辞書」の経路を埋めることで
ループが閉じる（使うほど自動分類率が上がる構造）。

設計判断:
- 同一 description が複数カテゴリに手動分類されている場合は **最新 post_date** を採用
  （最後に確定したものが正解という前提）。
- Transfer 等 EXPENSE/INCOME 以外のカテゴリは除外。辞書ベース分類
  (``link_categories_by_dictionary``) が Income/Expense しか扱わないため、
  辞書に入れても利用されず無駄に膨らむだけになる。
- 接続は引数で受け取る（テスタブル）。commit は本モジュールが行う。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def extract_manual_categories(finance_conn: sqlite3.Connection) -> list[dict]:
    """finance.db の手動分類済み取引から ``(description, main, sub)`` を抽出する。

    ``manual_category_guid`` は sub カテゴリの ``accounts.guid`` を指す。その親が
    main カテゴリ。同一 description が複数カテゴリに割り当てられている場合は
    最新 post_date（同日は enter_date）のものだけを残す。
    """
    query = """
    SELECT t.description   AS description,
           main.name       AS main_category,
           sub.name        AS sub_category
    FROM transactions t
    JOIN accounts sub  ON t.manual_category_guid = sub.guid
    JOIN accounts main ON sub.parent_guid = main.guid
    WHERE t.manual_category_guid IS NOT NULL
      AND t.description IS NOT NULL
      AND TRIM(t.description) != ''
      AND sub.account_type IN ('EXPENSE', 'INCOME')
    ORDER BY t.post_date ASC, t.enter_date ASC
    """
    rows = finance_conn.execute(query).fetchall()

    # ORDER BY 昇順なので、同一 description は後勝ち = 最新が残る。
    latest: dict[str, dict] = {}
    for description, main_category, sub_category in rows:
        latest[description] = {
            "description": description,
            "main_category": main_category,
            "sub_category": sub_category,
        }
    return list(latest.values())


def sync_manual_categories_to_dictionary(
    finance_conn: sqlite3.Connection,
    dict_conn: sqlite3.Connection,
) -> dict:
    """手動分類を辞書に還元する。冪等。

    Returns:
        ``{"new": int, "updated": int, "unchanged": int, "categories": int}``
        - new: 新規に追加した description ルール数
        - updated: 既存 description のカテゴリを付け替えた数
        - unchanged: 既に同じ内容で存在し変更不要だった数
        - categories: 関与した (main, sub) カテゴリの種類数
    """
    entries = extract_manual_categories(finance_conn)

    cur = dict_conn.cursor()
    stats = {"new": 0, "updated": 0, "unchanged": 0}
    category_ids: set[int] = set()

    for entry in entries:
        main_category = entry["main_category"]
        sub_category = entry["sub_category"]
        description = entry["description"]

        # 1. (main, sub) を category_dict_manual に確保し id を得る（UNIQUE 制約で重複しない）
        cur.execute(
            "INSERT OR IGNORE INTO category_dict_manual (main_category, sub_category) "
            "VALUES (?, ?)",
            (main_category, sub_category),
        )
        cur.execute(
            "SELECT id FROM category_dict_manual "
            "WHERE main_category = ? AND sub_category = ?",
            (main_category, sub_category),
        )
        category_id = cur.fetchone()[0]
        category_ids.add(category_id)

        # 2. description -> category_id を upsert（new / updated / unchanged を判定）
        cur.execute(
            "SELECT category_id FROM category_dict WHERE description = ?",
            (description,),
        )
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                "INSERT INTO category_dict (description, category_id) VALUES (?, ?)",
                (description, category_id),
            )
            stats["new"] += 1
        elif existing[0] != category_id:
            cur.execute(
                "UPDATE category_dict SET category_id = ? WHERE description = ?",
                (category_id, description),
            )
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1

    dict_conn.commit()
    stats["categories"] = len(category_ids)
    return stats


def run_sync(
    finance_db_path: str | Path,
    dictionary_db_path: str | Path,
    verbose: bool = False,
) -> dict | None:
    """DB パスを受けて sync を実行する薄いオーケストレーション層。

    接続の open/close を内包し、コア関数 ``sync_manual_categories_to_dictionary``
    （conn 受け取り・テスタブル）を呼ぶ。スクリプト実行 (``scripts/sync_manual_to_dictionary.py``)
    と run_categorize 前段フックの両方から再利用するため、ここに集約する。

    どちらかの DB が存在しなければ何もせず ``None`` を返す（取込前の初回実行などで
    パイプラインを止めないため）。Returns: stats dict、または ``None``（DB 未整備時）。
    """
    finance_db_path = Path(finance_db_path)
    dictionary_db_path = Path(dictionary_db_path)
    if not finance_db_path.exists() or not dictionary_db_path.exists():
        if verbose:
            print(f"[sync] DB 未整備のためスキップ "
                  f"(finance={finance_db_path.exists()}, dict={dictionary_db_path.exists()})")
        return None

    finance_conn = sqlite3.connect(finance_db_path)
    dict_conn = sqlite3.connect(dictionary_db_path)
    try:
        stats = sync_manual_categories_to_dictionary(finance_conn, dict_conn)
    finally:
        finance_conn.close()
        dict_conn.close()

    if verbose:
        print(f"[sync] 手動分類→辞書: new={stats['new']} updated={stats['updated']} "
              f"unchanged={stats['unchanged']} categories={stats['categories']}")
    return stats
