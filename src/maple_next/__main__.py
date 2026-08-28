"""Official PySide6 desktop entrypoint for Maple Next."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.transport import (
    GeminiSelectionAdviceTransport,
    load_selection_provider_config_from_env,
)
from maple_next.providers.turn_transport import (
    GeminiTurnAdviceTransport,
    load_authorized_turn_provider_config_from_env,
)
from maple_next.runtime_env import bootstrap_repo_root_dotenv
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.gemini_turn_advice import GeminiTurnAdviceAdapter
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController
from maple_next.ui.two_step_battle_record_ui import TwoStepBattleRecordUiWindow


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_database_path() -> Path:
    configured = os.environ.get("MAPLE_NEXT_DB")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".maple-next" / "maple-next.db"


def default_export_directory() -> Path:
    configured = os.environ.get("MAPLE_NEXT_EXPORT_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".maple-next" / "exports"


def default_ocr_data_directory() -> Path:
    """Keep every OCR/ROI asset inside the official local repository root."""

    return repository_root() / "data" / "ocr"


def build_turn_gemini_adapter() -> GeminiTurnAdviceAdapter:
    """Build the fail-closed production Turn Advice adapter."""

    return GeminiTurnAdviceAdapter(
        GeminiTurnAdviceTransport(),
        load_authorized_turn_provider_config_from_env,
    )


def build_rich_turn_gemini_adapter() -> GeminiRichTurnAdviceAdapter:
    """Build the fail-closed production rich-state Turn Advice adapter.

    Same production transport/config source as the legacy adapter above --
    this is the same authorized real-provider seam, not a second one.
    """

    return GeminiRichTurnAdviceAdapter(
        GeminiTurnAdviceTransport(),
        load_authorized_turn_provider_config_from_env,
    )


def main(argv: Sequence[str] | None = None) -> int:
    # Load only allowlisted values from this checkout's .env. Existing process
    # injection wins, parent directories are never searched, and no values are
    # logged or returned to the UI.
    bootstrap_repo_root_dotenv(repository_root())

    parser = argparse.ArgumentParser(description="Maple Next Battle Record desktop app")
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument(
        "--export-directory",
        type=Path,
        default=default_export_directory(),
    )
    parser.add_argument(
        "--ocr-data-directory",
        type=Path,
        default=default_ocr_data_directory(),
        help="Must resolve to this checkout's data/ocr directory.",
    )
    args = parser.parse_args(argv)
    database_path = cast(Path, args.database).expanduser()
    export_directory = cast(Path, args.export_directory).expanduser()
    expected_ocr_data_directory = default_ocr_data_directory().resolve()
    ocr_data_directory = cast(Path, args.ocr_data_directory).expanduser().resolve()
    if ocr_data_directory != expected_ocr_data_directory:
        parser.error(
            "--ocr-data-directory must be the current repository's data/ocr directory"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    ocr_data_directory.mkdir(parents=True, exist_ok=True)

    qt_application = QApplication([sys.argv[0]])
    repository = SQLiteRepository(database_path)
    try:
        battle_application = MatchApplication(repository, export_directory)
        battle_application.recover_after_restart()
        controller = TurnStateFlowController(
            battle_application,
            repository,
            MockSelectionAdviceAdapter(),
            gemini_adapter=GeminiSelectionAdviceAdapter(
                GeminiSelectionAdviceTransport(),
                load_selection_provider_config_from_env,
            ),
            turn_gemini_adapter=build_turn_gemini_adapter(),
            rich_turn_gemini_adapter=build_rich_turn_gemini_adapter(),
        )
        window = TwoStepBattleRecordUiWindow(
            controller,
            ocr_data_directory=ocr_data_directory,
        )
        window.show()
        return qt_application.exec()
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
