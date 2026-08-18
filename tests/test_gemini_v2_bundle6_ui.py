"""Gemini V2 Bundle 6: UI rendering tests for the versioned response contract.

Uses ``MapleMainWindow.render_view(view=...)`` (it accepts an explicit
``OperatorView | None`` -- see ``window.py``) to inject a synthetic view
directly, rather than driving the whole rich-state apply pipeline through a
real Gemini result. This exercises the actual, shared rendering code
(``_format_prediction_line_v2`` and the ``turn_advice_*`` label population
in ``render_view``) that both ``MapleMainWindow`` and ``BattleRecordUiWindow``
rely on for content, without needing the heavier Bundle-C capture/OCR
fixture machinery.

``BattleRecordUiWindow``'s own card-visibility wiring
(``turn_advice_robustness_card`` / ``turn_advice_alternatives_card``) is not
re-exercised by a new fixture here; its safety is established by (a) the
full existing ``test_issue31_battle_record_v5.py`` etc. regression suite
continuing to pass unchanged after this bundle's edits, and (b) direct code
review of the additive visibility-toggle lines alongside the pre-existing
``turn_advice_warning_card`` toggle they mirror.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import cast

from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.turn_response_v2 import (
    OpponentPredictionV2,
    PredictionLineV2,
    RecommendedAction,
    TurnAdviceBodyV2,
)
from maple_next.providers.turn_transport import FakeTurnAdviceTransport
from maple_next.ui.controller import TurnAdviceView
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_turn_advice import GeminiTurnAdviceAdapter
from maple_next.ui.turn_advice_integration import (
    TurnAdviceIntegrationController,
    TurnAdviceIntegrationWindow,
)
from maple_next.ui.window import _format_prediction_line_v2
from tests.test_gemini_turn_advice_window import advance_to_turn_reviewed


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


def _build_window(tmp_path: Path) -> tuple[SQLiteRepository, TurnAdviceIntegrationWindow]:
    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    turn_gemini_adapter = GeminiTurnAdviceAdapter(FakeTurnAdviceTransport())
    controller = TurnAdviceIntegrationController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        gemini_adapter=None,
        turn_gemini_adapter=turn_gemini_adapter,
    )
    window = TurnAdviceIntegrationWindow(controller)
    return repository, window


def _v2_body(
    *, robustness: str = "HIGH", alternatives: tuple[PredictionLineV2, ...] = ()
) -> TurnAdviceBodyV2:
    return TurnAdviceBodyV2(
        response_schema_version="maple-turn-advice-response.v2",
        recommended_action=RecommendedAction(
            action_id="move-1", action_type="MOVE", action_name="Wave Crash"
        ),
        recommendation_robustness=robustness,
        reasons=("確定情報から有利",),
        opponent_prediction=OpponentPredictionV2(
            primary=PredictionLineV2(
                category="DAMAGING_MOVE",
                specific_action=None,
                support_basis="GENERAL_KNOWLEDGE",
                support="LOW",
                summary="相手はダメージ技を選択",
            ),
            alternatives=alternatives,
        ),
        warnings=("相手の交代先が未確定",) if robustness == "LOW" else (),
    )


def _v1_turn_advice_view() -> TurnAdviceView:
    return TurnAdviceView(
        action_type="MOVE",
        action_name="Wave Crash",
        opponent_prediction="Opponent likely attacks",
        rationale="Best expected value",
        is_mock=False,
        source_type="GEMINI",
        model="gemini-2.5-flash",
        warnings=(),
    )


# =========================================================================
# J. UI
# =========================================================================


def test_format_prediction_line_v2_is_compact_and_never_fabricates_action() -> None:
    line = PredictionLineV2(
        category="SWITCH",
        specific_action=None,
        support_basis="GENERAL_KNOWLEDGE",
        support="LOW",
        summary="交代の可能性も残る",
    )
    rendered = _format_prediction_line_v2(line)
    assert "SWITCH" in rendered
    assert "GENERAL_KNOWLEDGE" in rendered
    assert "交代の可能性も残る" in rendered
    assert "—" in rendered  # null specific_action never shown as a fabricated name


def test_legacy_v1_row_renders_without_fabricated_v2_metadata(tmp_path: Path) -> None:
    qt_application()
    repository, window = _build_window(tmp_path)
    window.show()
    advance_to_turn_reviewed(window)

    view = window._controller.refresh()  # noqa: SLF001
    synthetic = dataclasses.replace(view, turn_advice=_v1_turn_advice_view())
    window.render_view(synthetic)

    assert window.turn_advice_robustness_label.text() == "—"
    assert window.turn_advice_alternatives_label.text() == "—"
    assert (
        window.turn_advice_schema_version_label.text() == "maple-turn-advice-response.v1"
    )
    assert window.turn_advice_prediction_label.text() == "Opponent likely attacks"

    window.close()
    repository.close()


def test_v2_row_with_zero_alternatives_renders_primary_only(tmp_path: Path) -> None:
    qt_application()
    repository, window = _build_window(tmp_path)
    window.show()
    advance_to_turn_reviewed(window)

    view = window._controller.refresh()  # noqa: SLF001
    v2_view = dataclasses.replace(
        _v1_turn_advice_view(),
        response_schema_version="maple-turn-advice-response.v2",
        structured_v2=_v2_body(),
    )
    window.render_view(dataclasses.replace(view, turn_advice=v2_view))

    assert window.turn_advice_robustness_label.text() == "HIGH"
    assert window.turn_advice_alternatives_label.text() == "—"
    assert "相手はダメージ技を選択" in window.turn_advice_prediction_label.text()
    assert window.turn_advice_schema_version_label.text() == "maple-turn-advice-response.v2"

    window.close()
    repository.close()


def test_v2_row_with_two_alternatives_renders_both(tmp_path: Path) -> None:
    qt_application()
    repository, window = _build_window(tmp_path)
    window.show()
    advance_to_turn_reviewed(window)

    alt1 = PredictionLineV2(
        category="SWITCH",
        specific_action=None,
        support_basis="GENERAL_KNOWLEDGE",
        support="LOW",
        summary="交代の可能性A",
    )
    alt2 = PredictionLineV2(
        category="NON_DAMAGING_MOVE",
        specific_action=None,
        support_basis="GENERAL_KNOWLEDGE",
        support="LOW",
        summary="補助技の可能性B",
    )
    body = _v2_body(alternatives=(alt1, alt2))
    view = window._controller.refresh()  # noqa: SLF001
    v2_view = dataclasses.replace(
        _v1_turn_advice_view(),
        response_schema_version="maple-turn-advice-response.v2",
        structured_v2=body,
    )
    window.render_view(dataclasses.replace(view, turn_advice=v2_view))

    alternatives_text = window.turn_advice_alternatives_label.text()
    assert "交代の可能性A" in alternatives_text
    assert "補助技の可能性B" in alternatives_text

    window.close()
    repository.close()


def test_low_robustness_row_still_shows_warning(tmp_path: Path) -> None:
    qt_application()
    repository, window = _build_window(tmp_path)
    window.show()
    advance_to_turn_reviewed(window)

    body = _v2_body(robustness="LOW")
    view = window._controller.refresh()  # noqa: SLF001
    v2_view = dataclasses.replace(
        _v1_turn_advice_view(),
        response_schema_version="maple-turn-advice-response.v2",
        structured_v2=body,
        warnings=body.warnings,
    )
    window.render_view(dataclasses.replace(view, turn_advice=v2_view))

    assert window.turn_advice_robustness_label.text() == "LOW"
    assert window.turn_advice_warnings_label.text() == "相手の交代先が未確定"

    window.close()
    repository.close()
