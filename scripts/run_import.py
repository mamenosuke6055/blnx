import sys
import argparse
import json
from pathlib import Path

# プロジェクトルートをPythonパスに追加
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.importers import import_rakutenbank
from py.importers import import_rakutencard
from py.importers import import_resona_bank
from py.importers import import_rakuten_sec
from py.importers import import_dneobank
from py.importers import import_sbi_sec
from py.importers import import_asset_balance
from py.importers import import_amazon_history
from py.importers.inbox_pipeline import run_inbox_import

def get_db_path():
    config_path = project_root / "config/settings.json"
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        return project_root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        print(f"警告: 設定ファイル '{config_path}' が見つかりません。デフォルトのDBパス 'db/finance.db' を使用します。")
        return project_root / "db/finance.db"

def get_inbox_path():
    inbox = project_root / "data" / "inbox"
    if inbox.exists():
        return inbox
    return project_root / "data"

def main():
    parser = argparse.ArgumentParser(description="金融機関のCSVデータをデータベースにインポートします。")
    parser.add_argument(
        "source",
        nargs='?',
        default="inbox",
        choices=['all', 'inbox', 'rakuten_bank', 'resona_bank', 'rakuten_card', 'asset_balance', 'amazon', 'rakuten_sec', 'dneobank', 'sbi_sec'],
        help=(
            "インポートするデータソース。"
            "省略時は 'inbox'（data/ 直下の CSV を自動判別して一括取込）。"
            "'all' は data/raw/<機関>/ サブディレクトリを機関別に取込む。"
        )
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="inboxモード時のCSVディレクトリ（省略時: data/inbox/ または data/）"
    )
    args = parser.parse_args()

    db_path = get_db_path()
    print(f"インポート先データベース: {db_path}")

    if args.source == 'inbox':
        inbox_dir = Path(args.dir) if args.dir else get_inbox_path()
        run_inbox_import(inbox_dir, db_path)
        return

    print(f"実行対象: {args.source}\n")

    if args.source in ["all", "rakuten_bank"]:
        print("--- 楽天銀行 ---")
        try:
            bank_csv_dir = project_root / "data" / "raw" / "rakuten_bank"
            for csv_file in bank_csv_dir.glob("*.csv"):
                import_rakutenbank.import_rakuten_bank_csv(csv_file, db_path)
        except Exception as e:
            print(f"楽天銀行のインポート中にエラーが発生しました: {e}")

    if args.source in ["all", "resona_bank"]:
        print("--- りそな銀行 ---")
        try:
            resona_csv_dir = project_root / "data" / "raw" / "resona_bank"
            for csv_file in resona_csv_dir.glob("*.csv"):
                import_resona_bank.import_resona_bank_csv(csv_file, db_path)
        except Exception as e:
            print(f"りそな銀行のインポート中にエラーが発生しました: {e}")

    if args.source in ["all", "rakuten_card"]:
        print("--- 楽天カード ---")
        try:
            card_csv_dir = project_root / "data" / "raw" / "rakuten_card"
            for csv_file in card_csv_dir.glob("*.csv"):
                import_rakutencard.import_rakuten_card_csv(csv_file, db_path)
        except Exception as e:
            print(f"楽天カードのインポート中にエラーが発生しました: {e}")

    if args.source in ["all", "rakuten_sec"]:
        print("--- 楽天証券 ---")
        try:
            sec_csv_dir = project_root / "data" / "raw" / "rakuten_sec"
            for csv_file in sec_csv_dir.glob("*.csv"):
                import_rakuten_sec.import_rakuten_sec_csv(csv_file)
        except Exception as e:
            print(f"楽天証券のインポート中にエラーが発生しました: {e}")

    if args.source in ["all", "dneobank"]:
        print("--- 住信SBIネット銀行 ---")
        try:
            dneo_csv_dir = project_root / "data" / "raw" / "dneobank"
            for csv_file in dneo_csv_dir.glob("*.csv"):
                import_dneobank.import_dneobank_csv(csv_file)
        except Exception as e:
            print(f"住信SBIネット銀行のインポート中にエラーが発生しました: {e}")

    if args.source == "sbi_sec":  # allには含めない
        print("--- SBI証券 ---")
        try:
            sbi_csv_dir = project_root / "data" / "raw" / "sbi_sec"
            for csv_file in sbi_csv_dir.glob("*.csv"):
                import_sbi_sec.import_sbi_sec_csv(csv_file)
        except Exception as e:
            print(f"SBI証券のインポート中にエラーが発生しました: {e}")

    if args.source in ["all", "asset_balance"]:
        print("--- 資産残高（楽天証券）---")
        try:
            asset_csv_dir = project_root / "data" / "raw" / "portfolio"
            for csv_file in asset_csv_dir.glob("assetbalance*.csv"):
                import_asset_balance.import_asset_balance_csv(csv_file, db_path)
        except Exception as e:
            print(f"資産残高のインポート中にエラーが発生しました: {e}")

    if args.source in ["all", "amazon"]:
        print("--- Amazon購入履歴 ---")
        try:
            amazon_csv_dir = project_root / "data" / "raw" / "amazon"
            for csv_file in amazon_csv_dir.glob("*.csv"):
                import_amazon_history.import_amazon_history(csv_file, db_path)
        except Exception as e:
            print(f"Amazon購入履歴のインポート中にエラーが発生しました: {e}")

    print("\nすべてのインポート処理が完了しました。")

if __name__ == "__main__":
    main()
