import sys
from pathlib import Path
import os
import argparse
from datetime import datetime

# Add the project root to the Python path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from py.analysis.generate_monthly_expense_report import generate_monthly_report

def main():
    """
    Entry point for the monthly report generation script.
    Parses command-line arguments for year and month.
    """
    parser = argparse.ArgumentParser(description="Generate a monthly expense report.")
    parser.add_argument(
        '-y', '--year',
        type=int,
        help="The year of the report (e.g., 2025). Defaults to the previous month's year."
    )
    parser.add_argument(
        '-m', '--month',
        type=int,
        help="The month of the report (1-12). Defaults to the previous month."
    )
    args = parser.parse_args()

    # Determine year and month
    year, month = args.year, args.month
    
    if year and not month:
        parser.error("If you specify a year, you must also specify a month.")
    if month and not year:
        # If only month is given, use the current year
        year = datetime.now().year

    print("==================================================")
    print("  Running Monthly Expense Report Generation")
    print("==================================================")
    
    os.chdir(project_root)
    
    generate_monthly_report(year=year, month=month)
    
    print("\nReport generation process finished.")
    print("Please check the 'data/reports' directory for the output.")
    print("==================================================")

if __name__ == "__main__":
    main()
