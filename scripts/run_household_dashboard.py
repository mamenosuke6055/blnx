import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
streamlit_app_path = project_root / "py" / "gui" / "household_dashboard.py"

def main():
    """
    Runs the Streamlit household dashboard application.
    """
    print("==================================================")
    print("  Launching Household Dashboard")
    print("==================================================")
    print(f"App path: {streamlit_app_path}")
    print("Please open the URL provided by Streamlit in your browser.")
    print("To stop the server, press Ctrl+C in this terminal.")

    command = ["uv", "run", "streamlit", "run", str(streamlit_app_path)]

    try:
        subprocess.run(command, check=True, cwd=project_root)
    except FileNotFoundError:
        print("Error: uv が見つかりません。uv をインストールしてください。")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"An error occurred while running the Streamlit app: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
