"""BarChartWidget construction, fail-soft rendering, and ranking/limiting.

R3: the donut/pie widget was removed -- one shared compact ranked bar
presentation covers moves, abilities, and items.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from maple_next.ui.opponent_intel_charts import (
    _ROW_HEIGHT,
    BarChartWidget,
    ReadableRankedListWidget,
    _row_layout,
    render_entries_as_text,
    top_ranked_entries,
)


def qt_application() -> QApplication:
    return QApplication.instance() or QApplication([])


# A widget must never be constructed before a QApplication exists -- ensure
# one is up before any test in this module builds a QWidget subclass.
qt_application()


def _pump(widget) -> None:
    app = qt_application()
    widget.resize(300, 160)
    widget.show()
    app.processEvents()


def test_bar_chart_normal_data_does_not_raise() -> None:
    widget = BarChartWidget()
    widget.set_entries(
        [
            ("じしん", 80.0, True),
            ("げきりん", 40.0, False),
            ("まもる", None, False),
        ]
    )
    _pump(widget)
    assert not widget.has_render_error()
    widget.deleteLater()


def test_bar_chart_empty_data_does_not_raise() -> None:
    widget = BarChartWidget()
    widget.set_entries([])
    _pump(widget)
    assert not widget.has_render_error()
    widget.deleteLater()


def test_bar_chart_sizehint_is_reasonable() -> None:
    widget = BarChartWidget()
    hint = widget.sizeHint()
    assert hint.width() > 0
    assert hint.height() > 0
    widget.deleteLater()


def test_bar_chart_sizehint_scales_with_row_count_not_entry_percentage() -> None:
    """Compact footprint: a 1-row chart and a 5-row chart use proportionally
    different heights at the exact same per-row thickness -- never a single
    100% entry ballooning to fill a fixed oversized area."""

    one_row = BarChartWidget()
    one_row.set_entries([("A", 100.0, False)])
    five_rows = BarChartWidget()
    five_rows.set_entries(
        [(f"P{i}", 20.0, False) for i in range(5)]
    )

    one_row_height = one_row.sizeHint().height()
    five_row_height = five_rows.sizeHint().height()
    assert five_row_height - one_row_height == 4 * _ROW_HEIGHT
    one_row.deleteLater()
    five_rows.deleteLater()


def test_bar_chart_entries_are_capped_defensively() -> None:
    widget = BarChartWidget()
    widget.set_entries([(f"P{i}", float(i), False) for i in range(20)])
    _pump(widget)
    assert not widget.has_render_error()
    widget.deleteLater()


def test_forced_paint_error_sets_flag_and_does_not_propagate() -> None:
    widget = BarChartWidget()
    widget.set_entries([("じしん", 80.0, True)])

    def _boom(painter) -> None:
        raise RuntimeError("forced paint failure")

    widget._paint = _boom  # type: ignore[method-assign]  # noqa: SLF001
    _pump(widget)  # must not raise despite the forced failure
    assert widget.has_render_error()
    widget.deleteLater()


def test_render_entries_as_text_bar_shape() -> None:
    text = render_entries_as_text([("じしん", 80.0, True), ("まもる", None, False)])
    assert "じしん" in text
    assert "80.0%" in text
    assert "確認済み" in text
    assert "--%" in text


def test_render_entries_as_text_legacy_two_tuple_shape() -> None:
    text = render_entries_as_text([("すながくれ", 60.0)])
    assert "すながくれ" in text
    assert "60.0%" in text


def test_render_entries_as_text_empty() -> None:
    assert render_entries_as_text([]) == "データなし"


def test_top_ranked_entries_sorts_descending_by_percentage() -> None:
    entries = [("low", 10.0, False), ("high", 90.0, False), ("mid", 50.0, False)]
    assert top_ranked_entries(entries, 5) == [
        ("high", 90.0, False),
        ("mid", 50.0, False),
        ("low", 10.0, False),
    ]


def test_top_ranked_entries_puts_unranked_none_last() -> None:
    entries = [("unranked", None, False), ("known", 5.0, False)]
    assert top_ranked_entries(entries, 5) == [
        ("known", 5.0, False),
        ("unranked", None, False),
    ]


def test_top_ranked_entries_applies_the_limit() -> None:
    entries = [(f"P{i}", float(i), False) for i in range(10)]
    top = top_ranked_entries(entries, 3)
    assert len(top) == 3
    assert [name for name, _, _ in top] == ["P9", "P8", "P7"]


def _assert_regions_do_not_overlap(layout) -> None:  # noqa: ANN001
    regions = [
        (layout.label_left, layout.label_width),
        (layout.badge_left, layout.badge_width),
        (layout.bar_left, layout.bar_width),
        (layout.percent_left, layout.percent_width),
    ]
    regions.sort()
    for (left, width), (next_left, _next_width) in zip(regions, regions[1:], strict=False):
        assert left + width <= next_left, (
            f"region [{left}, {left + width}) overlaps the next region starting at "
            f"{next_left}"
        )


def test_row_layout_regions_do_not_overlap_at_representative_desktop_width() -> None:
    """R3R1 mandatory case 14: label / badge / bar / percentage never
    geometrically overlap, at both the ~1920x1080 nominal per-chart width
    and the enforced minimum right-column floor width."""

    for width in (90, 150, 190, 260, 400):
        _assert_regions_do_not_overlap(_row_layout(0, width))


def test_bar_chart_long_label_with_badge_and_percentage_does_not_raise() -> None:
    widget = BarChartWidget()
    widget.set_entries(
        [
            ("とてもながいポケモンわざのなまえのれい", 100.0, True),
            ("イダイトウ (オス) すごくながいこだわりのもちもの", 45.0, True),
        ]
    )
    widget.resize(180, widget.sizeHint().height())
    _pump(widget)
    assert not widget.has_render_error()
    assert widget.toolTip() != ""
    widget.deleteLater()


def test_bar_chart_tooltip_only_set_when_something_is_observed() -> None:
    widget = BarChartWidget()
    widget.set_entries([("A", 10.0, False), ("B", 5.0, False)])
    assert widget.toolTip() == ""
    widget.deleteLater()


def _row_labels(widget: ReadableRankedListWidget) -> list[QLabel]:
    labels = []
    for i in range(widget._layout.count()):  # noqa: SLF001
        row = widget._layout.itemAt(i).widget()  # noqa: SLF001
        if row is None:
            continue
        for child in row.findChildren(QLabel):
            labels.append(child)
    return labels


def test_readable_ranked_list_never_truncates_long_japanese_names() -> None:
    """The main-panel readability hotfix: a long move/item name must render
    in full, with no "..." ellipsis, even at the narrow width the old
    3-column-per-row layout used to force truncation at."""

    long_move = "とんぼがえりインファイトふいうちきあいのタスキこだわりスカーフ"
    widget = ReadableRankedListWidget()
    widget.set_entries([(long_move, 78.0, False), ("インファイト", 56.0, True)])
    widget.resize(90, widget.sizeHint().height())  # narrower than the old 1/3-column width
    _pump(widget)

    labels = _row_labels(widget)
    full_texts = [label.text() for label in labels]
    assert any(long_move in text for text in full_texts)
    assert not any("..." in text or "…" in text for text in full_texts)
    name_labels = [label for label in labels if long_move in label.text()]
    assert name_labels and all(label.wordWrap() is True for label in name_labels)
    widget.deleteLater()


def test_readable_ranked_list_shows_percentage_per_row() -> None:
    widget = ReadableRankedListWidget()
    widget.set_entries([("ふいうち", 78.0, False), ("かえんだん", None, False)])
    _pump(widget)

    texts = [label.text() for label in _row_labels(widget)]
    assert any("78.0%" in text for text in texts)
    assert any("--%" in text for text in texts)
    widget.deleteLater()


def test_readable_ranked_list_empty_shows_placeholder_not_crash() -> None:
    widget = ReadableRankedListWidget()
    widget.set_entries([])
    _pump(widget)
    assert widget._layout.count() == 1  # noqa: SLF001
    widget.deleteLater()


def test_readable_ranked_list_row_count_matches_entries() -> None:
    widget = ReadableRankedListWidget()
    entries = [("A", 10.0, False), ("B", 20.0, False), ("C", 30.0, True)]
    widget.set_entries(entries)
    assert widget._layout.count() == len(entries)  # noqa: SLF001
    widget.deleteLater()
