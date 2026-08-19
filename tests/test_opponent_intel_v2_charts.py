"""BarChartWidget construction, fail-soft rendering, and ranking/limiting.

R3: the donut/pie widget was removed -- one shared compact ranked bar
presentation covers moves, abilities, and items.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.ui.opponent_intel_charts import (
    _ROW_HEIGHT,
    BarChartWidget,
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
