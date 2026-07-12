import sys
from pathlib import Path
import os

# Add the project root to the Python path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.ai.predict_category import predict_categories

def main():
    """
    Entry point for the AI category prediction script.
    """
    print("==================================================")
    print("     Running AI Category Prediction")
    print("==================================================")
    
    # Set the current working directory to the project root
    os.chdir(project_root)
    
    predict_categories()
    
    print("\nCategory prediction process finished.")
    print("==================================================")


if __name__ == "__main__":
    main()

