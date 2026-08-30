"""Hand-drawn QPainter chart widgets for the Opponent INTEL v2 panel.

No QtCharts, matplotlib, or pyqtgraph dependency -- everything here is a
plain :class:`QWidget` subclass that paints itself. The widget is
fail-soft: a rendering exception is caught inside ``paintEvent`` and never
propagates, so a chart bug can never crash the surrounding Battle Record UI.
Callers should check :meth:`has_render_error` and fall back to
:func:`render_entries_as_text` when it is ``True``.

One shared compact ranked-bar presentation covers every category (moves,
abilities, items) -- there is deliberately no separate donut/pie widget.
A single 100% entry (e.g. a species with only one known ability) renders as
one short bar, never a chart that balloons to fill its allotted space.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

_MAX_BAR_ENTRIES = 8
_OBSERVED_BADGE_EN = "OBSERVED"
_OBSERVED_BADGE_JA = "確認済み"
#: Uniform row thickness in pixels, shared by every BarChartWidget instance
#: regardless of how many rows it holds -- moves/abilities/items must read
#: as one consistent system, not three independently-scaled charts.
_ROW_HEIGHT = 20
_BADGE_WIDTH = 16
_PERCENT_WIDTH = 52


class _RowLayout(NamedTuple):
    """Four left-to-right reserved regions for one bar-chart row.

    Each region is a disjoint ``[left, left + width)`` span -- computed once
    per paint/geometry check so the label, observed badge, bar, and
    percentage can never be proven (or accidentally made) to overlap.
    """

    label_left: float
    label_width: float
    badge_left: float
    badge_width: float
    bar_left: float
    bar_width: float
    percent_left: float
    percent_width: float


def _row_layout(rect_left: float, rect_width: float) -> _RowLayout:
    badge_width = _BADGE_WIDTH
    percent_width = _PERCENT_WIDTH
    label_width = max(min(rect_width // 3, 100) - badge_width, 24)
    label_left = rect_left
    badge_left = label_left + label_width + 2
    bar_left = badge_left + badge_width + 6
    bar_width = max(rect_width - label_width - badge_width - percent_width - 20, 20)
    percent_left = bar_left + bar_width + 6
    return _RowLayout(
        label_left=label_left,
        label_width=label_width,
        badge_left=badge_left,
        badge_width=badge_width,
        bar_left=bar_left,
        bar_width=bar_width,
        percent_left=percent_left,
        percent_width=percent_width,
    )
_CHART_PADDING = 6


def render_entries_as_text(entries: Sequence[Sequence[object]]) -> str:
    """Plain-text fallback shared by the chart widget and its callers.

    Accepts either ``(label, percentage, observed)`` or the older
    ``(label, percentage)`` shape.
    """

    lines: list[str] = []
    try:
        for entry in entries:
            if len(entry) >= 3:
                label, raw_percentage, observed = entry[0], entry[1], entry[2]
            else:
                label, raw_percentage = entry[0], entry[1]
                observed = False
            percentage: float | None = (
                None if raw_percentage is None else float(str(raw_percentage))
            )
            percent_text = "--%" if percentage is None else f"{percentage:.1f}%"
            observed_text = f" [{_OBSERVED_BADGE_JA}]" if observed else ""
            lines.append(f"{label}: {percent_text}{observed_text}")
    except Exception:
        return "データなし"
    return "\n".join(lines) if lines else "データなし"


def top_ranked_entries(
    entries: Sequence[tuple[str, float | None, bool]],
    limit: int,
) -> list[tuple[str, float | None, bool]]:
    """Sort by percentage descending (unranked/``None`` entries last), top N.

    Nothing upstream of the chart layer guarantees ranked/sorted order, so
    this is the one place visible-limit truncation happens -- callers pass
    the full known set and get back exactly what should be drawn. The tail
    is never lost, only not drawn here; full lists remain available in the
    INTEL detail dialog.
    """

    ranked = sorted(entries, key=lambda entry: (entry[1] is None, -(entry[1] or 0.0)))
    return ranked[:limit]


class BarChartWidget(QWidget):
    """Horizontal usage-percentage bars: label left, bar middle, % right.

    Every row is exactly :data:`_ROW_HEIGHT` tall regardless of the entry
    count, so a 3-row (abilities) and a 5-row (moves/items) instance sit at
    the same bar thickness and never compete for a shared oversized area.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[tuple[str, float | None, bool]] = []
        self._render_error = False

    def set_entries(self, entries: list[tuple[str, float | None, bool]]) -> None:
        self._entries = list(entries)[:_MAX_BAR_ENTRIES]
        self._render_error = False
        if any(is_observed for _, _, is_observed in self._entries):
            self.setToolTip(f"✓ = {_OBSERVED_BADGE_EN} / {_OBSERVED_BADGE_JA}")
        else:
            self.setToolTip("")
        self.updateGeometry()
        self.update()

    def has_render_error(self) -> bool:
        return self._render_error

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        row_count = max(len(self._entries), 1)
        return QSize(240, row_count * _ROW_HEIGHT + _CHART_PADDING)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            self._paint(painter)
        except Exception:
            self._render_error = True
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        if not self._entries:
            painter.setPen(QPen(self.palette().mid().color()))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "データなし")
            return

        row_height = _ROW_HEIGHT
        max_percentage = max(
            (entry[1] for entry in self._entries if entry[1] is not None),
            default=0.0,
        )
        max_percentage = max(max_percentage, 1.0)
        # Four non-overlapping reserved regions, left to right: label,
        # observed-badge, bar, percentage. Each has its own fixed space --
        # the badge never shares the label's rect, so a long move/item name
        # can never paint through a "✓" glyph or vice versa.
        row_layout = _row_layout(rect.left(), rect.width())
        label_width = row_layout.label_width
        badge_left = row_layout.badge_left
        badge_width = row_layout.badge_width
        bar_left = row_layout.bar_left
        bar_max_width = row_layout.bar_width
        percent_left = row_layout.percent_left
        percent_width = row_layout.percent_width

        text_color = self.palette().text().color()
        bar_color = self.palette().highlight().color()
        observed_color = QColor(self.palette().link().color())
        observed_border = self.palette().text().color()
        metrics = painter.fontMetrics()

        for row, (label, percentage, is_observed) in enumerate(self._entries):
            top = rect.top() + row * row_height
            label_rect = QRectF(rect.left(), top, label_width, row_height)
            painter.setPen(QPen(text_color))
            elided_label = metrics.elidedText(
                str(label), Qt.TextElideMode.ElideRight, int(label_width)
            )
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                elided_label,
            )
            if is_observed:
                painter.setPen(QPen(observed_color))
                badge_rect = QRectF(badge_left, top, badge_width, row_height)
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, "✓")

            if percentage is None:
                bar_width = 0.0
                percent_text = "--%"
            else:
                ratio = max(0.0, min(1.0, float(percentage) / max_percentage))
                bar_width = ratio * bar_max_width
                percent_text = f"{float(percentage):.1f}%"

            bar_rect = QRectF(bar_left, top + row_height * 0.25, bar_width, row_height * 0.5)
            if is_observed:
                painter.setBrush(observed_color)
                painter.setPen(QPen(observed_border, 1.5))
            else:
                painter.setBrush(bar_color)
                painter.setPen(Qt.PenStyle.NoPen)
            if percentage is None:
                pen = QPen(text_color, 1, Qt.PenStyle.DashLine)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(pen)
                dashed_rect = QRectF(bar_left, top + row_height * 0.25, 30, row_height * 0.5)
                painter.drawRect(dashed_rect)
            else:
                painter.drawRect(bar_rect)

            # Percentages sit in one fixed-width, right-aligned column so
            # every row's number lines up regardless of that row's own bar
            # length -- never a value that floats immediately after the bar.
            painter.setPen(QPen(text_color))
            percent_rect = QRectF(percent_left, top, percent_width, row_height)
            painter.drawText(
                percent_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                percent_text,
            )


