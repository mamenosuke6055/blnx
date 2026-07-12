import sys
from pathlib import Path
import os
import argparse
from datetime import datetime

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.analysis.generate_cashflow_statement import generate_cashflow_statement


def main():
    parser = argparse.ArgumentParser(description="キャッシュフロー計算書を生成します。")
    parser.add_argument('-y', '--year', type=int, help="年")
    parser.add_argument('-m', '--month', type=int, help="月")
    args = parser.parse_args()

    if args.year and not args.month:
        parser.error("年を指定する場合は月も指定してください。")
    if args.month and not args.year:
        args.year = datetime.now().year

    print("==================================================")
    print("  キャッシュフロー計算書の生成")
    print("==================================================")

    os.chdir(project_root)
    generate_cashflow_statement(year=args.year, month=args.month)

    print("\n処理が完了しました。")
    print("==================================================")


if __name__ == "__main__":
    main()
