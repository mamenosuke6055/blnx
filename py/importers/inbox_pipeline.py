"""inbox 取込パイプライン: アーカイブ → 取込 → 新規/重複レポート → processed 退避。

scripts/run_import.py から呼ばれる本体ロジック。テスト容易性のため py/ 配下に置く
(scripts/ は薄いCLIラッパーに留める方針)。各ステップ:

  1. detect_source で source 判別
  2. raw_archive へ生CSVをアーカイブ(sha256 で再DL重複を冪等スキップ)
  3. 対応 importer を実行(ofx_fitid で冪等)
  4. 取込前後の transactions 件数差で「新規 N 件」を測る(importer 非改変・source 非依存)
  5. 成功ファイルを processed/ へ退避し、末尾に取込ウォーターマークを表示
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from py.importers import import_rakutenbank
from py.importers import import_rakutencard
from py.importers import import_asset_balance
from py.importers import import_amazon_history
from py.importers import import_rakuten_sec
from py.importers import import_dneobank
from py.importers import import_sbi_sec
from py.importers import import_resona_bank
from py.importers.detect_source import detect_source
from py.importers import raw_archive
from py.importers import import_gate
from py.analysis.import_watermark import compute_watermarks, render_table


def _import_one(csv_file: Path, source: str, db_path: Path) -> None:
    if source == 'rakuten_bank':
        import_rakutenbank.import_rakuten_bank_csv(csv_file, db_path)
    elif source == 'rakuten_card':
        import_rakutencard.import_rakuten_card_csv(csv_file, db_path)
    elif source == 'rakuten_sec':
        import_rakuten_sec.import_rakuten_sec_csv(csv_file)
    elif source == 'dneobank':
        import_dneobank.import_dneobank_csv(csv_file)
    elif source == 'sbi_sec':
        import_sbi_sec.import_sbi_sec_csv(csv_file)
    elif source == 'resona_bank':
        import_resona_bank.import_resona_bank_csv(csv_file, db_path)
    elif source == 'asset_balance':
        import_asset_balance.import_asset_balance_csv(csv_file, db_path)
    elif source == 'amazon':
        import_amazon_history.import_amazon_history(csv_file, db_path)


def _count_transactions(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()


def process_inbox_file(csv_file: Path, db_path: Path, raw_conn: sqlite3.Connection) -> dict:
    """1ファイルを「アーカイブ→取込」する。副作用はDB2本のみ(ファイル移動はしない)。

    返す record:
      file, source, archived(bool), duplicate_file(bool: 再DLで既知),
      new_tx(int: finance.db への新規取引数), imported(bool), error(str|None)
    """
    record = {"file": csv_file.name, "source": None, "archived": False,
              "duplicate_file": False, "new_tx": None, "imported": False, "error": None,
              "period": None, "period_overlap": []}

    source = detect_source(csv_file)
    record["source"] = source
    if source is None:
        record["error"] = "種別不明"
        return record

    try:
        ar = raw_archive.archive_file(raw_conn, csv_file, source=source)
        record["archived"] = True
        record["duplicate_file"] = ar.is_duplicate
    except Exception as e:
        record["error"] = f"アーカイブ失敗: {e}"
        return record

    # 取込ゲート: 同一期間・別フォーマットの再取込(2フォーマット二重取込)を上申。
    # period 算出可能な source のみ。重なりを検知しても取込は止めない(冪等のため)。
    period = import_gate.extract_period(csv_file, source)
    record["period"] = period
    if period and not record["duplicate_file"]:
        record["period_overlap"] = import_gate.find_period_overlaps(
            raw_conn, source, period[0], period[1], exclude_id=ar.row_id
        )

    before = _count_transactions(db_path)
    try:
        _import_one(csv_file, source, db_path)
    except Exception as e:
        record["error"] = f"取込失敗: {e}"
        return record
    after = _count_transactions(db_path)

    record["new_tx"] = after - before
    record["imported"] = True
    raw_archive.update_import_meta(
        raw_conn, ar.row_id, new_rows=record["new_tx"],
        period_start=period[0] if period else None,
        period_end=period[1] if period else None,
    )
    return record


def run_inbox_import(inbox_dir: Path, db_path: Path, processed_dir: Path | None = None,
                     show_watermark: bool = True) -> list[dict]:
    print(f"インポート元ディレクトリ: {inbox_dir}")
    csv_files = sorted(inbox_dir.glob("*.csv"))
    if not csv_files:
        print("CSVファイルが見つかりませんでした。")
        return []

    if processed_dir is None:
        processed_dir = inbox_dir.parent / "processed"

    print(f"{len(csv_files)} 件のCSVファイルを検出しました。\n")

    raw_conn = raw_archive.open_raw_db()
    records: list[dict] = []
    try:
        for csv_file in csv_files:
            rec = process_inbox_file(csv_file, db_path, raw_conn)
            records.append(rec)
            if rec["error"]:
                print(f"  [スキップ/{rec['source'] or '不明'}] {csv_file.name} — {rec['error']}")
                continue
            dup = "（再DL・既知ファイル）" if rec["duplicate_file"] else ""
            print(f"  [{rec['source']}] {csv_file.name} — 新規 {rec['new_tx']} 件{dup}")
            if rec["period_overlap"]:
                ovl = "、".join(f"{r['filename']}({r['period_start']}〜{r['period_end']})"
                               for r in rec["period_overlap"])
                print(f"    ⚠ 上申: 同一期間の既存アーカイブと重複 [{rec['period'][0]}〜{rec['period'][1]}] "
                      f"↔ {ovl}")
                print(f"      → フォーマット違いの二重取込の可能性。Tier-3 二重計上を "
                      f"scripts/cleanup_rakutencard_tier3.py 等で確認のこと。")
            # 取込成功したファイルのみ processed/ へ退避(失敗はinboxに残し再試行可能に)
            processed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(csv_file), str(processed_dir / csv_file.name))
    finally:
        raw_conn.close()

    ok = [r for r in records if r["imported"]]
    skipped = [r for r in records if not r["imported"]]
    total_new = sum(r["new_tx"] for r in ok)
    print(f"\n取込完了: {len(ok)} ファイル / 新規 {total_new} 件"
          + (f" / スキップ {len(skipped)} ファイル" if skipped else ""))
    if skipped:
        for r in skipped:
            print(f"  - {r['file']}: {r['error']}")

    if show_watermark:
        finance_conn = sqlite3.connect(db_path)
        try:
            print("\n=== 取込ウォーターマーク（次回DL目安）===")
            print(render_table(compute_watermarks(finance_conn)))
        finally:
            finance_conn.close()

    return records