_THIN_BAR_HEIGHT = 4
_PERCENT_COLUMN_WIDTH = 60


class _ThinUsageBar(QWidget):
    """A slim proportional bar with no text -- structurally cannot overlap
    a label or percentage, since those live in sibling widgets, never
    painted over this one."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ratio = 0.0
        self._is_observed = False
        self._has_value = False
        self.setFixedHeight(_THIN_BAR_HEIGHT)

    def set_value(self, ratio: float | None, *, is_observed: bool) -> None:
        self._has_value = ratio is not None
        self._ratio = 0.0 if ratio is None else max(0.0, min(1.0, ratio))
        self._is_observed = is_observed
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            rect = self.rect()
            painter.setPen(Qt.PenStyle.NoPen)
            track_color = QColor(self.palette().mid().color())
            track_color.setAlpha(90)
            painter.setBrush(track_color)
            painter.drawRect(rect)
            if not self._has_value:
                return
            fill_color = (
                QColor(self.palette().link().color())
                if self._is_observed
                else self.palette().highlight().color()
            )
            painter.setBrush(fill_color)
            fill_width = rect.width() * self._ratio
            painter.drawRect(QRectF(rect.left(), rect.top(), fill_width, rect.height()))
        finally:
            painter.end()


class ReadableRankedListWidget(QWidget):
    """One category's ranked entries as full-height, never-elided rows.

    Each row is: full name (word-wrap allowed, never truncated) and
    percentage on one line, with a thin auxiliary usage bar underneath --
    the bar is a separate sibling widget below the text, so it can never be
    painted over any character. Rows stack vertically, so this widget's own
    width never limits how much of a name is readable; only wrapping does.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._empty_label: QLabel | None = None

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._empty_label = None

    def set_entries(self, entries: Sequence[tuple[str, float | None, bool]]) -> None:
        self._clear()
        if not entries:
            label = QLabel("データなし")
            label.setProperty("muted", True)
            self._layout.addWidget(label)
            self._empty_label = label
            return
        max_percentage = max(
            (percentage for _, percentage, _ in entries if percentage is not None),
            default=0.0,
        )
        max_percentage = max(max_percentage, 1.0)
        for label_text, percentage, is_observed in entries:
            self._layout.addWidget(
                self._build_row(label_text, percentage, is_observed, max_percentage)
            )

    def _build_row(
        self,
        label_text: str,
        percentage: float | None,
        is_observed: bool,
        max_percentage: float,
    ) -> QWidget:
        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(2)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        name_text = f"✓ {label_text}" if is_observed else label_text
        name_label = QLabel(name_text)
        name_label.setWordWrap(True)
        if is_observed:
            name_label.setProperty("factChip", True)
        top_row.addWidget(name_label, 1)

        percent_label = QLabel("--%" if percentage is None else f"{percentage:.1f}%")
        percent_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        percent_label.setMinimumWidth(_PERCENT_COLUMN_WIDTH)
        top_row.addWidget(percent_label, 0)
        row_layout.addLayout(top_row)

        bar = _ThinUsageBar()
        ratio = None if percentage is None else float(percentage) / max_percentage
        bar.set_value(ratio, is_observed=is_observed)
        row_layout.addWidget(bar)

        return row
