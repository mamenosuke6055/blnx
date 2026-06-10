import sys
from pathlib import Path
import pandas as pd
import sqlite3
import json

# Add the project root to the sys.path to allow importing modules
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root))

print(f"DEBUG: project_root = {project_root}")
print(f"DEBUG: sys.path = {sys.path}")

from py.analysis.generate_report import generate_report
from py.analysis.portfolio_summary import generate_portfolio_report

def get_db_path():
    config_path = project_root / "config/settings.json"
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        return project_root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        return project_root / "db/finance.db"

def run_analysis():
    print("Starting financial data analysis...")

    print("Loading data from finance.db...")
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    
    try:
        # Comprehensive query for analysis
        query = """
        SELECT
            t.post_date as Date,
            t.description as Description,
            a.name as Account,
            a.account_type as Type,
            CAST(s.value_num AS REAL) / s.value_denom as Amount,
            c.mnemonic as Currency
        FROM transactions t
        JOIN splits s ON t.guid = s.tx_guid
        JOIN accounts a ON s.account_guid = a.guid
        LEFT JOIN currencies c ON t.currency_guid = c.guid
        ORDER BY t.post_date DESC
        """
        
        processed_df = pd.read_sql_query(query, conn)
        print(f"Loaded {len(processed_df)} rows from finance.db")
        
        # Convert Date to datetime
        processed_df['Date'] = pd.to_datetime(processed_df['Date'])

    except Exception as e:
        print(f"Error loading data from finance.db: {e}")
        return
    finally:
        conn.close()

    if processed_df.empty:
        print("No data found to analyze.")
        return

    # --- Phase 3: Analysis and Reporting ---
    print("Generating financial report...")
    report_output_dir = project_root / "data" / "reports"
    generate_report(processed_df, "financial_overview_report", report_output_dir)

    print("\n--- Portfolio Summary ---")
    try:
        generate_portfolio_report()
    except Exception as e:
        print(f"Error generating portfolio summary: {e}")

    print("\nAnalysis complete. Check the reports directory.")

if __name__ == "__main__":
    run_analysis()