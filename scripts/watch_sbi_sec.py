"""
SBI証券CSVウォッチャー
data/raw/sbi_sec/ にCSVファイルが追加されたら自動的にインポートを実行する。

使い方:
    uv run python scripts/watch_sbi_sec.py          # 監視開始
    uv run python scripts/watch_sbi_sec.py --once   # 既存ファイルを処理して終了
"""

import sys
import time
import argparse
import logging
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))

from py.importers.import_sbi_sec import import_sbi_sec_csv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

WATCH_DIR = project_root / "data" / "raw" / "sbi_sec"
SETTLE_DELAY = 2.0  # ファイルが書き込み完了するまで待つ秒数


def process_csv(path: Path):
    logger.info(f"検知: {path.name} → インポート開始")
    try:
        import_sbi_sec_csv(path)
        logger.info(f"完了: {path.name}")
    except Exception as e:
        logger.error(f"エラー: {path.name}: {e}")


def run_once():
    """既存のCSVをすべて処理して終了。"""
    csv_files = sorted(WATCH_DIR.glob("*.csv"))
    if not csv_files:
        logger.info(f"CSVファイルが見つかりません: {WATCH_DIR}")
        return
    for f in csv_files:
        process_csv(f)


def run_watch():
    """ディレクトリを監視し、新規CSVを自動インポートする。"""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent
    except ImportError:
        logger.error("watchdog がインストールされていません。`uv pip install watchdog` を実行してください。")
        sys.exit(1)

    WATCH_DIR.mkdir(parents=True, exist_ok=True)

    class CsvHandler(FileSystemEventHandler):
        def _handle(self, path_str: str):
            path = Path(path_str)
            if path.suffix.lower() != ".csv":
                return
            time.sleep(SETTLE_DELAY)
            if path.exists():
                process_csv(path)

        def on_created(self, event):
            if not event.is_directory:
                self._handle(event.src_path)

        def on_moved(self, event):
            if not event.is_directory:
                self._handle(event.dest_path)

    observer = Observer()
    observer.schedule(CsvHandler(), str(WATCH_DIR), recursive=False)
    observer.start()
    logger.info(f"監視中: {WATCH_DIR}  (Ctrl+C で停止)")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        logger.info("ウォッチャー停止")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SBI証券CSVウォッチャー")
    parser.add_argument("--once", action="store_true", help="既存ファイルを処理して終了")
    args = parser.parse_args()

    if args.once:
        run_once()
    else:
        run_watch()
