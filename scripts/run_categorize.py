import argparse
import sys
from pathlib import Path
import os

# Add the project root to the Python path
# This allows us to import modules from the 'py' directory
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.processing.link_categories_by_dictionary import categorize_and_update_transactions
from py.processing.sync_manual_to_dictionary import run_sync

FINANCE_DB = project_root / "db" / "finance.db"
DICTIONARY_DB = project_root / "db" / "dictionary.db"


def main():
    """
    Entry point for the categorization script.

    既定で「手動分類 → 辞書」sync を分類の*前段*に走らせる。これにより前回までに
    手動確定した分類が辞書へ還元され、今回の自動分類にそのまま反映される
    （「使うほど自動分類率が上がる」ループの自動化）。sync は冪等なので
    毎回呼んでも安全。--no-sync で従来挙動（分類のみ）に戻せる。
    """
    parser = argparse.ArgumentParser(description="辞書ベースの取引自動分類（前段で手動分類を辞書へ sync）")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="前段の手動分類→辞書 sync をスキップし、分類のみ実行する",
    )
    args = parser.parse_args()

    print("==================================================")
    print("  Running Transaction Categorization Script")
    print("==================================================")

    # Set the current working directory to the project root
    # This ensures that relative paths in the script work correctly
    os.chdir(project_root)

    if not args.no_sync:
        print("\n[1/2] 手動分類 → 辞書 sync（前段）")
        run_sync(FINANCE_DB, DICTIONARY_DB, verbose=True)
        print("\n[2/2] 辞書ベース自動分類")

    categorize_and_update_transactions()

    print("\nCategorization process finished.")
    print("Please check the log output above for details.")
    print("==================================================")


if __name__ == "__main__":
    main()
