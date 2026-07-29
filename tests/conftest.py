from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from maple_next.ui.match_window import MatchFlowWindow  # noqa: E402


@pytest.fixture(autouse=True)
def show_match_flow_window(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Mirror the official entrypoint by showing MatchFlowWindow offscreen."""

    original_init = MatchFlowWindow.__init__

    def initialize_and_show(
        window: MatchFlowWindow,
        *args: object,
        **kwargs: object,
    ) -> None:
        original_init(window, *args, **kwargs)  # type: ignore[arg-type]
        window.show()

    monkeypatch.setattr(MatchFlowWindow, "__init__", initialize_and_show)
    yield


def close_and_delete_top_level_widgets() -> int:
    """Close, deleteLater, and flush every current top-level QWidget.

    Returns the number of widgets that were closed. Shared by the autouse
    cleanup fixture below and by lifecycle regression tests that need to
    assert the sweep actually clears the widget population.
    """

    app = QApplication.instance()
    if app is None:
        return 0
    app.processEvents()
    widgets = app.topLevelWidgets()
    for widget in widgets:
        widget.close()
        widget.deleteLater()
    # A plain processEvents() call does not reliably flush the
    # DeferredDelete queue in the offscreen platform used by this test
    # suite; the deletions must be pumped explicitly or the widgets remain
    # in topLevelWidgets() (and alive) until Qt happens to flush them.
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    return len(widgets)


@pytest.fixture(autouse=True)
def _close_top_level_widgets_after_test() -> Iterator[None]:
    """Force every top-level QWidget a test created to be destroyed before
    the next test runs, instead of surviving until interpreter shutdown.

    The QApplication instance is a session-wide singleton (tests reuse
    ``QApplication.instance()`` rather than each creating/destroying their
    own), so any window a test forgets to explicitly tear down keeps
    accumulating as a parentless top-level widget for the rest of the
    process. When dozens of such widgets pile up across files, CPython's
    shutdown-time garbage collection finalizes them in an order Qt/shiboken
    does not guarantee is safe, which has been observed to corrupt the
    process heap (Windows fatal exception 0xC0000374) with no Python
    traceback. Closing and deleting each test's widgets immediately, with
    the deferred-delete queue flushed before the next test starts, keeps
    that population at zero instead of accumulating.
    """

    yield
    close_and_delete_top_level_widgets()
