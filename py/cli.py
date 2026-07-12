"""blnx の統一 CLI エントリポイント。

`scripts/*.py` は薄いランチャースクリプトとして元のまま残し、ここではサブコマンド名から
対応するスクリプトファイルへディスパッチするだけの薄いラッパーを提供する。
実行前に必ずプロジェクトルートへ chdir するため、`uv tool install --editable .` で
インストールした `blnx` コマンドをどのディレクトリから呼んでも `data/` `db/` `config/`
などの相対パスが正しく解決される。引数なしで呼ぶとサブコマンド一覧を表示する。
"""
import argparse
import os
import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# カテゴリ -> {サブコマンド名: (プロジェクトルートからの相対パス, 説明)}
COMMAND_GROUPS = {
    "インポート": {
        "import": ("scripts/run_import.py", "金融機関 CSV を finance.db へ取込(inbox=自動判別 / all 等)"),
        "import-status": ("scripts/run_import_status.py", "取込状況(ウォーターマーク)を表示"),
        "watch-sbi-sec": ("scripts/watch_sbi_sec.py", "SBI証券 CSV の投入を監視して自動取込(--once で一括)"),
        "replay": ("scripts/run_replay.py", "アーカイブ済み CSV からの再取込(リプレイ)"),
    },
    "分類": {
        "categorize": ("scripts/run_categorize.py", "辞書ベースの自動分類"),
        "ai-train": ("scripts/run_ai_training.py", "ML 分類モデルの学習"),
        "ai-predict": ("scripts/run_ai_prediction.py", "ML 分類の実行"),
        "llm-categorize": ("scripts/run_llm_categorize.py", "LLM による分類"),
        "manual-label": ("scripts/run_manual_labeling.py", "手動ラベリング GUI"),
        "sync-manual-dictionary": ("scripts/sync_manual_to_dictionary.py", "手動分類を辞書へ還元(フィードバックループ)"),
    },
    "財務諸表・レポート": {
        "balance-sheet": ("scripts/run_balance_sheet.py", "貸借対照表(資産・負債・純資産)"),
        "cashflow": ("scripts/run_cashflow_statement.py", "キャッシュフロー計算書"),
        "monthly-report": ("scripts/run_monthly_report.py", "月次支出グラフ"),
        "household-report": ("scripts/run_household_report.py", "家計レポート生成"),
        "household-dashboard": ("scripts/run_household_dashboard.py", "家計ダッシュボード(Streamlit)"),
        "interactive-report": ("scripts/run_interactive_report.py", "インタラクティブダッシュボード"),
        "detect-subscriptions": ("scripts/run_detect_subscriptions.py", "定期支出(サブスク)の検出"),
        "analysis": ("scripts/run_analysis.py", "分析レポート・ポートフォリオサマリー"),
        "nlp-query": ("scripts/run_nlp_query.py", "自然言語での問い合わせ"),
    },
    "投資": {
        "rebalance": ("scripts/run_rebalance.py", "ポートフォリオ・リバランス計算"),
        "update-exchange-rates": ("scripts/update_exchange_rates.py", "USD/JPY 為替レートの取得・保存"),
    },
    "初期化": {
        "init-db": ("py/init/init_db.py", "finance.db のスキーマ初期化"),
        "init-dictionary-schema": ("py/init/init_dictionary_schema.py", "分類辞書スキーマの初期化"),
    },
}

# サブコマンド名 -> プロジェクトルートからの相対パス(フラットな索引)
COMMANDS = {
    name: path
    for group in COMMAND_GROUPS.values()
    for name, (path, _) in group.items()
}


def _build_description() -> str:
    lines = ["blnx — 個人ファイナンスデータ配管の統一 CLI", ""]
    for category, cmds in COMMAND_GROUPS.items():
        lines.append(f"[{category}]")
        for name, (_, desc) in cmds.items():
            lines.append(f"  blnx {name:<24} {desc}")
        lines.append("")
    lines.append("サブコマンド以降の引数はそのまま各スクリプトへ渡される(例: blnx import inbox)。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="blnx",
        description=_build_description(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command", nargs="?", choices=sorted(COMMANDS), metavar="command",
        help="実行するサブコマンド(一覧は上記)",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER, help="サブコマンドへ渡す引数")
    ns = parser.parse_args()

    if ns.command is None:
        parser.print_help()
        return

    script_path = PROJECT_ROOT / COMMANDS[ns.command]

    os.chdir(PROJECT_ROOT)

    sys.argv = [str(script_path), *ns.args]
    runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    main()
