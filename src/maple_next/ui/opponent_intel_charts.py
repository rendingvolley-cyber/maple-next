"""Hand-drawn QPainter chart widgets for the Opponent INTEL v2 panel.

No QtCharts, matplotlib, or pyqtgraph dependency -- everything here is a
plain :class:`QWidget` subclass that paints itself. Both widgets are
fail-soft: a rendering exception is caught inside ``paintEvent`` and never
propagates, so a chart bug can never crash the surrounding Battle Record UI.
Callers should check :meth:`has_render_error` and fall back to
:func:`render_entries_as_text` when it is ``True``.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

_MAX_BAR_ENTRIES = 8
_OBSERVED_BADGE_EN = "OBSERVED"
_OBSERVED_BADGE_JA = "確認済み"


def render_entries_as_text(entries: Sequence[Sequence[object]]) -> str:
    """Plain-text fallback shared by both chart widgets and their callers.

    Accepts either the bar-chart entry shape ``(label, percentage, observed)``
    or the donut-chart entry shape ``(label, percentage)``.
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


class BarChartWidget(QWidget):
    """Horizontal usage-percentage bars, up to 8 rows."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[tuple[str, float | None, bool]] = []
        self._render_error = False

    def set_entries(self, entries: list[tuple[str, float | None, bool]]) -> None:
        self._entries = list(entries)[:_MAX_BAR_ENTRIES]
        self._render_error = False
        self.update()

    def has_render_error(self) -> bool:
        return self._render_error

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(260, 140)

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

        row_height = max(rect.height() // max(len(self._entries), 1), 14)
        max_percentage = max(
            (entry[1] for entry in self._entries if entry[1] is not None),
            default=0.0,
        )
        max_percentage = max(max_percentage, 1.0)
        label_width = min(rect.width() // 3, 110)
        bar_left = rect.left() + label_width + 6
        bar_max_width = max(rect.width() - label_width - 70, 20)

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

            painter.setPen(QPen(text_color))
            text_x = bar_left + max(bar_width, 32) + 4
            percent_rect = QRectF(text_x, top, 60, row_height)
            painter.drawText(
                percent_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
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


class DonutChartWidget(QWidget):
    """A donut chart of proportional usage slices with a center caption."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[tuple[str, float | None]] = []
        self._center_text = ""
        self._render_error = False

    def set_entries(self, entries: list[tuple[str, float | None]]) -> None:
        self._entries = list(entries)
        self._render_error = False
        self.update()

    def set_center_text(self, text: str) -> None:
        self._center_text = text
        self.update()

    def has_render_error(self) -> bool:
        return self._render_error

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(160, 160)

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
        side = max(min(rect.width(), rect.height()) - 10, 10)
        donut_rect = QRectF(
            rect.left() + (rect.width() - side) / 2,
            rect.top() + (rect.height() - side) / 2,
            side,
            side,
        )

        if not self._entries:
            painter.setPen(QPen(self.palette().mid().color(), max(side * 0.12, 4)))
            painter.drawArc(donut_rect, 0, 360 * 16)
            self._draw_center_text(painter, rect)
            return

        known_total = sum(
            float(percentage) for _, percentage in self._entries if percentage is not None
        )
        total = known_total if known_total > 0 else max(len(self._entries), 1)

        colors = [
            self.palette().highlight().color(),
            self.palette().link().color(),
            self.palette().mid().color(),
            self.palette().dark().color(),
            self.palette().shadow().color(),
        ]

        start_angle = 90 * 16
        pen_width = max(side * 0.18, 6)
        for index, (_, percentage) in enumerate(self._entries):
            if percentage is None:
                # Unranked/unknown slice: an even dashed share of whatever
                # room is left, drawn so arithmetic never divides by zero.
                span = int((1.0 / max(len(self._entries), 1)) * 360 * 16)
                pen = QPen(self.palette().mid().color(), pen_width, Qt.PenStyle.DashLine)
            else:
                fraction = float(percentage) / total if total else 0.0
                span = int(fraction * 360 * 16)
                pen = QPen(colors[index % len(colors)], pen_width)
            painter.setPen(pen)
            painter.drawArc(donut_rect, -start_angle, -span)
            start_angle += span

        self._draw_center_text(painter, rect)

    def _draw_center_text(self, painter: QPainter, rect: QRect) -> None:
        if not self._center_text:
            return
        painter.setPen(QPen(self.palette().text().color()))
        alignment = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
        painter.drawText(rect, alignment, self._center_text)
