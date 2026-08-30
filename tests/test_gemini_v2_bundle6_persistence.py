"""Gemini V2 Bundle 6: persistence migration v20 -> v21 and structured V2 storage.

Mirrors the additive-migration test pattern established by
``test_issue31_turn_state_contract_bundle_a.py``
(``test_legacy_rich_action_completions_schema_migrates_additively``): a raw
sqlite3 connection is hand-built at the pre-Bundle-6 schema shape, seeded
with a legacy-shaped ``turn_advices`` row, then migrated -- proving the new
columns are added additively with the correct default and existing data
survives untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maple_next.application.service import (
    TurnAdviceStructuredDataCorruptError,
    load_structured_turn_advice_v2,
)
from maple_next.domain.enums import ActionType
from maple_next.domain.models import TurnAdviceSnapshot
from maple_next.persistence.schema import SCHEMA_VERSION, migrate
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.turn_response_v2 import (
    RESPONSE_SCHEMA_VERSION_V1,
    RESPONSE_SCHEMA_VERSION_V2,
    turn_advice_body_v2_from_dict,
)

_LEGACY_SCHEMA = """
CREATE TABLE schema_meta (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    schema_version INTEGER NOT NULL
);
INSERT INTO schema_meta(singleton_id, schema_version) VALUES (1, 20);

CREATE TABLE battle_sessions (
    session_id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL UNIQUE,
    state TEXT NOT NULL,
    battle_revision INTEGER NOT NULL,
    metadata_revision INTEGER NOT NULL,
    active_slot INTEGER NULL CHECK (active_slot IS NULL OR active_slot = 1)
);

CREATE TABLE battle_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
    created_at TEXT NOT NULL,
    UNIQUE(session_id, turn_number),
    FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id)
);

CREATE TABLE reviewed_turn_facts (
    turn_facts_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
    self_active TEXT NOT NULL,
    opponent_active TEXT NOT NULL,
    self_hp TEXT NOT NULL,
    opponent_hp TEXT NOT NULL,
    legal_moves_json TEXT NOT NULL,
    legal_switches_json TEXT NOT NULL,
    human_note TEXT NOT NULL,
    previous_snapshot_id TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id),
    FOREIGN KEY(turn_id) REFERENCES battle_turns(turn_id)
);

