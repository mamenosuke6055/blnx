"""blnx の統一 CLI エントリポイント。

`scripts/*.py` は薄いランチャースクリプトとして元のまま残し、ここではサブコマンド名から
対応するスクリプトファイルへディスパッチするだけの薄いラッパーを提供する。
実行前に必ずプロジェクトルートへ chdir するため、`uv tool install --editable .` で
インストールした `blnx` コマンドをどのディレクトリから呼んでも `data/` `db/` `config/`
などの相対パスが正しく解決される。
"""
import argparse
import os
import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# サブコマンド名 -> プロジェクトルートからの相対パス
COMMANDS = {
    # インポート
    "import": "scripts/run_import.py",
    "import-status": "scripts/run_import_status.py",
    "watch-sbi-sec": "scripts/watch_sbi_sec.py",
    # 分類
    "categorize": "scripts/run_categorize.py",
    "ai-train": "scripts/run_ai_training.py",
    "ai-predict": "scripts/run_ai_prediction.py",
    "llm-categorize": "scripts/run_llm_categorize.py",
    "manual-label": "scripts/run_manual_labeling.py",
    "sync-manual-dictionary": "scripts/sync_manual_to_dictionary.py",
    # 財務諸表・レポート
    "balance-sheet": "scripts/run_balance_sheet.py",
    "cashflow": "scripts/run_cashflow_statement.py",
    "monthly-report": "scripts/run_monthly_report.py",
    "household-report": "scripts/run_household_report.py",
    "household-dashboard": "scripts/run_household_dashboard.py",
    "interactive-report": "scripts/run_interactive_report.py",
    "detect-subscriptions": "scripts/run_detect_subscriptions.py",
    "analysis": "scripts/run_analysis.py",
    "nlp-query": "scripts/run_nlp_query.py",
    # 投資
    "rebalance": "scripts/run_rebalance.py",
    "replay": "scripts/run_replay.py",
    "update-exchange-rates": "scripts/update_exchange_rates.py",
    # 初期化
    "init-db": "py/init/init_db.py",
    "init-dictionary-schema": "py/init/init_dictionary_schema.py",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="blnx",
        description="blnx — 個人ファイナンスデータ配管の統一 CLI",
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="実行するサブコマンド")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="サブコマンドへ渡す引数")
    ns = parser.parse_args()

    script_path = PROJECT_ROOT / COMMANDS[ns.command]

    os.chdir(PROJECT_ROOT)

    sys.argv = [str(script_path), *ns.args]
    runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    main()
