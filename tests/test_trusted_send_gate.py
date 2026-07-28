from __future__ import annotations

import os
from typing import cast

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from maple_next.ui.trusted_input import (
    TrustedSendButton,
    is_trusted_key_activation,
    is_trusted_mouse_release_activation,
)


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


class _FakeMouseEvent:
    """Minimal stand-in satisfying the same duck-typed gate interface.

    A genuine ``spontaneous()`` True mouse event cannot be constructed from
    Python: Qt only sets that flag internally when the platform event
    dispatcher delivers a real OS input event. This harness runs the exact
    same production judgment function (``is_trusted_mouse_release_activation``)
    against a stand-in that can set the flag either way, proving the gate
    logic itself accepts trusted input and rejects everything else.
    """

    def __init__(self, *, spontaneous: bool, button: Qt.MouseButton) -> None:
        self._spontaneous = spontaneous
        self._button = button

    def spontaneous(self) -> bool:
        return self._spontaneous

    def button(self) -> Qt.MouseButton:
        return self._button


class _FakeKeyEvent:
    def __init__(self, *, spontaneous: bool, key: int, auto_repeat: bool = False) -> None:
        self._spontaneous = spontaneous
        self._key = key
        self._auto_repeat = auto_repeat

    def spontaneous(self) -> bool:
        return self._spontaneous

    def key(self) -> int:
        return self._key

    def isAutoRepeat(self) -> bool:
        return self._auto_repeat


# --- Pure gate function tests -------------------------------------------------


def test_trusted_mouse_release_activation_true_for_genuine_left_click() -> None:
    event = _FakeMouseEvent(spontaneous=True, button=Qt.MouseButton.LeftButton)
    assert is_trusted_mouse_release_activation(
        event, was_pressed_inside=True, released_inside=True
    )


def test_trusted_mouse_release_activation_false_when_not_spontaneous() -> None:
    """Models QTest.mouseClick / postEvent, which never set spontaneous=True."""

    event = _FakeMouseEvent(spontaneous=False, button=Qt.MouseButton.LeftButton)
    assert not is_trusted_mouse_release_activation(
        event, was_pressed_inside=True, released_inside=True
    )


def test_trusted_mouse_release_activation_false_when_not_pressed_inside_first() -> None:
    event = _FakeMouseEvent(spontaneous=True, button=Qt.MouseButton.LeftButton)
    assert not is_trusted_mouse_release_activation(
        event, was_pressed_inside=False, released_inside=True
    )


def test_trusted_mouse_release_activation_false_when_released_outside() -> None:
    event = _FakeMouseEvent(spontaneous=True, button=Qt.MouseButton.LeftButton)
    assert not is_trusted_mouse_release_activation(
        event, was_pressed_inside=True, released_inside=False
    )


def test_trusted_mouse_release_activation_false_for_right_button() -> None:
    event = _FakeMouseEvent(spontaneous=True, button=Qt.MouseButton.RightButton)
    assert not is_trusted_mouse_release_activation(
        event, was_pressed_inside=True, released_inside=True
    )


@pytest.mark.parametrize(
    "key", [Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter]
)
def test_trusted_key_activation_true_for_genuine_space_or_enter(key: Qt.Key) -> None:
    event = _FakeKeyEvent(spontaneous=True, key=key)
    assert is_trusted_key_activation(event)


def test_trusted_key_activation_false_when_not_spontaneous() -> None:
    event = _FakeKeyEvent(spontaneous=False, key=Qt.Key.Key_Space)
    assert not is_trusted_key_activation(event)


def test_trusted_key_activation_false_for_auto_repeat() -> None:
    event = _FakeKeyEvent(spontaneous=True, key=Qt.Key.Key_Space, auto_repeat=True)
    assert not is_trusted_key_activation(event)


def test_trusted_key_activation_false_for_unrelated_key() -> None:
    event = _FakeKeyEvent(spontaneous=True, key=Qt.Key.Key_A)
    assert not is_trusted_key_activation(event)


# --- Widget-level bypass proofs (real TrustedSendButton, real Qt events) -----


def _make_button() -> TrustedSendButton:
    calls: list[None] = []
    button = TrustedSendButton("SEND", lambda: calls.append(None))
    button.activation_calls = calls  # type: ignore[attr-defined]
    button.resize(120, 32)
    return button


def test_programmatic_click_yields_zero_sends() -> None:
    qt_application()
    button = _make_button()
    button.click()
    assert button.activation_calls == []  # type: ignore[attr-defined]


def test_direct_clicked_signal_emit_yields_zero_sends() -> None:
    qt_application()
    button = _make_button()
    button.clicked.emit()
    assert button.activation_calls == []  # type: ignore[attr-defined]


def test_qtest_mouse_click_is_a_genuine_trusted_activation() -> None:
    """QTest.mouseClick drives real QWindowSystemInterface input injection.

    Qt marks these events ``spontaneous`` exactly like real OS input (verified
    empirically against this PySide6/offscreen build), so this is the
    standard, supported way to simulate one genuine human left-click and
    prove it sends exactly once.
    """

    qt_application()
    button = _make_button()
    button.show()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert button.activation_calls == [None]  # type: ignore[attr-defined]
    assert button.trusted_mouse_activation_count == 1


def test_qtest_key_click_is_a_genuine_trusted_activation() -> None:
    """QTest.keyClick likewise injects a real, spontaneous key event."""

    qt_application()
    button = _make_button()
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    button.show()
    button.setFocus()
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert button.activation_calls == [None]  # type: ignore[attr-defined]
    assert button.trusted_key_activation_count == 1


def test_disabled_button_ignores_a_genuine_trusted_click() -> None:
    qt_application()
    button = _make_button()
    button.show()
    button.setEnabled(False)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert button.activation_calls == []  # type: ignore[attr-defined]


def test_double_click_sends_at_most_once_while_disabled_between_clicks() -> None:
    """Models the pending-disable guard: a second click before re-enable is inert."""

    qt_application()
    button = _make_button()
    button.show()

    def on_activate() -> None:
        button.activation_calls.append(None)  # type: ignore[attr-defined]
        button.setEnabled(False)

    button._on_trusted_activate = on_activate  # type: ignore[attr-defined]
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert button.activation_calls == [None]  # type: ignore[attr-defined]


def test_synthetic_posted_mouse_event_yields_zero_sends() -> None:
    """A manually constructed + posted QMouseEvent is never spontaneous.

    This is the ``postEvent``/``sendEvent`` synthetic-dispatch path called out
    by the Issue, distinct from QTest's windowing-system input injection.
    """

    qapp = qt_application()
    button = _make_button()
    button.show()
    center = QPointF(button.rect().center())
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        center,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        center,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert press.spontaneous() is False
    assert release.spontaneous() is False
    QApplication.sendEvent(button, press)
    QApplication.sendEvent(button, release)
    qapp.processEvents()
    assert button.activation_calls == []  # type: ignore[attr-defined]
