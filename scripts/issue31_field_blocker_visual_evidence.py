"""Create 1920x1080, hardware-free field-blocker remediation evidence."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.capture.contracts import DeviceOpenResult, SourceFramePacket
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.turn_state_flow import TurnStateFlowController

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Salamence", "Gholdengo", "Dragonite", "Flutter Mane", "Tyranitar", "Pelipper")


class _NoHardwareCaptureBackend:
    def __init__(self) -> None:
        self.start_count = 0

    def start(self, selector: str, on_frame=None) -> DeviceOpenResult:
        del selector, on_frame
        self.start_count += 1
        return DeviceOpenResult(False, False, None, "CAPTURE_DISABLED_FOR_EVIDENCE")

    def stop(self) -> None:
        return None

    def get_latest_frame(self) -> SourceFramePacket | None:
        return None

    def is_running(self) -> bool:
        return False


def _save(app: QApplication, window: BattleRecordUiWindow, path: Path) -> None:
    app.processEvents()
    image = window.grab()
    if image.width() != 1920 or image.height() != 1080:
        raise RuntimeError(f"unexpected evidence size: {image.width()}x{image.height()}")
    if not image.save(str(path), "PNG"):
        raise RuntimeError(f"failed to save {path}")


def main(output_directory: Path) -> int:
    output_directory.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    repository = SQLiteRepository(output_directory / "evidence.db")
    export_directory = output_directory / "exports"
    export_directory.mkdir(exist_ok=True)
    controller = TurnStateFlowController(
        MatchApplication(repository, export_directory),
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    capture_backend = _NoHardwareCaptureBackend()
    ocr_directory = output_directory / "ocr"
    ocr_directory.mkdir(exist_ok=True)
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_directory,
        capture_backend=capture_backend,
        auto_start_capture=False,
    )
    view = controller.new_match()
    controller.confirm_selection_facts(list(SELF_TEAM), list(OPPONENT_TEAM))
    controller.submit_mock_advice(list(SELF_TEAM[:3]), SELF_TEAM[0])
    controller.apply_selection(list(SELF_TEAM[:3]), SELF_TEAM[0], human_confirmed=True)
    controller.start_turn_capture()
    window.render_view()
    window.header_tabs.setCurrentIndex(1)
    window.show()

    window.opponent_active_input.setText("Dragonite")
    if not window.parity_ability_card.isHidden():
        raise RuntimeError("no-entry-ability species unexpectedly showed prompt")
    _save(app, window, output_directory / "01-dragonite-no-ability-prompt.png")

    window.opponent_active_input.setText("Salamence")
    candidates = tuple(button.text() for button in window.parity_ability_buttons)
    if candidates != ("いかく", "じしんかじょう", "不明"):
        raise RuntimeError(f"unexpected Salamence candidates: {candidates!r}")
    if "#demo" in window.battle_context_label.text():
        raise RuntimeError("demo match label remains visible")
    if view.projection.match_id not in window.battle_context_label.text():
        raise RuntimeError("authoritative match id is not visible")
    _save(app, window, output_directory / "02-salamence-entry-candidates.png")

    manifest = {
        "resolution": "1920x1080",
        "match_header": window.battle_context_label.text(),
        "demo_label_visible": False,
        "dragonite_prompt_visible": False,
        "salamence_candidates": list(candidates),
        "real_provider_send": 0,
        "network_send": 0,
        "capture_start": capture_backend.start_count,
        "game_action": 0,
    }
    (output_directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    window.close()
    repository.close()
    return 0


if __name__ == "__main__":
    destination = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("C:/tmp/maple-issue31-field-blockers")
    )
    raise SystemExit(main(destination))
