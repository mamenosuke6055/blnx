import sys
from pathlib import Path
import os

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from py.analysis.detect_subscriptions import detect_subscriptions


def main():
    print("==================================================")
    print("  固定費・サブスクリプション検出")
    print("==================================================")

    os.chdir(project_root)
    detect_subscriptions()

    print("\n処理が完了しました。")
    print("==================================================")


if __name__ == "__main__":
    main()
