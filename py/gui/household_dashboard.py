import streamlit as st
import sqlite3
import json
import pandas as pd
from pathlib import Path
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import logging
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# generate_household_report のモジュールを import
sys.path.insert(0, str(BASE_DIR))
from py.analysis.generate_household_report import (
    get_monthly_income_expense,
    get_category_breakdown,
    get_yoy_comparison,
    EXCLUDED_EXPENSE_CATEGORIES,
    FIXED_COST_CATEGORIES,
    VARIABLE_COST_CATEGORIES,
    HOBBY_CATEGORIES,
)


def load_config():
    config_path = BASE_DIR / "config" / "settings.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_db_connection(db_path):
    try:
        return sqlite3.connect(db_path)
    except sqlite3.Error as e:
        st.error(f"DB接続エラー: {e}")
        return None


@st.cache_data
def cached_monthly_income_expense(_conn, year, month, months=12):
    return get_monthly_income_expense(_conn, year, month, months)


@st.cache_data
def cached_category_breakdown(_conn, year, month):
    return get_category_breakdown(_conn, year, month)


@st.cache_data
def cached_yoy_comparison(_conn, year, month):
    return get_yoy_comparison(_conn, year, month)


def get_available_months(conn):
    """DBに存在する年月の一覧を取得する。"""
    query = """
    SELECT DISTINCT strftime('%Y-%m', post_date) AS month
    FROM transactions
    ORDER BY month DESC
    """
    df = pd.read_sql_query(query, conn)
    return df["month"].tolist()


