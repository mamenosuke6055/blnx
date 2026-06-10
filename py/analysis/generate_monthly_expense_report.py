import sqlite3
import pandas as pd
from pathlib import Path
import matplotlib.pyplot as plt
import japanize_matplotlib
from datetime import datetime, timedelta
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Define project base directory and paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
FINANCE_DB_PATH = BASE_DIR / "db" / "finance.db"
REPORTS_DIR = BASE_DIR / "data" / "reports"

def get_db_connection(db_path):
    """Establishes a connection to the SQLite database."""
    try:
        conn = sqlite3.connect(db_path)
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to database {db_path}: {e}")
        return None

def get_monthly_expense_data(conn, year, month):
    """
    Retrieves and aggregates expense data for a specific month.
    """
    start_date = f"{year}-{month:02d}-01"
    end_date = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=32)).strftime("%Y-%m-01")

    query = f"""
    WITH RECURSIVE account_hierarchy AS (
        -- Base case: root accounts (Expenses)
        SELECT
            guid,
            name,
            guid as root_guid,
            name as root_name
        FROM accounts
        WHERE parent_guid = (SELECT guid FROM accounts WHERE name = 'Expenses')

        UNION ALL

        -- Recursive step: child accounts
        SELECT
            a.guid,
            a.name,
            ah.root_guid,
            ah.root_name
        FROM accounts a
        JOIN account_hierarchy ah ON a.parent_guid = ah.guid
    )
    SELECT
        ah.root_name as parent_category,
        SUM(s.value_num * 1.0 / s.value_denom) as amount
    FROM transactions t
    JOIN splits s ON t.guid = s.tx_guid
    JOIN account_hierarchy ah ON s.account_guid = ah.guid
    WHERE
        t.post_date >= '{start_date}' AND t.post_date < '{end_date}'
        AND s.value_num > 0 -- Consider only positive values for expenses
    GROUP BY
        parent_category
    HAVING
        amount > 0;
    """
    try:
        df = pd.read_sql_query(query, conn)
        logging.info(f"Successfully retrieved {len(df)} expense categories for {year}-{month}.")
        return df
    except Exception as e:
        logging.error(f"Failed to retrieve monthly expense data: {e}")
        return pd.DataFrame()

def generate_expense_pie_chart(df, year, month):
    """
    Generates and saves a pie chart of the expense data.
    """
    if df.empty:
        logging.warning("DataFrame is empty. Cannot generate chart.")
        return

    # Create reports directory if it doesn't exist
    REPORTS_DIR.mkdir(exist_ok=True)

    # Plotting the pie chart
    plt.figure(figsize=(12, 10))
    wedges, texts, autotexts = plt.pie(
        df['amount'],
        labels=df['parent_category'],
        autopct='%1.1f%%',
        startangle=140,
        pctdistance=0.85
    )
    
    # Style adjustments
    plt.setp(texts, size=12)
    plt.setp(autotexts, size=10, color="white")
    
    # Draw a circle at the center to make it a donut chart
    centre_circle = plt.Circle((0,0),0.70,fc='white')
    fig = plt.gcf()
    fig.gca().add_artist(centre_circle)

    # Title and labels
    total_expense = df['amount'].sum()
    plt.title(f'{year}年{month}月 支出レポート\n総支出: {total_expense:,.0f} 円', fontsize=16)
    plt.axis('equal')  # Equal aspect ratio ensures that pie is drawn as a circle.

    # Save the figure
    output_path = REPORTS_DIR / f"monthly_expense_report_{year}-{month:02d}.png"
    plt.savefig(output_path, bbox_inches='tight')
    logging.info(f"Report saved to {output_path}")
    plt.close()


def generate_monthly_report(year=None, month=None):
    """
    Main function to generate the monthly expense report.
    If year or month is not provided, it defaults to the previous month.
    """
    if year is None or month is None:
        today = datetime.today()
        first_day_of_current_month = today.replace(day=1)
        last_day_of_previous_month = first_day_of_current_month - timedelta(days=1)
        year = last_day_of_previous_month.year
        month = last_day_of_previous_month.month

    logging.info(f"Generating expense report for {year}-{month}...")

    conn = get_db_connection(FINANCE_DB_PATH)
    if not conn:
        return

    try:
        expense_df = get_monthly_expense_data(conn, year, month)
        if not expense_df.empty:
            generate_expense_pie_chart(expense_df, year, month)
        else:
            logging.info("No expense data found for the specified period.")
    finally:
        conn.close()
        logging.info("Process finished.")


if __name__ == "__main__":
    # Example usage: generate a report for the previous month
    generate_monthly_report()

    # Example usage: generate a report for a specific month
    # generate_monthly_report(year=2025, month=10)
