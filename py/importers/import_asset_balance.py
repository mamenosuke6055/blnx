import pandas as pd
import sqlite3
import json
from pathlib import Path
import glob
import re
import csv

def get_project_root() -> Path:
    """プロジェクトのルートディレクトリを取得します。"""
    return Path(__file__).resolve().parent.parent.parent

def safe_float(s: str) -> float:
    """カンマを除去し、空文字列の場合は0.0を返す安全なfloat変換"""
    s_cleaned = s.replace(',', '')
    return float(s_cleaned) if s_cleaned else 0.0

def parse_asset_balance_csv(csv_path: Path):
    """
    資産残高CSVを解析し、スナップショット日、総資産、カテゴリ別詳細を抽出します。
    """
    # ファイル名から日付を抽出 (例: assetbalance(all)_20250623_... -> 2025-06-23)
    match = re.search(r'_(\d{8})_', csv_path.name)
    if not match:
        print(f"エラー: ファイル名 '{csv_path.name}' から日付を抽出できませんでした。")
        return None, None, None
    snapshot_date = pd.to_datetime(match.group(1), format='%Y%m%d').strftime('%Y-%m-%d')

    total_assets = None
    asset_details = []

    with open(csv_path, 'r', encoding='cp932') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            
            # 全角スペースや不要な文字を削除
            cleaned_row = [cell.strip().replace(' ', '').replace('　', '') for cell in row]
            
            # 総資産の行を特定
            if cleaned_row[0] == '資産合計':
                total_assets = safe_float(cleaned_row[1])

            # カテゴリ詳細の行を特定
            categories = ['国内株式', '米国株式', '中国株式', 'アセアン株式', '投資信託']
            if cleaned_row[0] in categories:
                category = cleaned_row[0]
                valuation = safe_float(cleaned_row[1])
                profit_loss = safe_float(cleaned_row[6])
                asset_details.append({
                    'snapshot_date': snapshot_date,
                    'asset_category': category,
                    'valuation': valuation,
                    'profit_loss': profit_loss
                })

    return snapshot_date, total_assets, asset_details


def import_asset_balance_csv(csv_path: Path, db_path: str = None):
    """
    資産残高CSVを読み込み、データベースに登録します。
    """
    PROJECT_ROOT = get_project_root()
    
    if db_path is None:
        # db_pathが指定されていない場合、設定ファイルから取得
        CONFIG_FILE = PROJECT_ROOT / "config/settings.json"
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            db_path = PROJECT_ROOT / settings.get("db_path", "db/finance.db")
        except FileNotFoundError:
            print(f"エラー: 設定ファイル '{CONFIG_FILE}' が見つかりません。デフォルトパス 'db/finance.db' を使用します。")
            db_path = PROJECT_ROOT / "db/finance.db"

    snapshot_date, total_assets, asset_details = parse_asset_balance_csv(csv_path)

    if not snapshot_date or total_assets is None or not asset_details:
        print(f"'{csv_path.name}' から有効なデータを抽出できませんでした。")
        return

    try:
        # データベースディレクトリが存在しない場合は作成
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # テーブルを初回自動作成（asset_snapshotsはimport_rakuten_sec.pyが別スキーマで使うため別名）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                snapshot_date TEXT PRIMARY KEY,
                total_assets REAL NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS balance_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL,
                asset_category TEXT NOT NULL,
                valuation REAL,
                profit_loss REAL
            )
        """)

        # --- balance_snapshotsテーブルへの挿入（重複チェックあり） ---
        cursor.execute("SELECT 1 FROM balance_snapshots WHERE snapshot_date = ?", (snapshot_date,))
        if cursor.fetchone():
            print(f"'{snapshot_date}' の資産スナップショットは既に存在するため、スキップします。")
        else:
            cursor.execute(
                "INSERT INTO balance_snapshots (snapshot_date, total_assets) VALUES (?, ?)",
                (snapshot_date, total_assets)
            )
            print(f"'{snapshot_date}' の楽天証券 総資産 ({total_assets:,.0f}円) をインポートしました。")

        # --- balance_detailsテーブルへの挿入（冪等）---
        cursor.execute("DELETE FROM balance_details WHERE snapshot_date = ?", (snapshot_date,))
        details_df = pd.DataFrame(asset_details)
        if not details_df.empty:
            details_df.to_sql('balance_details', conn, if_exists='append', index=False)
            print(f"'{snapshot_date}' の資産カテゴリ詳細 {len(details_df)}件をインポートしました。")

        conn.commit()
        conn.close()

    except sqlite3.Error as e:
        print(f"データベースエラー: {e}")
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}")


if __name__ == '__main__':
    PROJECT_ROOT = get_project_root()
    csv_files = glob.glob(str(PROJECT_ROOT / "data" / "raw" / "portfolio" / "assetbalance*.csv"))
    
    if not csv_files:
        print("資産残高CSVファイルが見つかりません。")
    else:
        for csv_file in csv_files:
            import_asset_balance_csv(Path(csv_file))
