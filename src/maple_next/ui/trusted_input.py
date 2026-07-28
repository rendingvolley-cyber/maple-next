"""OS-trusted mouse/keyboard activation gate for irreversible human sends.

The gate itself is two pure functions operating on any object exposing the
same duck-typed surface as ``QMouseEvent`` / ``QKeyEvent``
(``spontaneous()``, ``button()``, ``key()``, ``isAutoRepeat()``). Qt marks
an event spontaneous only when it originates from the real windowing
system's input queue — events built and dispatched by ``QPushButton.click()``,
a direct ``Signal.emit()``, ``QApplication.postEvent``/``sendEvent``, or
``QTest.mouseClick``/``QTest.keyClick`` are never spontaneous. That is what
makes every programmatic path yield zero sends without any test-only bypass
in production code: the same pure functions run in production and in tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QPushButton

_TRUSTED_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)


class SpontaneousMouseEventLike(Protocol):
    def spontaneous(self) -> bool: ...
    def button(self) -> Qt.MouseButton: ...


class SpontaneousKeyEventLike(Protocol):
    def spontaneous(self) -> bool: ...
    def key(self) -> int: ...
    def isAutoRepeat(self) -> bool: ...


def is_trusted_mouse_release_activation(
    event: SpontaneousMouseEventLike,
    *,
    was_pressed_inside: bool,
    released_inside: bool,
) -> bool:
    """True only for a genuine OS left-click press+release inside the widget."""

    return (
        was_pressed_inside
        and released_inside
        and event.spontaneous()
        and event.button() == Qt.MouseButton.LeftButton
    )


def is_trusted_key_activation(event: SpontaneousKeyEventLike) -> bool:
    """True only for a genuine OS Space/Enter/Return key release, no auto-repeat."""

    return (
        event.spontaneous()
        and not event.isAutoRepeat()
        and event.key() in _TRUSTED_KEYS
    )


class TrustedSendButton(QPushButton):
    """A button whose activation callback fires only for trusted OS input.

    Deliberately does not expose a public "activated" signal: the callback is
    invoked directly from inside the raw event override once the trust check
    passes, so there is no signal for a test (or malicious code) to emit
    directly and bypass the gate. The standard ``clicked`` signal still
    exists (inherited from ``QPushButton``) but nothing production connects
    to it, so ``button.click()`` and ``clicked.emit()`` both remain inert.
    """

    def __init__(
        self,
        text: str,
        on_trusted_activate: Callable[[], None],
        parent: object | None = None,
    ) -> None:
        super().__init__(text, parent)  # type: ignore[arg-type]
        self._on_trusted_activate = on_trusted_activate
        self._mouse_pressed_inside = False
        self.trusted_mouse_activation_count = 0
        self.trusted_key_activation_count = 0

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        self._mouse_pressed_inside = (
            event.spontaneous()
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        released_inside = self.rect().contains(event.position().toPoint())
        was_pressed_inside = self._mouse_pressed_inside
        self._mouse_pressed_inside = False
        if is_trusted_mouse_release_activation(
            event, was_pressed_inside=was_pressed_inside, released_inside=released_inside
        ) and self.isEnabled():
            self.trusted_mouse_activation_count += 1
            self._on_trusted_activate()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        super().keyReleaseEvent(event)
        if is_trusted_key_activation(event) and self.isEnabled():
            self.trusted_key_activation_count += 1
            self._on_trusted_activate()
