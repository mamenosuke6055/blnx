import streamlit as st
import sqlite3
import pandas as pd
from pathlib import Path
import plotly.express as px
from datetime import datetime
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Define project base directory and paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
FINANCE_DB_PATH = BASE_DIR / "db" / "finance.db"

def get_db_connection(db_path):
    """Establishes a connection to the SQLite database."""
    try:
        return sqlite3.connect(db_path)
    except sqlite3.Error as e:
        st.error(f"Error connecting to database: {e}")
        return None

@st.cache_data
def get_expense_data(_conn, start_date, end_date):
    """
    Retrieves hierarchical expense data for a specific date range, suitable for a treemap chart.
    """
    query = f"""
    WITH RECURSIVE account_hierarchy AS (
        -- Start with parent accounts under 'Expenses'
        SELECT 
            guid, 
            name, 
            guid as parent_guid, 
            name as parent_name,
            0 as level
        FROM accounts
        WHERE parent_guid = (SELECT guid FROM accounts WHERE name = 'Expenses')

        UNION ALL

        -- Recursively find sub-accounts
        SELECT 
            a.guid, 
            a.name, 
            ah.parent_guid, 
            ah.parent_name,
            ah.level + 1
        FROM accounts a
        JOIN account_hierarchy ah ON a.parent_guid = ah.guid
    )
    SELECT
        ah.parent_name as main_category,
        a.name as sub_category,
        SUM(s.value_num * 1.0 / s.value_denom) as amount
    FROM transactions t
    JOIN splits s ON t.guid = s.tx_guid
    -- Use the account directly from the splits table
    JOIN accounts a ON s.account_guid = a.guid
    -- Join with hierarchy to get main/sub categories
    JOIN account_hierarchy ah ON a.parent_guid = ah.guid OR (a.guid = ah.guid AND ah.level = 0)
    WHERE
        t.post_date >= '{start_date}' AND t.post_date < '{end_date}'
        AND s.value_num > 0
        AND a.account_type = 'EXPENSE'
    GROUP BY
        main_category, sub_category
    HAVING
        amount > 0;
    """
    try:
        df = pd.read_sql_query(query, _conn)
        df['root'] = '支出' # Add a root category for the chart
        logging.info(f"Successfully retrieved {len(df)} expense records for {start_date} to {end_date}.")
        return df
    except Exception as e:
        st.error(f"Failed to retrieve expense data: {e}")
        return pd.DataFrame()

@st.cache_data
def get_trend_data(_conn, end_year, end_month=None, mode='monthly'):
    """
    Retrieves trend expense data.
    mode='monthly': Past 12 months from end_year-end_month.
    mode='yearly': Past 5 years from end_year.
    """
    if mode == 'monthly':
        # Calculate start date (12 months back)
        end_date_obj = datetime(end_year, end_month, 1) + pd.DateOffset(months=1)
        start_date_obj = end_date_obj - pd.DateOffset(months=12)
        group_format = '%Y-%m'
    else: # yearly
        # Calculate start date (5 years back)
        end_date_obj = datetime(end_year + 1, 1, 1) 
        start_date_obj = datetime(end_year - 4, 1, 1)
        group_format = '%Y'
    
    start_date_str = start_date_obj.strftime("%Y-%m-%d")
    end_date_str = end_date_obj.strftime("%Y-%m-%d")

    query = f"""
    WITH RECURSIVE account_hierarchy AS (
        -- Start with parent accounts under 'Expenses'
        SELECT 
            guid, 
            name, 
            guid as parent_guid, 
            name as parent_name,
            0 as level
        FROM accounts
        WHERE parent_guid = (SELECT guid FROM accounts WHERE name = 'Expenses')

        UNION ALL

        -- Recursively find sub-accounts
        SELECT 
            a.guid, 
            a.name, 
            ah.parent_guid, 
            ah.parent_name,
            ah.level + 1
        FROM accounts a
        JOIN account_hierarchy ah ON a.parent_guid = ah.guid
    )
    SELECT
        strftime('{group_format}', t.post_date) as period,
        ah.parent_name as main_category,
        SUM(s.value_num * 1.0 / s.value_denom) as amount
    FROM transactions t
    JOIN splits s ON t.guid = s.tx_guid
    JOIN accounts a ON s.account_guid = a.guid
    JOIN account_hierarchy ah ON a.parent_guid = ah.guid OR (a.guid = ah.guid AND ah.level = 0)
    WHERE
        t.post_date >= '{start_date_str}' AND t.post_date < '{end_date_str}'
        AND s.value_num > 0
        AND a.account_type = 'EXPENSE'
    GROUP BY
        period, main_category
    HAVING
        amount > 0
    ORDER BY
        period;
    """
    try:
        df = pd.read_sql_query(query, _conn)
        logging.info(f"Successfully retrieved trend data from {start_date_str} to {end_date_str}.")
        return df
    except Exception as e:
        st.error(f"Failed to retrieve trend data: {e}")
        return pd.DataFrame()

