"""A human-controlled move-name autocomplete popup for a target ``QLineEdit``.

Typing never mutates the target field's text -- the popup only shows ranked
candidates below the field. A value only ever lands in the field through an
explicit human action: a mouse click on a row, or Enter/Tab while a row is
highlighted. Escape (or losing focus) closes the popup without committing
anything.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from maple_next.domain.move_catalog import MoveCandidate, MoveMatcher

_MatcherSource = MoveMatcher | Callable[[], MoveMatcher]


def _empty_matcher() -> MoveMatcher:
    return MoveMatcher([])


class MoveAutocompletePopup(QWidget):
    """Frameless popup listing ranked move candidates under ``target``."""

    committed = Signal(str)

    def __init__(
        self,
        target: QLineEdit,
        matcher: _MatcherSource | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._target = target
        self._matcher_source: _MatcherSource = matcher if matcher is not None else _empty_matcher
        self._boosts: dict[str, float] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget(self)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self._list)
        self._list.itemClicked.connect(self._commit_item)

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        target.textChanged.connect(self._on_text_changed)
        target.installEventFilter(self)

    # -- boosts ----------------------------------------------------------

    def set_boosts(self, boosts: dict[str, float]) -> None:
        self._boosts = dict(boosts)

    def _matcher(self) -> MoveMatcher:
        try:
            source = self._matcher_source
            return source() if callable(source) else source
        except Exception:
            return _empty_matcher()

    # -- typing: rank and show, never mutate the field --------------------

    def _on_text_changed(self, text: str) -> None:
        try:
            candidates = self._matcher().rank(text, boosts=self._boosts)
        except Exception:
            candidates = []
        if not candidates:
            self.hide()
            return
        self._populate(candidates)
        self._show_under_target()

    def _populate(self, candidates: list[MoveCandidate]) -> None:
        self._list.clear()
        for candidate in candidates:
            item = QListWidgetItem(candidate.canonical_name)
            item.setData(Qt.ItemDataRole.UserRole, candidate.canonical_name)
            self._list.addItem(item)
        self._list.setCurrentRow(-1)

    def _show_under_target(self) -> None:
        point = self._target.mapToGlobal(self._target.rect().bottomLeft())
        width = max(self._target.width(), 160)
        self.setGeometry(point.x(), point.y(), width, min(28 * self._list.count() + 4, 220))
        if not self.isVisible():
            self.show()

    # -- commit paths -------------------------------------------------------

    def _commit_item(self, item: QListWidgetItem) -> None:
        name = str(item.data(Qt.ItemDataRole.UserRole) or item.text())
        self._commit(name)

    def _commit(self, name: str) -> None:
        self._target.setText(name)
        self.hide()
        self.committed.emit(name)

    # -- keyboard navigation on the target field -----------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self._target and event.type() == QEvent.Type.KeyPress:
            key_event = event
            if isinstance(key_event, QKeyEvent):
                return self._handle_key(key_event)
        return super().eventFilter(watched, event)

    def _handle_key(self, event: QKeyEvent) -> bool:
        if not self.isVisible():
            return False
        key = event.key()
        if key == Qt.Key.Key_Down:
            self._move_highlight(1)
            return True
        if key == Qt.Key.Key_Up:
            self._move_highlight(-1)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            current = self._list.currentItem()
            if current is not None:
                self._commit_item(current)
                return key != Qt.Key.Key_Tab
            self.hide()
            return False
        if key == Qt.Key.Key_Escape:
            self.hide()
            return True
        return False

    def _move_highlight(self, delta: int) -> None:
        count = self._list.count()
        if count == 0:
            return
        current = self._list.currentRow()
        new_row = (current + delta) % count if current >= 0 else (0 if delta > 0 else count - 1)
        self._list.setCurrentRow(new_row)
