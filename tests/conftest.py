from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

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
