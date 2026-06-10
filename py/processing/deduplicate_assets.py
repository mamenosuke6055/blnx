import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PROCESSED = BASE_DIR / "data/processed"

def deduplicate_assets(file_name):
    df = pd.read_csv(DATA_PROCESSED / file_name)
    df = df.drop_duplicates()
    df.to_csv(DATA_PROCESSED / file_name, index=False)
    print(f"{file_name} の重複削除完了")

if __name__ == "__main__":
    deduplicate_assets("assetbalance(all)_20250623_213732.csv")
