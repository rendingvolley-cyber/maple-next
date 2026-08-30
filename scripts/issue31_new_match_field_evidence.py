"""Generate hardware-free 1920x1080 NEW MATCH field-entrypoint evidence."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    SanitizedProviderResult,
)
from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.turn_state_flow import TurnStateFlowController
from scripts.issue31_selection_v3_visual_evidence import (
    OPPONENT_TEAM,
    SELF_TEAM,
    _capture,
    _NoCaptureBackend,
    _SyncDispatch,
)


def main(output_directory: Path) -> int:
    output_directory.mkdir(parents=True, exist_ok=True)
    app = cast(QApplication, QApplication.instance() or QApplication([]))
    repository = SQLiteRepository(output_directory / "new-match-field-evidence.db")
    export_directory = output_directory / "export"
    export_directory.mkdir(exist_ok=True)
    application = MatchApplication(repository, export_directory)
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(SELF_TEAM[:3]), "lead": SELF_TEAM[0]},
                source_type=GEMINI_SOURCE_TYPE,
                model="fixture-new-match-field",
            )
        ]
    )
    adapter = GeminiSelectionAdviceAdapter(
        transport,
        lambda: ProviderConfig(
            api_key="fixture-only",
            model="fixture-new-match-field",
            timeout_seconds=5.0,
        ),
        dispatch_factory=_SyncDispatch,
    )
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        adapter,
    )
    capture_backend = _NoCaptureBackend()
    ocr_directory = output_directory / "ocr"
    ocr_directory.mkdir(exist_ok=True)
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_directory,
        capture_backend=capture_backend,
        auto_start_capture=False,
    )
    window.show()

    if window.header_tabs.currentIndex() != 1:
        raise RuntimeError("standard landing did not open Battle Record")
    _capture(app, window, output_directory / "01-battle-no-active-new-match.png")
    if not window.new_match_button.isVisible() or not window.new_match_button.isEnabled():
        raise RuntimeError("NO_ACTIVE_MATCH NEW MATCH is not field-ready")

    window.new_match_button.click()
    _capture(app, window, output_directory / "02-selection-after-new-match.png")
    if window.header_tabs.currentIndex() != 0 or window.new_match_button.isVisible():
        raise RuntimeError("NEW MATCH did not open Selection without a Selection-side button")

    for field, name in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(name)
    for slot, name in enumerate(OPPONENT_TEAM, start=1):
        window._set_selection_slot_value(  # noqa: SLF001 - explicit fixture input
            slot,
            name,
            origin=SelectionInputOrigin.MANUAL_TEXT,
            user_locked=True,
        )
    window.render_view()
    window._on_send_current_selection_to_gemini()  # noqa: SLF001 - fake transport only
    window.render_view()
    window._on_apply()  # noqa: SLF001 - explicit fixture confirmation
    window.header_tabs.setCurrentIndex(1)
    _capture(app, window, output_directory / "03-battle-v5-active-no-new-match.png")
    window.match_win_button.click()
    _capture(app, window, output_directory / "04-match-end-win-selected.png")
    window.match_loss_button.click()
    _capture(app, window, output_directory / "05-match-end-loss-selected.png")

    if controller.refresh().session_state != "BATTLE_READY":
        raise RuntimeError("fixture did not reach BATTLE_READY")
    if not window.match_loss_button.isChecked() or window.match_win_button.isChecked():
        raise RuntimeError("WIN / LOSS controls are not exclusive")
    if window.end_match_button.isEnabled():
        raise RuntimeError("MATCH END enabled without explicit confirmation")
    if window.new_match_button.isEnabled() or window.new_match_after_export_button.isEnabled():
        raise RuntimeError("active match exposes destructive NEW MATCH")
    if repository.count_sessions() != 1:
        raise RuntimeError("one NEW MATCH click did not create exactly one session")
    if transport.call_count != 1:
        raise RuntimeError("fake Selection transport dispatch count changed")
    if capture_backend.start_count != 0:
        raise RuntimeError("capture backend unexpectedly started")

    print("screenshots=5 size=1920x1080 new_match_clicks=1 sessions=1")
    print("no_active_new_match=visible_enabled selection_new_match=absent")
    print("default_view=battle_record match_end=local win_loss=exclusive")
    print(
        "real_provider_network=0 real_capture=0 automatic_new_match=0 "
        "match_end_send=0 game_action=0"
    )
    window.close()
    repository.close()
    return 0


if __name__ == "__main__":
    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("C:/tmp/maple-issue31-new-match-field-evidence")
    )
    raise SystemExit(main(target))
