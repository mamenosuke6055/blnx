"""box_table レンダラのテスト（特に全角混在の桁揃え）。"""

from py.util.box_table import display_width, render_box


def test_display_width_counts_fullwidth_as_two():
    assert display_width("abc") == 3
    assert display_width("家賃") == 4          # 全角2文字 = 4
    assert display_width("6-7月") == 5         # ASCII3 + 全角1(月)=2


def test_render_box_basic_structure():
    out = render_box(["id", "label"], [["1", "Rent"]])
    lines = out.splitlines()
    assert lines[0].startswith("┌") and lines[0].endswith("┐")
    assert lines[1] == "│ id │ label │"
    assert lines[2].startswith("├") and "┼" in lines[2]
    assert lines[-1].startswith("└") and lines[-1].endswith("┘")


def test_fullwidth_rows_align():
    # 全角ラベルでも全行の枠線長が一致する（桁揃え）
    out = render_box(
        ["label", "amount"],
        [["家賃", "¥75,000"], ["旅行費用", "¥80,000"]],
        ["l", "r"],
    )
    lengths = {display_width(line) for line in out.splitlines()}
    assert len(lengths) == 1  # 全行が同じ表示幅


def test_right_align_pads_left():
    out = render_box(["n"], [["1"], ["100"]], ["r"])
    rows = [ln for ln in out.splitlines() if ln.startswith("│")]
    assert rows[1] == "│   1 │"   # 右寄せ
    assert rows[2] == "│ 100 │"


def test_empty_rows_renders_header_only():
    out = render_box(["a", "b"], [])
    lines = out.splitlines()
    assert lines[1] == "│ a │ b │"
    assert len(lines) == 4  # top, header, sep, bottom
