"""
貸借対照表 (Balance Sheet) スナップショット実行スクリプト

使用例:
  uv run python scripts/run_balance_sheet.py                # 今日時点
  uv run python scripts/run_balance_sheet.py -d 2026-03-31  # 指定日時点
"""
import argparse
import sys
from datetime import date
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.analysis.generate_balance_sheet import generate_balance_sheet


def fmt(n: int) -> str:
    return f"{n:>14,}円"


def _print_section(title: str, items: list[dict], total: int):
    print(f"\n【{title}】")
    if not items:
        print("  (該当なし)")
    for r in items:
        anomaly = "  ⚠ 負値" if r['balance'] < 0 else ""
        print(f"  {r['name'][:32]:<32s} {fmt(r['balance'])}{anomaly}")
    print(f"  {'合計':<32s} {fmt(total)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--date', type=str, help="基準日 (YYYY-MM-DD)。省略時は今日。")
    args = parser.parse_args()

    at = date.fromisoformat(args.date) if args.date else None
    bs = generate_balance_sheet(at=at)

    print(f"=== 貸借対照表 (Balance Sheet) ===")
    print(f"  基準日: {bs['as_of']}")
    print(f"  sign 規約: ASSET 正値 = 残高あり、LIABILITY/EQUITY/RE は符号反転後の正値 = 残高あり")

    _print_section("資産 (Assets)", bs['assets'], bs['total_assets'])
    _print_section("負債 (Liabilities)", bs['liabilities'], bs['total_liabilities'])
    _print_section("純資産 / 出資勘定 (Equity accounts)", bs['equity_accounts'], bs['total_equity_accounts'])

    print(f"\n【当期未閉鎖 RE (Retained Earnings)】")
    print(f"  当期 INCOME - EXPENSE       {fmt(bs['retained_earnings'])}")
    print(f"  ↑ 期末閉鎖 (audit §4 K) 未実装のため純資産に未振替の累積利益")

    print(f"\n{'=' * 60}")
    print(f"  資産合計            {fmt(bs['total_assets'])}")
    print(f"  負債合計            {fmt(bs['total_liabilities'])}")
    print(f"  純資産 + RE 合計    {fmt(bs['total_equity_with_re'])}")
    print(f"  ───────────────────────────────────────────────")
    print(f"  資産 − (負債+純資産) {fmt(bs['diff'])}")
    if bs['balanced']:
        print(f"  ✅ 貸借一致")
    else:
        print(f"  ⚠ 貸借不一致 (差額 {bs['diff']:,}円)")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
