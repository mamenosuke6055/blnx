import sys
from pathlib import Path
import os
import argparse

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.analysis.rebalance import suggest_rebalance


def main():
    parser = argparse.ArgumentParser(description="ポートフォリオ・リバランス提案を生成します。")
    parser.add_argument(
        '-a', '--additional',
        type=int,
        default=0,
        help="追加投資額 (円)。省略時は0。"
    )
    args = parser.parse_args()

    print("==================================================")
    print("  ポートフォリオ・リバランス提案")
    print("==================================================")

    os.chdir(project_root)
    suggest_rebalance(additional_investment=args.additional)

    print("\n処理が完了しました。")
    print("==================================================")


if __name__ == "__main__":
    main()
