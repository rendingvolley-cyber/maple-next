"""Official PySide6 desktop entrypoint for Maple Next."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.explicit_turn_number import (
    ExplicitTurnNumberController,
    ExplicitTurnNumberWindow,
)


def default_database_path() -> Path:
    configured = os.environ.get("MAPLE_NEXT_DB")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".maple-next" / "maple-next.db"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maple Next Battle Record desktop app")
    parser.add_argument("--database", type=Path, default=default_database_path())
    args = parser.parse_args(argv)
    database_path = cast(Path, args.database).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    qt_application = QApplication([sys.argv[0]])
    repository = SQLiteRepository(database_path)
    try:
        battle_application = BattleApplication(repository)
        battle_application.recover_after_restart()
        controller = ExplicitTurnNumberController(
            battle_application,
            repository,
            MockSelectionAdviceAdapter(),
        )
        window = ExplicitTurnNumberWindow(controller)
        window.show()
        return qt_application.exec()
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
