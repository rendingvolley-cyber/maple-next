"""Create six 1920x1080 Qt captures and HTML-reference comparisons.

Uses only the fake/injected Turn provider and writes outside the repository
by default. No capture device, network provider, or game input is touched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.domain.effect_catalog import find_effect
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import ProviderTransportError, SanitizedProviderResult
from maple_next.providers.turn_transport import (
    FAKE_TURN_ADVICE_SOURCE_TYPE,
    FakeTurnAdviceTransport,
)
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_turn_advice import FAKE_TURN_MODEL
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Salamence", "Gholdengo", "Dragonite", "Flutter Mane", "Tyranitar", "Pelipper")
SELECTED_THREE = SELF_TEAM[:3]


class _SyncDispatch:
    def __init__(self, transport, request, config, *, on_succeeded, on_failed) -> None:
        self._transport = transport
        self._request = request
        self._config = config
        self._on_succeeded = on_succeeded
        self._on_failed = on_failed

    def start(self) -> None:
        try:
            result = self._transport.send(self._request, self._config)
        except ProviderTransportError as exc:
            self._on_failed(str(exc))
        else:
            self._on_succeeded(result)


def _save(app: QApplication, widget, path: Path) -> None:
    app.processEvents()
    if not widget.grab().save(str(path), "PNG"):
        raise RuntimeError(f"failed to save {path}")


def _prepare_live_baseline(window) -> None:
    window._preview_timer.stop()  # noqa: SLF001 - deterministic disconnected evidence
    window._clear_capture_preview(placeholder="16:9 LIVE PREVIEW")  # noqa: SLF001
    window.capture_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.capture_preview_label.setStyleSheet(
        "background: #07131f; color: #314b64; font-size: 18px; letter-spacing: 3px;"
    )


def _save_modal_composite(
    app: QApplication, window, dialog, path: Path
) -> None:
    """Capture a native top-level dialog as a full-window modal scene."""
    app.processEvents()
    canvas = window.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
    painter = QPainter(canvas)
    painter.fillRect(canvas.rect(), QColor(0, 0, 0, 205))
    dialog_image = dialog.grab().toImage()
    x = (canvas.width() - dialog_image.width()) // 2
    y = (canvas.height() - dialog_image.height()) // 2
    painter.drawImage(x, y, dialog_image)
    painter.end()
    if not canvas.save(str(path), "PNG"):
        raise RuntimeError(f"failed to save {path}")


def _write_comparisons(
    output_directory: Path, reference_directory: Path
) -> None:
    pairs = (
        (
            "01-turn-capture-reference.png",
            "01-turn-capture-qt.png",
            "01-turn-capture-side-by-side.png",
        ),
        (
            "02-turn-review-reference.png",
            "02-turn-review-qt.png",
            "02-turn-review-side-by-side.png",
        ),
        (
            "03-effect-candidate-reference.png",
            "03-effect-candidate-qt.png",
            "03-effect-candidate-side-by-side.png",
        ),
        (
            "04-action-result-reference.png",
            "04-action-result-qt.png",
            "04-action-result-side-by-side.png",
        ),
        (
            "05-compact-intel-reference.png",
            "05-compact-intel-qt.png",
            "05-compact-intel-side-by-side.png",
        ),
        (
            "06-intel-detail-reference.png",
            "06-intel-detail-qt.png",
            "06-intel-detail-side-by-side.png",
        ),
    )
    comparison_directory = output_directory / "side-by-side"
    comparison_directory.mkdir(exist_ok=True)
    for reference_name, qt_name, output_name in pairs:
        reference = QImage(str(reference_directory / reference_name))
        qt_capture = QImage(str(output_directory / qt_name))
        if reference.size() != qt_capture.size() or reference.size().width() != 1920:
            raise RuntimeError(
                f"comparison inputs must both be 1920x1080: {reference_name}, {qt_name}"
            )
        canvas = QImage(3840, 1080, QImage.Format.Format_RGB32)
        painter = QPainter(canvas)
        painter.drawImage(0, 0, reference)
        painter.drawImage(1920, 0, qt_capture)
        painter.end()
        if not canvas.save(str(comparison_directory / output_name), "PNG"):
            raise RuntimeError(f"failed to save {output_name}")


def main(output_directory: Path, reference_directory: Path) -> int:
    output_directory.mkdir(parents=True, exist_ok=True)
    app = cast(QApplication, QApplication.instance() or QApplication([]))
    repository = SQLiteRepository(output_directory / "visual-evidence.db")
    export_directory = output_directory / "export"
    export_directory.mkdir(exist_ok=True)
    application = MatchApplication(repository, export_directory)
    transport = FakeTurnAdviceTransport()
    adapter = GeminiRichTurnAdviceAdapter(transport, dispatch_factory=_SyncDispatch)
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        None,
        None,
        adapter,
    )
    ocr_directory = output_directory / "ocr"
    ocr_directory.mkdir(exist_ok=True)
    window = BattleRecordUiWindow(controller, ocr_data_directory=ocr_directory)
    controller.new_match()
    controller.confirm_selection_facts(list(SELF_TEAM), list(OPPONENT_TEAM))
    controller.submit_mock_advice(list(SELECTED_THREE), SELECTED_THREE[0])
    controller.apply_selection(list(SELECTED_THREE), SELECTED_THREE[0], human_confirmed=True)
    window.render_view()
    window.header_tabs.setCurrentIndex(1)
    window.show()
    _prepare_live_baseline(window)
    _save(app, window, output_directory / "01-turn-capture-qt.png")

    controller.start_turn_capture()
    window.render_view()
    _prepare_live_baseline(window)
    window.self_active_box.setCurrentText(SELECTED_THREE[0])
    window.opponent_active_input.setText("Garchomp")
    window.self_hp_box.setCurrentText("100")
    window.opponent_hp_box.setCurrentText("100")
    window.move_inputs[0].setText("Flower Trick")
    window.move_inputs[1].setText("Knock Off")
    window.switch_checkboxes[1].setChecked(True)
    for editor in (window.self_state_editor, window.opponent_state_editor):
        editor.status_field.unknown_box.setChecked(False)
        editor.status_field.line.setText("NONE")
    window.weather_field.unknown_box.setChecked(False)
    window.weather_field.line.setText("NONE")
    window.terrain_field.unknown_box.setChecked(False)
    window.terrain_field.line.setText("NONE")

    window.opponent_active_input.setText("Salamence")
    _prepare_live_baseline(window)
    _save(app, window, output_directory / "02-turn-review-qt.png")
    _save(app, window, output_directory / "05-compact-intel-qt.png")
    window.opponent_intel_widget.detail_button.click()
    detail = window.opponent_intel_widget._detail_dialog  # noqa: SLF001
    if detail is None:
        raise RuntimeError("INTEL detail dialog did not open")
    _prepare_live_baseline(window)
    _save_modal_composite(
        app, window, detail, output_directory / "06-intel-detail-qt.png"
    )
    detail.close()
    intimidate = find_effect("intimidate")
    if intimidate is None:
        raise RuntimeError("catalog missing intimidate")
    window.review_effect_candidate.propose(intimidate, prefix="相手のいかく")
    _prepare_live_baseline(window)
    _save(app, window, output_directory / "03-effect-candidate-qt.png")

    window._on_confirm_turn_facts()  # noqa: SLF001 - human SEND click simulation
    summary = controller.turn_state_summary()
    legal_move = next(
        selection
        for selection in summary.confirmed_legal_actions
        if selection.action_name == "Flower Trick"
    )
    transport.responses.append(
        SanitizedProviderResult(
            payload={
                "recommended_action": {
                    "action_id": legal_move.confirmation_id,
                    "action_type": "MOVE",
                    "action_name": "Flower Trick",
                },
                "reasons": ["fake/injected visual evidence"],
                "warnings": [],
                "opponent_prediction": {
                    "category": "MOVE",
                    "predicted_action": "Dragon Dance",
                    "summary": "setup candidate",
                    "confidence": 0.5,
                },
            },
            source_type=FAKE_TURN_ADVICE_SOURCE_TYPE,
            model=FAKE_TURN_MODEL,
        )
    )
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("Dragon Dance")
    window.mock_turn_rationale_input.setText("fake/injected visual evidence")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    window.opponent_action_type_box.setCurrentText("MOVE")
    window.opponent_action_name_input.setText("りゅうのまい")
    _prepare_live_baseline(window)
    _save(app, window, output_directory / "04-action-result-qt.png")

    _write_comparisons(output_directory, reference_directory)

    print(f"qt_screenshots=6 comparisons=6 fake_provider_dispatch={transport.call_count}")
    print("qt_capture=1920x1080 side_by_side=3840x1080 reference_left=1")
    print("real_provider_network_send=0 game_action=0 meta_runtime_network_fetch=0")
    window.close()
    repository.close()
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/tmp/maple-issue31-v5-evidence")
    references = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.home() / "Downloads"
    raise SystemExit(main(target, references))
