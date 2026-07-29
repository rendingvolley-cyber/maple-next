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
    load_provider_config_from_env,
)
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController
from maple_next.ui.match_window import MatchFlowWindow


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maple Next Battle Record desktop app")
    parser.add_argument("--database", type=Path, default=default_database_path())
    parser.add_argument(
        "--export-directory",
        type=Path,
        default=default_export_directory(),
    )
    args = parser.parse_args(argv)
    database_path = cast(Path, args.database).expanduser()
    export_directory = cast(Path, args.export_directory).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    qt_application = QApplication([sys.argv[0]])
    repository = SQLiteRepository(database_path)
    try:
        battle_application = MatchApplication(repository, export_directory)
        battle_application.recover_after_restart()
        controller = MatchFlowController(
            battle_application,
            repository,
            MockSelectionAdviceAdapter(),
            gemini_adapter=GeminiSelectionAdviceAdapter(
                GeminiSelectionAdviceTransport(),
                load_provider_config_from_env,
            ),
        )
        window = MatchFlowWindow(controller)
        window.show()
        return qt_application.exec()
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
