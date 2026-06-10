import pandas as pd
from ydata_profiling import ProfileReport
from pathlib import Path

def generate_report(df: pd.DataFrame, report_name: str, output_dir: Path):
    """
    Generates an HTML profiling report using ydata-profiling.

    Args:
        df (pd.DataFrame): The DataFrame to profile.
        report_name (str): The name of the report file (without extension).
        output_dir (Path): The directory where the report will be saved.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{report_name}.html"
    profile = ProfileReport(df, title=report_name.replace("_", " ").title(), explorative=True)
    profile.to_file(report_path)
    print(f"Report generated at: {report_path}")

if __name__ == "__main__":
    # This is a placeholder for demonstration. In a real scenario,
    # you would load your processed data here.
    print("Running generate_report.py directly is for testing purposes.")
    print("Please ensure you have processed data available.")

    # Example dummy data
    data = {
        "col1": [1, 2, 3, 4, 5],
        "col2": ["A", "B", "A", "C", "B"],
        "col3": pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-04", "2023-01-05"])
    }
    dummy_df = pd.DataFrame(data)

    output_directory = Path("../..") / "data" / "reports"
    generate_report(dummy_df, "sample_finance_report", output_directory)