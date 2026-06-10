import sys
from pathlib import Path
import os

# Add the project root to the Python path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from py.ai.train_model import train_and_save_model

def main():
    """
    Entry point for the AI model training script.
    """
    print("==================================================")
    print("      Running AI Category Model Training")
    print("==================================================")
    
    # Set the current working directory to the project root
    os.chdir(project_root)
    
    train_and_save_model()
    
    print("\nModel training process finished.")
    print("==================================================")


if __name__ == "__main__":
    main()

