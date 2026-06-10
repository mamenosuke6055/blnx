"""手動分類を辞書に還元する実行スクリプト（フィードバックループ）。

    uv run python scripts/sync_manual_to_dictionary.py

手動分類（finance.db）→ 辞書（dictionary.db）。実行後は
``run_categorize.py`` が来月以降の新規取引を自動分類できるようになる。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from py.processing.sync_manual_to_dictionary import run_sync  # noqa: E402

FINANCE_DB = PROJECT_ROOT / "db" / "finance.db"
DICTIONARY_DB = PROJECT_ROOT / "db" / "dictionary.db"


def main():
    print("=" * 50)
    print("  手動分類 → 辞書 フィードバックループ")
    print("=" * 50)

    stats = run_sync(FINANCE_DB, DICTIONARY_DB)
    if stats is None:
        print(f"DB が見つかりません (finance={FINANCE_DB.exists()}, dict={DICTIONARY_DB.exists()})")
        return

    print(f"新規辞書ルール : {stats['new']}")
    print(f"更新辞書ルール : {stats['updated']}")
    print(f"変更なし       : {stats['unchanged']}")
    print(f"カテゴリ種類数 : {stats['categories']}")
    print("=" * 50)
    if stats["new"] or stats["updated"]:
        print("→ 次回 run_categorize.py で新規取引が自動分類されます。")


if __name__ == "__main__":
    main()
