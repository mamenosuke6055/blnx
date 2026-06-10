import sqlite3
import json
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "finance_reports"

# 投資振替・カード引落し・ATM引出し等、実質支出から除外するカテゴリ
EXCLUDED_EXPENSE_CATEGORIES = {"現金・カード", "税・社会保障"}

# 固定費に分類するカテゴリ
FIXED_COST_CATEGORIES = {"住宅", "保険", "水道・光熱費", "通信費"}

# 変動費に分類するカテゴリ
VARIABLE_COST_CATEGORIES = {"食費", "日用品", "交通費", "自動車", "衣服・美容", "健康・医療"}

# 趣味・娯楽系
HOBBY_CATEGORIES = {"趣味・娯楽", "教養・教育", "交際費"}


def load_config():
    config_path = BASE_DIR / "config" / "settings.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_db_connection(db_path):
    try:
        conn = sqlite3.connect(db_path)
        return conn
    except sqlite3.Error as e:
        logging.error(f"DB接続エラー {db_path}: {e}")
        return None


def get_monthly_income_expense(conn, target_year, target_month, months=12):
    """直近N ヶ月の月別収支（手取り・実質支出・差額・貯蓄率）を返す。"""

    # target_year/target_month を終点として months 分さかのぼる
    end_dt = datetime(target_year, target_month, 1)
    start_dt = end_dt - timedelta(days=months * 31)
    start_dt = start_dt.replace(day=1)

    # 月末を次月1日で表現
    if target_month == 12:
        end_boundary = f"{target_year + 1}-01-01"
    else:
        end_boundary = f"{target_year}-{target_month + 1:02d}-01"

    query = """
    WITH RECURSIVE account_tree AS (
        SELECT guid, name, name AS root_name, account_type
        FROM accounts
        WHERE parent_guid IN (SELECT guid FROM accounts WHERE name IN ('Income', 'Expenses'))
        UNION ALL
        SELECT a.guid, a.name, t.root_name, a.account_type
        FROM accounts a
        JOIN account_tree t ON a.parent_guid = t.guid
    )
    SELECT
        strftime('%Y-%m', t.post_date) AS month,
        at.account_type,
        at.root_name,
        SUM(s.value_num * 1.0 / s.value_denom) AS amount
    FROM transactions t
    JOIN splits s ON t.guid = s.tx_guid
    JOIN account_tree at ON s.account_guid = at.guid
    WHERE t.post_date >= ? AND t.post_date < ?
    GROUP BY month, at.account_type, at.root_name
    ORDER BY month
    """
    df = pd.read_sql_query(query, conn, params=[start_dt.strftime("%Y-%m-%d"), end_boundary])

    # 月ごとに集計
    months_list = []
    for month_str, grp in df.groupby("month"):
        income = grp.loc[
            (grp["account_type"] == "INCOME") & (grp["amount"] < 0), "amount"
        ].sum()
        income = abs(income)  # 収入は splits で負値

        # 実質支出: 正の EXPENSE split から除外カテゴリを引く
        expense_mask = (grp["account_type"] == "EXPENSE") & (grp["amount"] > 0)
        excluded_mask = expense_mask & grp["root_name"].isin(EXCLUDED_EXPENSE_CATEGORIES)
        real_expense = grp.loc[expense_mask & ~grp["root_name"].isin(EXCLUDED_EXPENSE_CATEGORIES), "amount"].sum()

        diff = income - real_expense
        if income > 0:
            savings_rate = diff / income * 100
        else:
            savings_rate = None

        months_list.append({
            "month": month_str,
            "income": int(round(income)),
            "expense": int(round(real_expense)),
            "diff": int(round(diff)),
            "savings_rate": savings_rate,
        })

    return months_list


