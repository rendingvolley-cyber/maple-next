"""Generate the three accepted Selection v3 evidence states at 1920x1080.

The script uses fixture ``SelectionSlotMatch.crop`` images, an isolated SQLite
database, a fake Selection transport, and a capture backend that cannot start.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.capture.contracts import DeviceOpenResult, SourceFramePacket
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GEMINI_SOURCE_TYPE,
    FakeSelectionAdviceTransport,
    ProviderConfig,
    ProviderTransportError,
    SanitizedProviderResult,
)
from maple_next.selection_roi.contracts import (
    SelectionCandidateScore,
    SelectionSlotMatch,
)
from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.turn_state_flow import TurnStateFlowController

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Salamence",
    "Garchomp",
    "Dragonite",
    "Flutter Mane",
    "Tyranitar",
    "Pelipper",
)
FIXTURE_COLORS = ("#d04d4d", "#4d78d0", "#7c55c7", "#ca8a35")


class _NoCaptureBackend:
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


class _SyncDispatch:
    def __init__(self, transport, request, config, *, on_succeeded, on_failed) -> None:
        self.transport = transport
        self.request = request
        self.config = config
        self.on_succeeded = on_succeeded
        self.on_failed = on_failed

    def start(self) -> None:
        try:
            result = self.transport.send(self.request, self.config)
        except ProviderTransportError as exc:
            self.on_failed(str(exc))
        else:
            self.on_succeeded(result)


def _fixture_match(slot: int, name: str, color: str) -> SelectionSlotMatch:
    crop = QImage(240, 128, QImage.Format.Format_RGB32)
    crop.fill(QColor(color))
    # A supplied fixture mark makes it visually obvious that the widget is
    # showing crop pixels rather than a decorative Pokemon image.
    for x in range(18, 222):
        crop.setPixelColor(x, 12 + slot * 4, QColor("#edf5fb"))
    return SelectionSlotMatch(
        slot=slot,
        crop=crop,
        assigned_label=name,
        assigned_score=0.91 - slot * 0.01,
        top_candidates=(
            SelectionCandidateScore(name, 0.91 - slot * 0.01, 3),
        ),
    )


def _capture(app: QApplication, window: BattleRecordUiWindow, path: Path) -> None:
    window.updateGeometry()
    for _ in range(3):
        app.processEvents()
    window.repaint()
    app.processEvents()
    pixmap = window.grab()
    if (pixmap.width(), pixmap.height()) != (1920, 1080):
        raise RuntimeError(f"unexpected evidence size: {pixmap.size()}")
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"failed to save {path}")


def main(output_directory: Path) -> int:
    output_directory.mkdir(parents=True, exist_ok=True)
    app = cast(QApplication, QApplication.instance() or QApplication([]))
    repository = SQLiteRepository(output_directory / "selection-v3-evidence.db")
    export_directory = output_directory / "export"
    export_directory.mkdir(exist_ok=True)
    application = MatchApplication(repository, export_directory)
    transport = FakeSelectionAdviceTransport(
        responses=[
            SanitizedProviderResult(
                payload={"selected_three": list(SELF_TEAM[:3]), "lead": SELF_TEAM[0]},
                source_type=GEMINI_SOURCE_TYPE,
                model="fixture-selection-v3",
            )
        ]
    )
    adapter = GeminiSelectionAdviceAdapter(
        transport,
        lambda: ProviderConfig(
            api_key="fixture-only",
            model="fixture-selection-v3",
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
    ocr_directory = output_directory / "ocr"
    ocr_directory.mkdir(exist_ok=True)
    capture_backend = _NoCaptureBackend()
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=ocr_directory,
        capture_backend=capture_backend,
        auto_start_capture=False,
    )
    window.header_tabs.setCurrentIndex(0)
    window.show()

    # Offline identity transition only. No visible NEW MATCH control, capture,
    # provider, or external game process is invoked.
    controller.new_match()
    window.render_view()
    for field, name in zip(window.self_team_inputs, SELF_TEAM, strict=True):
        field.setText(name)
    for slot in range(1, 5):
        window._render_selection_roi_slot(  # noqa: SLF001
            slot,
            _fixture_match(slot, OPPONENT_TEAM[slot - 1], FIXTURE_COLORS[slot - 1]),
        )
    for slot in (5, 6):
        window._render_selection_roi_slot(slot, None)  # noqa: SLF001
    for slot in range(1, 4):
        window._set_selection_slot_value(  # noqa: SLF001
            slot,
            OPPONENT_TEAM[slot - 1],
            origin=SelectionInputOrigin.MANUAL_TEXT,
            user_locked=True,
        )
    window._set_selection_slot_value(  # noqa: SLF001
        4,
        OPPONENT_TEAM[3],
        origin=SelectionInputOrigin.OCR_AUTO,
        user_locked=False,
    )
    window._render_selection_v3(controller.refresh())  # noqa: SLF001
    _capture(app, window, output_directory / "01-opponent-roi-incomplete-qt.png")

    for slot in range(1, 7):
        window._set_selection_slot_value(  # noqa: SLF001
            slot,
            OPPONENT_TEAM[slot - 1],
            origin=SelectionInputOrigin.MANUAL_TEXT,
            user_locked=True,
        )
    window._on_send_current_selection_to_gemini()  # noqa: SLF001
    window.render_view()
    _capture(app, window, output_directory / "02-gemini-default-selection-qt.png")

    window._toggle_selection_v3_actual(1)  # noqa: SLF001 - remove number 2
    window._toggle_selection_v3_actual(3)  # noqa: SLF001 - append new number 3
    _capture(app, window, output_directory / "03-human-modified-renumbered-qt.png")

    if capture_backend.start_count != 0:
        raise RuntimeError("capture backend unexpectedly started")
    if transport.call_count != 1:
        raise RuntimeError("fake Selection transport did not dispatch exactly once")
    print("selection_v3_screenshots=3 size=1920x1080 fixture_crops=4 empty_crops=2")
    print("default_order=1/2/3 human_toggle=1 renumbered=1 confirm_enabled=1")
    print("real_selection_gemini=0 real_turn_gemini=0 real_capture=0 real_new_match=0")
    print("provider_network=0 runtime_meta_fetch=0 automatic_apply=0 game_action=0")
    window.close()
    repository.close()
    return 0


if __name__ == "__main__":
    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("C:/tmp/maple-issue31-selection-v3-evidence")
    )
    raise SystemExit(main(target))