def main():
    st.set_page_config(page_title="家計分析ダッシュボード", layout="wide")
    st.title("家計分析ダッシュボード")

    config = load_config()
    db_path = BASE_DIR / config["db_path"]
    conn = get_db_connection(str(db_path))
    if not conn:
        return

    # --- サイドバー: 対象年月セレクタ ---
    st.sidebar.header("設定")

    available_months = get_available_months(conn)
    if not available_months:
        st.warning("データベースにトランザクションがありません。")
        return

    selected_month_str = st.sidebar.selectbox("対象年月", available_months, index=0)
    selected_year = int(selected_month_str[:4])
    selected_month = int(selected_month_str[5:7])

    # --- データ取得 ---
    monthly_data = cached_monthly_income_expense(conn, selected_year, selected_month, months=12)
    category_df = cached_category_breakdown(conn, selected_year, selected_month)
    comparison = cached_yoy_comparison(conn, selected_year, selected_month)

    # 当月データを探す
    current_month_str = f"{selected_year}-{selected_month:02d}"
    current_data = next((m for m in monthly_data if m["month"] == current_month_str), None)

    # 前月データを探す
    if selected_month == 1:
        prev_month_str = f"{selected_year - 1}-12"
    else:
        prev_month_str = f"{selected_year}-{selected_month - 1:02d}"
    prev_data = next((m for m in monthly_data if m["month"] == prev_month_str), None)

    # ===== メトリクスカード (3列) =====
    st.header(f"{selected_year}年{selected_month}月")

    if current_data:
        col1, col2, col3 = st.columns(3)

        prev_income = prev_data["income"] if prev_data else None
        prev_expense = prev_data["expense"] if prev_data else None
        prev_sr = prev_data["savings_rate"] if prev_data else None

        col1.metric(
            label="手取り",
            value=f"{current_data['income']:,} 円",
            delta=f"{current_data['income'] - prev_income:,} 円" if prev_income is not None else None,
        )
        col2.metric(
            label="実質支出",
            value=f"{current_data['expense']:,} 円",
            delta=f"{current_data['expense'] - prev_expense:,} 円" if prev_expense is not None else None,
            delta_color="inverse",
        )
        sr_value = f"{current_data['savings_rate']:.0f}%" if current_data["savings_rate"] is not None else "-"
        sr_delta = None
        if current_data["savings_rate"] is not None and prev_sr is not None:
            sr_delta = f"{current_data['savings_rate'] - prev_sr:+.1f}pt"
        col3.metric(
            label="貯蓄率",
            value=sr_value,
            delta=sr_delta,
        )
    else:
        st.info("当月のデータがありません。")

    # ===== 月次収支推移 (折れ線 + 棒グラフ、直近12ヶ月) =====
    st.subheader("月次収支推移（直近12ヶ月）")

    if monthly_data:
        trend_df = pd.DataFrame(monthly_data)

        fig_trend = go.Figure()

        # 差額 (棒グラフ、正負で色分け)
        colors = ["#2ecc71" if d >= 0 else "#e74c3c" for d in trend_df["diff"]]
        fig_trend.add_trace(go.Bar(
            x=trend_df["month"],
            y=trend_df["diff"],
            name="差額",
            marker_color=colors,
            opacity=0.5,
        ))

        # 手取り (線)
        fig_trend.add_trace(go.Scatter(
            x=trend_df["month"],
            y=trend_df["income"],
            name="手取り",
            mode="lines+markers",
            line=dict(color="#3498db", width=2),
        ))

        # 実質支出 (線)
        fig_trend.add_trace(go.Scatter(
            x=trend_df["month"],
            y=trend_df["expense"],
            name="実質支出",
            mode="lines+markers",
            line=dict(color="#e67e22", width=2),
        ))

        fig_trend.update_layout(
            xaxis_title="月",
            yaxis_title="金額 (円)",
            xaxis_type="category",
            height=400,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="x unified",
        )
        st.plotly_chart(fig_trend, use_container_width=True)
    else:
        st.info("推移データがありません。")

    # ===== カテゴリ別支出 + 固定費/変動費/趣味 ドーナツ =====
    if not category_df.empty:
        filtered = category_df[~category_df["category"].isin(EXCLUDED_EXPENSE_CATEGORIES)].copy()

        if not filtered.empty:
            cat_totals = filtered.groupby("category").agg(
                amount=("amount", "sum"),
                tx_count=("tx_count", "sum"),
            ).sort_values("amount", ascending=False).reset_index()

            col_bar, col_donut = st.columns(2)

            # カテゴリ別支出 (横棒グラフ)
            with col_bar:
                st.subheader("カテゴリ別支出")
                fig_cat = px.bar(
                    cat_totals,
                    x="amount",
                    y="category",
                    orientation="h",
                    text_auto=",.0f",
                    labels={"amount": "金額 (円)", "category": "カテゴリ"},
                )
                fig_cat.update_layout(
                    yaxis=dict(autorange="reversed"),
                    height=400,
                )
                fig_cat.update_traces(textposition="outside")
                st.plotly_chart(fig_cat, use_container_width=True)

            # 固定費/変動費/趣味 ドーナツチャート
            with col_donut:
                st.subheader("支出分類")
                all_grouped = FIXED_COST_CATEGORIES | VARIABLE_COST_CATEGORIES | HOBBY_CATEGORIES | EXCLUDED_EXPENSE_CATEGORIES
                donut_data = []
                for label, cats in [("固定費", FIXED_COST_CATEGORIES),
                                    ("変動費", VARIABLE_COST_CATEGORIES),
                                    ("趣味・娯楽・教養", HOBBY_CATEGORIES)]:
                    total = cat_totals[cat_totals["category"].isin(cats)]["amount"].sum()
                    if total > 0:
                        donut_data.append({"分類": label, "金額": total})

                others_total = cat_totals[~cat_totals["category"].isin(all_grouped)]["amount"].sum()
                if others_total > 0:
                    donut_data.append({"分類": "その他", "金額": others_total})

                if donut_data:
                    donut_df = pd.DataFrame(donut_data)
                    fig_donut = px.pie(
                        donut_df,
                        values="金額",
                        names="分類",
                        hole=0.45,
                    )
                    fig_donut.update_traces(
                        textinfo="label+percent",
                        hovertemplate="<b>%{label}</b><br>%{value:,.0f} 円<br>%{percent}",
                    )
                    fig_donut.update_layout(height=400)
                    st.plotly_chart(fig_donut, use_container_width=True)

            # ===== サブカテゴリ TOP10 =====
            st.subheader("サブカテゴリ TOP10")
            sub_totals = filtered[filtered["category"] != filtered["sub_category"]].copy()
            sub_totals = sub_totals.sort_values("amount", ascending=False).head(10)

            if not sub_totals.empty:
                sub_totals["label"] = sub_totals["category"] + " / " + sub_totals["sub_category"]
                fig_sub = px.bar(
                    sub_totals,
                    x="amount",
                    y="label",
                    orientation="h",
                    text_auto=",.0f",
                    color="category",
                    labels={"amount": "金額 (円)", "label": "サブカテゴリ", "category": "カテゴリ"},
                )
                fig_sub.update_layout(
                    yaxis=dict(autorange="reversed"),
                    height=400,
                    showlegend=False,
                )
                fig_sub.update_traces(textposition="outside")
                st.plotly_chart(fig_sub, use_container_width=True)
            else:
                st.info("サブカテゴリデータがありません。")

    # ===== 前月比・前年同月比 =====
    st.subheader("前月比・前年同月比")

    current_total = comparison.get("当月", 0)
    prev_month_total = comparison.get("前月", 0)
    prev_year_total = comparison.get("前年同月", 0)

    col_mom, col_yoy = st.columns(2)

    if prev_month_total > 0:
        mom_diff = current_total - prev_month_total
        mom_pct = (mom_diff / prev_month_total) * 100
        col_mom.metric(
            label="前月比 (実質支出)",
            value=f"{current_total:,} 円",
            delta=f"{mom_diff:+,} 円 ({mom_pct:+.1f}%)",
            delta_color="inverse",
        )
    else:
        col_mom.metric(label="前月比", value="データなし")

    if prev_year_total > 0:
        yoy_diff = current_total - prev_year_total
        yoy_pct = (yoy_diff / prev_year_total) * 100
        col_yoy.metric(
            label="前年同月比 (実質支出)",
            value=f"{current_total:,} 円",
            delta=f"{yoy_diff:+,} 円 ({yoy_pct:+.1f}%)",
            delta_color="inverse",
        )
    else:
        col_yoy.metric(label="前年同月比", value="データなし")

    # ===== 詳細テーブル =====
    if not category_df.empty:
        with st.expander("詳細テーブル"):
            filtered_detail = category_df[~category_df["category"].isin(EXCLUDED_EXPENSE_CATEGORIES)].copy()
            if not filtered_detail.empty:
                filtered_detail["amount"] = filtered_detail["amount"].astype(int)
                filtered_detail["tx_count"] = filtered_detail["tx_count"].astype(int)
                st.dataframe(
                    filtered_detail[["category", "sub_category", "amount", "tx_count"]]
                    .rename(columns={
                        "category": "カテゴリ",
                        "sub_category": "サブカテゴリ",
                        "amount": "金額",
                        "tx_count": "件数",
                    })
                    .sort_values("金額", ascending=False)
                    .style.format({"金額": "{:,} 円"}),
                    use_container_width=True,
                )

    conn.close()


if __name__ == "__main__":
    main()
