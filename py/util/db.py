"""DB パス解決の共通ユーティリティ。"""
import json
from pathlib import Path


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def get_db_path() -> Path:
    """config/settings.json の db_path を解決する（無ければ既定の db/finance.db）。"""
    root = get_project_root()
    config = root / "config/settings.json"
    try:
        with open(config, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        return root / settings.get("db_path", "db/finance.db")
    except FileNotFoundError:
        return root / "db/finance.db"