def get_category_breakdown(conn, year, month):
    """当月のカテゴリ別支出を取得（投資振替等を除外）。"""

    start_date = f"{year}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1}-01-01"
    else:
        end_date = f"{year}-{month + 1:02d}-01"

    query = """
    WITH RECURSIVE account_tree AS (
        SELECT guid, name, name AS root_name
        FROM accounts
        WHERE parent_guid = (SELECT guid FROM accounts WHERE name = 'Expenses')
        UNION ALL
        SELECT a.guid, a.name, t.root_name
        FROM accounts a
        JOIN account_tree t ON a.parent_guid = t.guid
    )
    SELECT
        at.root_name AS category,
        at.name AS sub_category,
        SUM(s.value_num * 1.0 / s.value_denom) AS amount,
        COUNT(*) AS tx_count
    FROM transactions t
    JOIN splits s ON t.guid = s.tx_guid
    JOIN account_tree at ON s.account_guid = at.guid
    WHERE t.post_date >= ? AND t.post_date < ?
      AND s.value_num > 0
    GROUP BY at.root_name, at.name
    ORDER BY amount DESC
    """
    df = pd.read_sql_query(query, conn, params=[start_date, end_date])
    return df


def get_yoy_comparison(conn, year, month):
    """前月比・前年同月比を取得するためのヘルパー。"""
    results = {}

    for label, y, m in [("当月", year, month),
                         ("前月", year if month > 1 else year - 1, month - 1 if month > 1 else 12),
                         ("前年同月", year - 1, month)]:
        start = f"{y}-{m:02d}-01"
        if m == 12:
            end = f"{y + 1}-01-01"
        else:
            end = f"{y}-{m + 1:02d}-01"

        query = """
        WITH RECURSIVE account_tree AS (
            SELECT guid, name, name AS root_name
            FROM accounts
            WHERE parent_guid = (SELECT guid FROM accounts WHERE name = 'Expenses')
            UNION ALL
            SELECT a.guid, a.name, t.root_name
            FROM accounts a
            JOIN account_tree t ON a.parent_guid = t.guid
        )
        SELECT
            SUM(s.value_num * 1.0 / s.value_denom) AS total
        FROM transactions t
        JOIN splits s ON t.guid = s.tx_guid
        JOIN account_tree at ON s.account_guid = at.guid
        WHERE t.post_date >= ? AND t.post_date < ?
          AND s.value_num > 0
          AND at.root_name NOT IN ('現金・カード', '税・社会保障')
        """
        row = pd.read_sql_query(query, conn, params=[start, end])
        total = row["total"].iloc[0] if not row.empty and row["total"].iloc[0] is not None else 0
        results[label] = int(round(total))

    return results


