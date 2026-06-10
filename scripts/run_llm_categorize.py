import sys
from pathlib import Path
import os

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from py.ai.llm_categorize import categorize_with_llm


def main():
    print("==================================================")
    print("  LLMベースのカテゴリ分類")
    print("==================================================")

    os.chdir(project_root)
    categorize_with_llm()

    print("\n処理が完了しました。")
    print("==================================================")


if __name__ == "__main__":
    main()
