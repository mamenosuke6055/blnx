"""取込ウォーターマークを表示する CLI。

各金融機関(source)について「最後に取り込んだ記録の日付」「次回DL目安」
「最後に取込を実行した日時」を一覧する。CSV を再ダウンロードする際に、
どの source をどの日付以降から落とせばよいかの判断に使う。

    uv run python scripts/run_import_status.py          # 表形式
    uv run python scripts/run_import_status.py --json    # JSON(AI-readable)
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from py.analysis.import_watermark import compute_watermarks, render_table  # noqa: E402


def get_db_path() -> Path:
    config_path = project_root / "config/settings.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return project_root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        return project_root / "db/finance.db"


def main() -> None:
    parser = argparse.ArgumentParser(description="金融機関ごとの取込ウォーターマーク(最終記録日/次回DL目安)を表示する。")
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    args = parser.parse_args()

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        rows = compute_watermarks(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=2))
        return

    print(f"取込ウォーターマーク (DB: {db_path})\n")
    print(render_table(rows))
    print("\n次回DL目安 = 最後の記録日の翌日。重複DLしても ofx_fitid で冪等に弾かれるため、")
    print("不安なら目安日より数日前から落として構わない。")


if __name__ == "__main__":
    main()
