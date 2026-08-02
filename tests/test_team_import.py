from __future__ import annotations

import pytest

from maple_next.ui.team_import import (
    MAX_TEAM_IMPORT_BYTES,
    TeamImportError,
    parse_team_import,
    read_team_import,
)

TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")


def test_parse_maple_json_preserves_order_and_ignores_source() -> None:
    imported = parse_team_import(
        '{"schema_version":"maple-team.v1","name":"最後に使用した構築",'
        '"pokemon":["Meowscarada","Gholdengo","Dragonite","Dondozo",'
        '"Flutter Mane","Urshifu"],"source":{"kind":"selection_snapshot"}}'
    )

    assert imported.pokemon == TEAM
    assert imported.name == "最後に使用した構築"


@pytest.mark.parametrize(
    "text",
    [
        "Meowscarada\nGholdengo\nDragonite\nDondozo\nFlutter Mane\nUrshifu\n",
        "Meowscarada,Gholdengo,Dragonite,Dondozo,Flutter Mane,Urshifu",
    ],
)
def test_parse_utf8_text_import_forms(text: str) -> None:
    imported = parse_team_import(text)

    assert imported.pokemon == TEAM
    assert imported.name is None


@pytest.mark.parametrize(
    "text",
    [
        "one\ntwo\nthree",
        '{"schema_version":"maple-team.v1","pokemon":["one","two"]}',
        "one,two,three,four,five,five",
        "one,two,,four,five,six",
    ],
)
def test_invalid_import_is_rejected(text: str) -> None:
    with pytest.raises(TeamImportError):
        parse_team_import(text)


def test_unsupported_schema_is_rejected() -> None:
    with pytest.raises(TeamImportError):
        parse_team_import(
            '{"schema_version":"maple-team.v2",'
            '"pokemon":["one","two","three","four","five","six"]}'
        )


def test_invalid_json_is_rejected_instead_of_treated_as_text() -> None:
    with pytest.raises(TeamImportError):
        parse_team_import(
            '{"schema_version":"maple-team.v1",'
            '"pokemon":["one","two","three","four","five","six"]'
        )


def test_five_and_seven_entries_are_rejected() -> None:
    with pytest.raises(TeamImportError):
        parse_team_import("one,two,three,four,five")
    with pytest.raises(TeamImportError):
        parse_team_import("one,two,three,four,five,six,seven")


def test_oversized_file_is_rejected(tmp_path) -> None:
    source = tmp_path / "oversized.txt"
    source.write_bytes(b"a" * (MAX_TEAM_IMPORT_BYTES + 1))

    with pytest.raises(TeamImportError, match="大きすぎます"):
        read_team_import(source)