def render_markdown_report(year, month, monthly_data, category_df, comparison, output_dir):
    """Markdown テキストを組み立てて保存する。"""

    lines = []
    lines.append(f"# 家計分析レポート {year}年{month}月")
    lines.append("")
    lines.append(f"生成日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"データソース: finance.db")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 1. 月次収支推移テーブル
    lines.append("## 月次収支推移")
    lines.append("")
    lines.append("| 月 | 手取り | 実質支出 | 差額 | 貯蓄率 |")
    lines.append("|---|---:|---:|---:|---:|")
    for row in monthly_data:
        m = row["month"]
        inc = f'{row["income"]:,}'
        exp = f'{row["expense"]:,}'
        d = row["diff"]
        diff_str = f'+{d:,}' if d >= 0 else f'{d:,}'
        if row["savings_rate"] is not None and row["income"] > 0:
            if d < 0:
                sr = "赤字"
            else:
                sr = f'{row["savings_rate"]:.0f}%'
        else:
            sr = "-"
        lines.append(f"| {m} | {inc} | {exp} | {diff_str} | {sr} |")
    lines.append("")
    lines.append("※実質支出 = 投資振替・カード引落し・ATM引出し・税社保を除外した生活支出")
    lines.append("")

    # 2. カテゴリ別支出ランキング
    if not category_df.empty:
        # 除外カテゴリを除く
        filtered = category_df[~category_df["category"].isin(EXCLUDED_EXPENSE_CATEGORIES)].copy()

        # カテゴリ大分類ごとの合計
        cat_totals = filtered.groupby("category").agg(
            amount=("amount", "sum"),
            tx_count=("tx_count", "sum")
        ).sort_values("amount", ascending=False).reset_index()

        lines.append(f"## {year}年{month}月 カテゴリ別支出")
        lines.append("")
        lines.append("| カテゴリ | 金額 | 件数 |")
        lines.append("|---|---:|---:|")
        for _, r in cat_totals.iterrows():
            lines.append(f"| {r['category']} | {int(r['amount']):,} | {int(r['tx_count'])} |")
        lines.append("")

        # 3. 固定費 / 変動費 / 趣味 分類サマリー
        lines.append("## 支出分類サマリー")
        lines.append("")

        for group_name, group_cats in [("固定費", FIXED_COST_CATEGORIES),
                                        ("変動費", VARIABLE_COST_CATEGORIES),
                                        ("趣味・娯楽・教養", HOBBY_CATEGORIES)]:
            subset = cat_totals[cat_totals["category"].isin(group_cats)]
            total = int(subset["amount"].sum())
            lines.append(f"- **{group_name}**: {total:,}円")
            for _, r in subset.iterrows():
                lines.append(f"  - {r['category']}: {int(r['amount']):,}円")

        # その他（上記いずれにも属さない）
        all_grouped = FIXED_COST_CATEGORIES | VARIABLE_COST_CATEGORIES | HOBBY_CATEGORIES | EXCLUDED_EXPENSE_CATEGORIES
        others = cat_totals[~cat_totals["category"].isin(all_grouped)]
        if not others.empty:
            total = int(others["amount"].sum())
            lines.append(f"- **その他**: {total:,}円")
            for _, r in others.iterrows():
                lines.append(f"  - {r['category']}: {int(r['amount']):,}円")

        lines.append("")

        # カテゴリ別 サブカテゴリ TOP5 の内訳
        lines.append("## サブカテゴリ別 TOP10")
        lines.append("")
        sub_totals = filtered.copy()
        # root_name と同名の sub_category はスキップ（集約行）
        sub_totals = sub_totals[sub_totals["category"] != sub_totals["sub_category"]]
        sub_totals = sub_totals.sort_values("amount", ascending=False).head(10)
        if not sub_totals.empty:
            lines.append("| カテゴリ | サブカテゴリ | 金額 | 件数 |")
            lines.append("|---|---|---:|---:|")
            for _, r in sub_totals.iterrows():
                lines.append(f"| {r['category']} | {r['sub_category']} | {int(r['amount']):,} | {int(r['tx_count'])} |")
            lines.append("")

    # 4. 前月比・前年同月比
    lines.append("## 前月比・前年同月比")
    lines.append("")
    current = comparison.get("当月", 0)
    prev_month = comparison.get("前月", 0)
    prev_year = comparison.get("前年同月", 0)

    if prev_month > 0:
        mom_diff = current - prev_month
        mom_pct = (mom_diff / prev_month) * 100
        sign = "+" if mom_diff >= 0 else ""
        lines.append(f"- 前月比: {sign}{mom_diff:,}円 ({sign}{mom_pct:.1f}%)")
    else:
        lines.append(f"- 前月比: データなし")

    if prev_year > 0:
        yoy_diff = current - prev_year
        yoy_pct = (yoy_diff / prev_year) * 100
        sign = "+" if yoy_diff >= 0 else ""
        lines.append(f"- 前年同月比: {sign}{yoy_diff:,}円 ({sign}{yoy_pct:.1f}%)")
    else:
        lines.append(f"- 前年同月比: データなし")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"生成元: Claude Code 家計分析スクリプト")

    # ファイル保存
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{year}-{month:02d}_household_analysis.md"
    output_path = output_dir / filename

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    logging.info(f"レポートを保存しました: {output_path}")
    return output_path


def generate_household_report(year=None, month=None, output_dir=None):
    """メイン関数。省略時は前月分を生成。"""

    if year is None or month is None:
        today = datetime.today()
        first_of_month = today.replace(day=1)
        prev_month = first_of_month - timedelta(days=1)
        year = prev_month.year
        month = prev_month.month

    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR

    logging.info(f"{year}年{month}月の家計分析レポートを生成します...")

    config = load_config()
    db_path = BASE_DIR / config["db_path"]

    conn = get_db_connection(str(db_path))
    if not conn:
        return None

    try:
        monthly_data = get_monthly_income_expense(conn, year, month, months=12)
        category_df = get_category_breakdown(conn, year, month)
        comparison = get_yoy_comparison(conn, year, month)

        output_path = render_markdown_report(
            year, month, monthly_data, category_df, comparison, output_dir
        )
        return output_path
    finally:
        conn.close()
        logging.info("処理完了。")


if __name__ == "__main__":
    generate_household_report()
