"""BarChartWidget/DonutChartWidget construction and fail-soft rendering."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from maple_next.ui.opponent_intel_charts import (
    BarChartWidget,
    DonutChartWidget,
    render_entries_as_text,
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


def test_donut_chart_normal_data_does_not_raise() -> None:
    widget = DonutChartWidget()
    widget.set_entries([("すながくれ", 60.0), ("さめはだ", 25.0), ("不明", None)])
    widget.set_center_text("候補")
    _pump(widget)
    assert not widget.has_render_error()
    widget.deleteLater()


def test_donut_chart_empty_data_does_not_raise() -> None:
    widget = DonutChartWidget()
    widget.set_entries([])
    _pump(widget)
    assert not widget.has_render_error()
    widget.deleteLater()


def test_donut_chart_all_none_percentages_does_not_raise() -> None:
    widget = DonutChartWidget()
    widget.set_entries([("A", None), ("B", None)])
    _pump(widget)
    assert not widget.has_render_error()
    widget.deleteLater()


def test_donut_chart_sizehint_is_reasonable() -> None:
    widget = DonutChartWidget()
    hint = widget.sizeHint()
    assert hint.width() > 0
    assert hint.height() > 0
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


def test_render_entries_as_text_donut_shape() -> None:
    text = render_entries_as_text([("すながくれ", 60.0)])
    assert "すながくれ" in text
    assert "60.0%" in text


def test_render_entries_as_text_empty() -> None:
    assert render_entries_as_text([]) == "データなし"