CREATE TABLE turn_advices (
    turn_advice_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
    job_id TEXT NOT NULL UNIQUE,
    input_snapshot_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_name TEXT NOT NULL,
    opponent_prediction TEXT NOT NULL,
    rationale TEXT NOT NULL,
    is_mock INTEGER NOT NULL CHECK (is_mock IN (0, 1)),
    source_type TEXT NOT NULL DEFAULT 'MOCK',
    model TEXT NOT NULL DEFAULT 'mock-dev',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id),
    FOREIGN KEY(turn_id) REFERENCES battle_turns(turn_id),
    FOREIGN KEY(input_snapshot_id) REFERENCES reviewed_turn_facts(turn_facts_id)
);
"""


def _seed_legacy_database(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    connection.executescript(_LEGACY_SCHEMA)
    connection.execute(
        "INSERT INTO battle_sessions "
        "(session_id, match_id, generation, state, battle_revision, "
        "metadata_revision, active_slot) "
        "VALUES ('s-legacy', 'm-legacy', 1, 'BATTLE_READY', 3, 0, 1)"
    )
    connection.execute(
        "INSERT INTO battle_turns (turn_id, session_id, turn_number, created_at) "
        "VALUES ('turn-1', 's-legacy', 1, '2026-08-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO reviewed_turn_facts "
        "(turn_facts_id, session_id, turn_id, turn_number, self_active, opponent_active, "
        "self_hp, opponent_hp, legal_moves_json, legal_switches_json, human_note, "
        "previous_snapshot_id, created_at) VALUES "
        "('facts-1', 's-legacy', 'turn-1', 1, 'Gholdengo', 'Garchomp', "
        "'71-80', '41-50', '[]', '[]', '', NULL, '2026-08-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO turn_advices "
        "(turn_advice_id, session_id, turn_id, turn_number, job_id, input_snapshot_id, "
        "action_type, action_name, opponent_prediction, rationale, is_mock, source_type, "
        "model, warnings_json, created_at) VALUES "
        "('advice-legacy', 's-legacy', 'turn-1', 1, 'job-legacy', 'facts-1', "
        "'MOVE', 'Make It Rain', 'Opponent likely attacks', 'Best expected value', 0, "
        "'GEMINI', 'gemini-2.5-flash', '[]', '2026-08-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()


# =========================================================================
# H. PERSISTENCE
# =========================================================================


def test_v20_to_v21_migration_adds_columns_with_correct_default(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    _seed_legacy_database(database_path)

    connection = sqlite3.connect(database_path)
    migrate(connection)

    version = connection.execute(
        "SELECT schema_version FROM schema_meta WHERE singleton_id = 1"
    ).fetchone()[0]
    assert version == SCHEMA_VERSION == 23

    row = connection.execute(
        "SELECT response_schema_version, advice_json, action_name FROM turn_advices "
        "WHERE turn_advice_id = 'advice-legacy'"
    ).fetchone()
    assert row[0] == RESPONSE_SCHEMA_VERSION_V1
    assert row[1] is None
    assert row[2] == "Make It Rain"
    connection.close()


def test_historical_row_defaults_response_schema_version_v1(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy2.db"
    _seed_legacy_database(database_path)
    connection = sqlite3.connect(database_path)
    migrate(connection)
    connection.close()

    repository = SQLiteRepository(database_path)
    advice = repository.get_turn_advice("advice-legacy")
    assert advice.response_schema_version == RESPONSE_SCHEMA_VERSION_V1
    assert advice.advice_json is None
    repository.close()


def test_repeated_migration_after_v21_bump_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "repeat.db"
    _seed_legacy_database(database_path)
    connection = sqlite3.connect(database_path)
    migrate(connection)
    migrate(connection)
    migrate(connection)
    version = connection.execute(
        "SELECT schema_version FROM schema_meta WHERE singleton_id = 1"
    ).fetchone()[0]
    assert version == SCHEMA_VERSION
    connection.close()


def _valid_v2_body_dict() -> dict[str, object]:
    return {
        "response_schema_version": RESPONSE_SCHEMA_VERSION_V2,
        "recommended_action": {
            "action_id": "move-1",
            "action_type": "MOVE",
            "action_name": "Make It Rain",
        },
        "recommendation_robustness": "HIGH",
        "reasons": ["確定情報から有利"],
        "opponent_prediction": {
            "primary": {
                "category": "DAMAGING_MOVE",
                "specific_action": None,
                "support_basis": "GENERAL_KNOWLEDGE",
                "support": "LOW",
                "summary": "相手はダメージ技を選択",
            },
            "alternatives": [],
        },
        "warnings": [],
    }


def test_v1_row_with_null_advice_json_loads(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "fresh.db")
    _seed_current_session_turn_facts(repository)
    snapshot = TurnAdviceSnapshot(
        turn_advice_id="advice-1",
        turn_id="turn-1",
        turn_number=1,
        job_id="job-1",
        input_snapshot_id="facts-1",
        action_type=ActionType.MOVE,
        action_name="Make It Rain",
        opponent_prediction="Opponent likely attacks",
        rationale="Best expected value",
        is_mock=False,
        source_type="GEMINI",
        model="gemini-2.5-flash",
        warnings=(),
        response_schema_version=RESPONSE_SCHEMA_VERSION_V1,
        advice_json=None,
    )
    repository.append_turn_advice("s-1", snapshot)
    loaded = repository.get_turn_advice("advice-1")
    assert loaded.response_schema_version == RESPONSE_SCHEMA_VERSION_V1
    assert loaded.advice_json is None
    repository.close()


def test_v2_canonical_advice_json_round_trips(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "fresh.db")
    _seed_current_session_turn_facts(repository)
    body = turn_advice_body_v2_from_dict(_valid_v2_body_dict())
    from maple_next.providers.turn_response_v2 import canonical_turn_advice_v2_json

    snapshot = TurnAdviceSnapshot(
        turn_advice_id="advice-2",
        turn_id="turn-1",
        turn_number=1,
        job_id="job-2",
        input_snapshot_id="facts-1",
        action_type=ActionType.MOVE,
        action_name="Make It Rain",
        opponent_prediction="相手はダメージ技を選択",
        rationale="確定情報から有利",
        is_mock=False,
        source_type="GEMINI",
        model="gemini-2.5-flash",
        warnings=(),
        response_schema_version=RESPONSE_SCHEMA_VERSION_V2,
        advice_json=canonical_turn_advice_v2_json(body),
    )
    repository.append_turn_advice("s-1", snapshot)
    loaded = repository.get_turn_advice("advice-2")
    decoded = load_structured_turn_advice_v2(loaded)
    assert decoded == body
    repository.close()


def test_corrupt_v2_advice_json_fails_closed(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "fresh.db")
    _seed_current_session_turn_facts(repository)
    snapshot = TurnAdviceSnapshot(
        turn_advice_id="advice-3",
        turn_id="turn-1",
        turn_number=1,
        job_id="job-3",
        input_snapshot_id="facts-1",
        action_type=ActionType.MOVE,
        action_name="Make It Rain",
        opponent_prediction="相手はダメージ技を選択",
        rationale="確定情報から有利",
        is_mock=False,
        source_type="GEMINI",
        model="gemini-2.5-flash",
        warnings=(),
        response_schema_version=RESPONSE_SCHEMA_VERSION_V2,
        advice_json='{"response_schema_version": "maple-turn-advice-response.v2"}',
    )
    repository.append_turn_advice("s-1", snapshot)
    loaded = repository.get_turn_advice("advice-3")
    with pytest.raises(TurnAdviceStructuredDataCorruptError):
        load_structured_turn_advice_v2(loaded)
    # Fail closed means no fallback: the flattened columns must not be
    # silently substituted as though they were structured detail.
    assert loaded.opponent_prediction == "相手はダメージ技を選択"
    repository.close()


def test_v1_row_is_never_treated_as_structured_v2(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "fresh.db")
    _seed_current_session_turn_facts(repository)
    snapshot = TurnAdviceSnapshot(
        turn_advice_id="advice-4",
        turn_id="turn-1",
        turn_number=1,
        job_id="job-4",
        input_snapshot_id="facts-1",
        action_type=ActionType.MOVE,
        action_name="Make It Rain",
        opponent_prediction="Opponent likely attacks",
        rationale="Best expected value",
        is_mock=False,
        source_type="GEMINI",
        model="gemini-2.5-flash",
        warnings=(),
    )
    repository.append_turn_advice("s-1", snapshot)
    loaded = repository.get_turn_advice("advice-4")
    with pytest.raises(TurnAdviceStructuredDataCorruptError):
        load_structured_turn_advice_v2(loaded)
    repository.close()


def _seed_current_session_turn_facts(repository: SQLiteRepository) -> None:
    repository.connection.execute(
        "INSERT INTO battle_sessions "
        "(session_id, match_id, generation, state, battle_revision, "
        "metadata_revision, active_slot) "
        "VALUES ('s-1', 'm-1', 1, 'BATTLE_READY', 1, 0, 1)"
    )
    repository.connection.execute(
        "INSERT INTO battle_turns (turn_id, session_id, turn_number, created_at) "
        "VALUES ('turn-1', 's-1', 1, '2026-08-18T00:00:00+00:00')"
    )
    repository.connection.execute(
        "INSERT INTO reviewed_turn_facts "
        "(turn_facts_id, session_id, turn_id, turn_number, self_active, opponent_active, "
        "self_hp, opponent_hp, legal_moves_json, legal_switches_json, human_note, "
        "previous_snapshot_id, created_at) VALUES "
        "('facts-1', 's-1', 'turn-1', 1, 'Gholdengo', 'Garchomp', "
        "'71-80', '41-50', '[]', '[]', '', NULL, '2026-08-18T00:00:00+00:00')"
    )
    repository.connection.commit()
