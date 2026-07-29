"""Regression tests for the Windows PySide6 full-suite native crash.

Issue #19 root cause: ``tests/test_gemini_selection_advice_window.py::
test_gemini_send_button_disabled_while_pending`` started the real
``QThread``-based ``SelectionAdviceDispatch`` and returned without waiting
for it to finish. ``SelectionAdviceDispatch._shutdown`` only calls
``QThread.quit()``/``QThread.wait()`` from inside the queued result-signal
handler, so a test that never drains that signal can let its Python
objects (and the whole ``QApplication`` singleton's accumulated top-level
widgets) be torn down while the worker OS thread is still running or while
Qt/shiboken finalize objects in an unsafe order. Both conditions produced a
native, untraceable process exit (Windows fatal exception / exit code 127)
that only appeared once enough of the full suite had run -- never in
isolation.

These tests assert the two invariants whose violation caused that crash.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import cast

from conftest import close_and_delete_top_level_widgets
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from maple_next.application.service import BattleApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import FakeSelectionAdviceTransport, ProviderConfig
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.window import MapleMainWindow
from maple_next.workers.selection_advice_worker import SelectionAdviceDispatch

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def pump_until(qapp: QApplication, predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, "timed out waiting for async Gemini result"
        qapp.processEvents()
        time.sleep(0.005)


def test_qapplication_instance_is_a_singleton_never_recreated(tmp_path: Path) -> None:
    """No test may construct a second, competing QApplication.

    Full-suite crashes are also a documented symptom of duplicate
    QApplication construction; every test-owned helper must reuse
    ``QApplication.instance()`` once one already exists.
    """

    first = qt_application()
    second = qt_application()
    assert first is second
    assert QApplication.instance() is first


def test_top_level_widgets_do_not_survive_across_tests() -> None:
    """A window a test forgets to close must not accumulate for later tests.

    This reproduces the accumulation pattern (many parentless top-level
    widgets alive at once) that made interpreter-shutdown garbage
    collection corrupt the process heap. The autouse cleanup fixture in
    conftest.py runs this same sweep after every test; this test asserts
    the sweep itself actually empties the widget population instead of
    merely hiding it.
    """

    qapp = qt_application()
    assert qapp.topLevelWidgets() == []

    leaked = QWidget()
    leaked.show()
    assert leaked in qapp.topLevelWidgets()

    closed_count = close_and_delete_top_level_widgets()

    assert closed_count >= 1
    assert qapp.topLevelWidgets() == []


def test_async_gemini_worker_thread_is_stopped_before_test_returns(
    tmp_path: Path,
) -> None:
    """The exact defect from Issue #19: draining to the post-dispatch state
    must leave the worker QThread fully stopped, never merely "probably
    finished by now". A QThread destroyed while ``isRunning()`` is still
    true is a fatal, untraceable native crash in PySide6, not a Python
    exception -- so this must be checked explicitly rather than inferred
    from a passing assertion elsewhere.
    """

    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()  # no responses queued -> fails fast
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    gemini_adapter = GeminiSelectionAdviceAdapter(
        transport, lambda: ProviderConfig(api_key="test-key", model="test-model")
    )
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        gemini_adapter=gemini_adapter,
    )
    window = MapleMainWindow(controller)
    window.show()

    window.new_match_button.click()
    window.render_view()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
    window.render_view()

    QTest.mouseClick(window.gemini_send_button, Qt.MouseButton.LeftButton)
    assert window.gemini_send_button.isEnabled() is False

    pump_until(qapp, lambda: window.gemini_send_button.isEnabled() is True)

    dispatch = gemini_adapter._active_dispatch  # noqa: SLF001 - white-box lifecycle check
    assert isinstance(dispatch, SelectionAdviceDispatch)
    assert dispatch._worker_thread.isRunning() is False  # noqa: SLF001
    assert dispatch._worker_thread.isFinished() is True  # noqa: SLF001

    window.close()
    repository.close()


def test_gemini_window_test_order_that_previously_crashed_the_process(
    tmp_path: Path,
) -> None:
    """Minimal reproduction subset from the pre-fix investigation.

    Running ``test_gemini_selection_advice_window.py`` (which starts the
    real QThread-based dispatch) followed by any test that creates further
    top-level widgets deterministically hit the native crash before this
    fix, both from the un-drained worker thread and from unbounded
    top-level widget accumulation. Exercising that same shape in a single
    test -- send-to-Gemini without waiting for completion, immediately
    followed by constructing another window -- keeps that failure mode
    under regression without depending on cross-file pytest collection
    order.
    """

    qapp = qt_application()
    transport = FakeSelectionAdviceTransport()
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    gemini_adapter = GeminiSelectionAdviceAdapter(
        transport, lambda: ProviderConfig(api_key="test-key", model="test-model")
    )
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        gemini_adapter=gemini_adapter,
    )
    window = MapleMainWindow(controller)
    window.show()
    window.new_match_button.click()
    window.render_view()
    for field, value in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(value)
    for field, value in zip(window.opponent_team_inputs, OPPONENT_TEAM, strict=True):
        field.setText(value)
    window.confirm_facts_button.click()
    window.render_view()

    QTest.mouseClick(window.gemini_send_button, Qt.MouseButton.LeftButton)
    pump_until(qapp, lambda: window.gemini_send_button.isEnabled() is True)
    window.close()
    repository.close()

    other_repository = SQLiteRepository(tmp_path / "maple2.db")
    other_application = BattleApplication(other_repository)
    other_controller = SelectionFlowController(
        other_application,
        other_repository,
        MockSelectionAdviceAdapter(),
    )
    other_window = MapleMainWindow(other_controller)
    other_window.show()
    qapp.processEvents()
    other_window.close()
    other_repository.close()