def main():
    """
    Main function to run the Streamlit application.
    """
    st.set_page_config(page_title="支出レポート", layout="wide")
    st.title("📊 支出インタラクティブレポート")

    conn = get_db_connection(FINANCE_DB_PATH)
    if not conn:
        return

    # --- Sidebar for user input ---
    st.sidebar.header("設定")
    
    # Report Type Selection
    report_type = st.sidebar.radio("集計単位", ["月次", "年次"])
    
    current_year = datetime.now().year
    
    # Date Selection
    if report_type == "月次":
        selected_year = st.sidebar.selectbox("年", range(current_year - 5, current_year + 1), index=5)
        selected_month = st.sidebar.selectbox("月", range(1, 13), index=datetime.now().month - 1)
        
        start_date = f"{selected_year}-{selected_month:02d}-01"
        end_date = (datetime.strptime(start_date, "%Y-%m-%d").replace(day=1) + pd.DateOffset(months=1)).strftime("%Y-%m-%d")
        header_text = f"{selected_year}年{selected_month}月の支出分析"
        trend_title = "過去12ヶ月の支出推移"
        trend_mode = 'monthly'
        
    else: # 年次
        selected_year = st.sidebar.selectbox("年", range(current_year - 5, current_year + 1), index=5)
        selected_month = None # Not used
        
        start_date = f"{selected_year}-01-01"
        end_date = f"{selected_year + 1}-01-01"
        header_text = f"{selected_year}年の支出分析"
        trend_title = "過去5年の支出推移"
        trend_mode = 'yearly'

    # --- Main content ---
    st.header(header_text)

    # Fetch expense data for the selected period
    expense_df = get_expense_data(conn, start_date, end_date)

    if expense_df.empty:
        st.warning("この期間の支出データはありません。")
    else:
        # Calculate total expense
        total_expense = expense_df['amount'].sum()
        st.metric(label="総支出額", value=f"{total_expense:,.0f} 円")

        # --- Treemap Chart ---
        st.subheader("支出カテゴリの割合 (トレマップ)")
        
        fig = px.treemap(
            expense_df,
            path=['root', 'main_category', 'sub_category'],
            values='amount',
            color='main_category',
            hover_data=['amount'],
            title="面積が大きいほど支出が多いカテゴリです"
        )
        
        fig.update_layout(
            margin=dict(t=40, l=0, r=0, b=0),
            height=600,
        )
        fig.update_traces(
            textinfo='label+value+percent parent',
            hovertemplate='<b>%{label}</b><br>金額: %{value:,.0f} 円<br>割合: %{percentParent:.1%}'
        )
        
        st.plotly_chart(fig, use_container_width=True)

    # --- Trend Chart ---
    st.subheader(trend_title)
    trend_df = get_trend_data(conn, selected_year, selected_month, mode=trend_mode)
    
    if not trend_df.empty:
        fig_trend = px.bar(
            trend_df,
            x="period",
            y="amount",
            color="main_category",
            title=f"支出推移 ({'月別' if trend_mode == 'monthly' else '年別'}・カテゴリ別積み上げ)",
            labels={"period": "期間", "amount": "支出額", "main_category": "カテゴリ"},
            text_auto='.2s'
        )
        fig_trend.update_layout(xaxis_type='category')
        st.plotly_chart(fig_trend, use_container_width=True)
    else:
        st.info("過去の推移データがありません。")

    if not expense_df.empty:
        # --- Filter for detailed data ---
        st.subheader("詳細データ")
        main_categories = ["すべて"] + sorted(expense_df['main_category'].unique().tolist())
        selected_main_category = st.selectbox("親カテゴリで絞り込み", main_categories)

        # --- Data Table ---
        with st.expander("詳細データを見る"):
            filtered_df = expense_df.copy()
            if selected_main_category != "すべて":
                filtered_df = filtered_df[filtered_df['main_category'] == selected_main_category]
            
            st.dataframe(
                filtered_df[['main_category', 'sub_category', 'amount']].sort_values(
                    by='amount', ascending=False
                ).style.format({"amount": "{:,.0f} 円"})
            )

if __name__ == "__main__":
    main()
