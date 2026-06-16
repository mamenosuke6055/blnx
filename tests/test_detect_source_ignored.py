"""ignored_report（取込対象外 SBI レポートの明確化）のテスト。"""
from pathlib import Path

from py.importers.detect_source import detect_source, ignored_report


def test_summary_reports_are_ignored_without_advice():
    for name in ("summaryAll20260604150511.csv",
                 "summaryYear20260507200301.csv",
                 "shisanchart_20260507.csv"):
        res = ignored_report(Path(name))
        assert res is not None, name
        label, advice = res
        assert advice is None  # サマリー/チャートは代替なし（単に不要）


def test_realized_pl_points_to_yakujo():
    res = ignored_report(Path("realized_pl(INVST)_20260604_145408.csv"))
    assert res is not None
    assert "約定履歴" in res[1]
    assert ignored_report(Path("realized_pl(JP)_20260604_145403.csv")) is not None


def test_assetbalance_invst_points_to_savefile():
    res = ignored_report(Path("assetbalance(INVST)_20260616_223320.csv"))
    assert res is not None
    assert "保有証券一覧" in res[1] or "SaveFile" in res[1]


def test_real_import_files_are_not_ignored():
    for name in ("SaveFile_20260616.csv",
                 "yakujo20260604150407.csv",
                 "nyushukkin20260616221003.csv"):
        assert ignored_report(Path(name)) is None, name


def test_assetbalance_all_still_imports_as_asset_balance():
    # (all) 形式は対象外にせず従来通り取り込む（(invst) だけ対象外）
    p = Path("assetbalance(all)_20250623_120000.csv")
    assert ignored_report(p) is None
    assert detect_source(p) == "asset_balance"
