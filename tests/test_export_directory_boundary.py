from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.domain.enums import BattleState, MatchOutcome, ResultDisposition
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Garchomp",
    "Gholdengo",
    "Dragonite",
    "Flutter Mane",
    "Garganacl",
    "Iron Bundle",
)


def build_ended_application(
    tmp_path: Path,
    *,
    repository_root: Path,
    export_directory: Path,
) -> tuple[SQLiteRepository, MatchApplication]:
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(exist_ok=True)
    repository = SQLiteRepository(runtime_directory / "maple.db")
    application = MatchApplication(
        repository,
        export_directory,
        repository_root=repository_root,
    )
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    result = MockSelectionAdviceAdapter().submit(
        application,
        selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
        lead="Meowscarada",
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.apply_selection(
        selected_three=("Dondozo", "Flutter Mane", "Urshifu"),
        lead="Dondozo",
        human_confirmed=True,
    )
    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    return repository, application


@pytest.mark.parametrize(
    "relative_export_path",
    [
        Path("."),
        Path("exports"),
        Path("src") / ".." / "exports",
    ],
)
def test_repository_local_export_directory_fails_closed_before_write(
    tmp_path: Path,
    relative_export_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    export_directory = repository_root / relative_export_path
    repository, application = build_ended_application(
        tmp_path,
        repository_root=repository_root,
        export_directory=export_directory,
    )
    before = repository.load_active_session()
    assert before is not None

    with pytest.raises(DomainError, match="EXPORT_DIRECTORY_INSIDE_REPOSITORY"):
        application.export_match()

    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.MATCH_ENDED
    assert after.battle_revision == before.battle_revision
    assert repository.get_match_export(after.session_id) is None
    assert list(repository_root.rglob("*.json")) == []
    repository.close()


def test_export_directory_outside_repository_still_succeeds(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    export_directory = tmp_path / "user-data" / "exports"
    repository, application = build_ended_application(
        tmp_path,
        repository_root=repository_root,
        export_directory=export_directory,
    )

    record = application.export_match()
    session = repository.load_active_session()

    assert Path(record.export_path).is_relative_to(export_directory.resolve())
    assert Path(record.export_path).is_file()
    assert not Path(record.export_path).is_relative_to(repository_root.resolve())
    assert session is not None
    assert session.state is BattleState.MATCH_EXPORTED
    repository.close()
