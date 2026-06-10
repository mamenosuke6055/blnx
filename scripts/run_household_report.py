import sys
from pathlib import Path
import os
import argparse
from datetime import datetime

# Add the project root to the Python path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from py.analysis.generate_household_report import generate_household_report

def main():
    parser = argparse.ArgumentParser(description="月次家計分析レポートを生成します。")
    parser.add_argument(
        '-y', '--year',
        type=int,
        help="対象年 (例: 2025)。省略時は前月の年。"
    )
    parser.add_argument(
        '-m', '--month',
        type=int,
        help="対象月 (1-12)。省略時は前月。"
    )
    parser.add_argument(
        '-o', '--output-dir',
        type=str,
        default=None,
        help="出力先ディレクトリ。省略時は ~/Documents/finance_reports/"
    )
    args = parser.parse_args()

    year, month = args.year, args.month

    if year and not month:
        parser.error("年を指定する場合は月も指定してください。")
    if month and not year:
        year = datetime.now().year

    print("==================================================")
    print("  月次家計分析レポート生成")
    print("==================================================")

    os.chdir(project_root)

    output_path = generate_household_report(
        year=year, month=month, output_dir=args.output_dir
    )

    if output_path:
        print(f"\nレポートを生成しました: {output_path}")
    else:
        print("\nレポート生成に失敗しました。")
    print("==================================================")

if __name__ == "__main__":
    main()
