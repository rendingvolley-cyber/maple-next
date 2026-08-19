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

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

_MAX_BAR_ENTRIES = 8
_OBSERVED_BADGE_EN = "OBSERVED"
_OBSERVED_BADGE_JA = "確認済み"
#: Uniform row thickness in pixels, shared by every BarChartWidget instance
#: regardless of how many rows it holds -- moves/abilities/items must read
#: as one consistent system, not three independently-scaled charts.
_ROW_HEIGHT = 20
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
        label_width = min(rect.width() // 3, 100)
        percent_width = 52
        bar_left = rect.left() + label_width + 6
        bar_max_width = max(rect.width() - label_width - percent_width - 12, 20)
        percent_left = bar_left + bar_max_width + 6

        text_color = self.palette().text().color()
        bar_color = self.palette().highlight().color()
        observed_color = QColor(self.palette().link().color())
        observed_border = self.palette().text().color()

        for row, (label, percentage, is_observed) in enumerate(self._entries):
            top = rect.top() + row * row_height
            label_rect = QRectF(rect.left(), top, label_width, row_height)
            painter.setPen(QPen(text_color))
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                str(label),
            )

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
            if is_observed:
                painter.setPen(QPen(observed_color))
                badge_rect = QRectF(rect.left(), top, label_width, row_height)
                painter.drawText(
                    badge_rect,
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                    f"✓ {_OBSERVED_BADGE_EN}/{_OBSERVED_BADGE_JA}",
                )
