"""Focused pure-domain coverage for tournament Battle Mega state."""

from __future__ import annotations

import pytest

from maple_next.domain.mega_evolution import (
    MEGA_EVENT_PROVENANCE,
    MEGA_STATE_SCHEMA_VERSION,
    MegaBattleState,
    MegaEvolutionError,
    MegaSide,
    deterministic_mega_form,
    mega_state_from_canonical_dict,
    mega_state_to_canonical_dict,
)


def test_default_state_has_no_actual_mega_use() -> None:
    state = MegaBattleState()
    assert state.schema_version == MEGA_STATE_SCHEMA_VERSION
    assert state.self_side.mega_used is False
    assert state.opponent_side.mega_used is False


def test_self_metagross_actual_mega_is_persistent_canonical_fact() -> None:
    state = MegaBattleState().record_use(
        side=MegaSide.SELF,
        pokemon_name="メタグロス",
        current_form=deterministic_mega_form("メタグロス"),
        confirmed_turn=3,
        confirmed_at_utc="2026-08-29T10:00:00+00:00",
    )
    assert state.self_side.mega_used is True
    assert state.self_side.mega_pokemon == "メタグロス"
    assert state.self_side.current_form == "メガメタグロス"
    assert state.self_side.confirmed_turn == 3
    assert state.self_side.provenance == MEGA_EVENT_PROVENANCE
    assert mega_state_from_canonical_dict(mega_state_to_canonical_dict(state)) == state


def test_self_swampert_form_is_deterministic() -> None:
    assert deterministic_mega_form("ラグラージ") == "メガラグラージ"
    assert deterministic_mega_form("Swampert") == "Mega Swampert"


def test_unknown_opponent_form_stays_unknown_instead_of_being_guessed() -> None:
    state = MegaBattleState().record_use(
        side=MegaSide.OPPONENT,
        pokemon_name="不明なポケモン",
        current_form=deterministic_mega_form("不明なポケモン"),
        confirmed_turn=2,
        confirmed_at_utc="2026-08-29T10:00:00+00:00",
    )
    assert state.opponent_side.mega_used is True
    assert state.opponent_side.mega_pokemon == "不明なポケモン"
    assert state.opponent_side.current_form is None


def test_each_side_can_use_mega_once_independently() -> None:
    state = MegaBattleState().record_use(
        side=MegaSide.SELF,
        pokemon_name="ラグラージ",
        current_form="メガラグラージ",
        confirmed_turn=1,
        confirmed_at_utc="2026-08-29T10:00:00+00:00",
    )
    state = state.record_use(
        side=MegaSide.OPPONENT,
        pokemon_name="メタグロス",
        current_form="メガメタグロス",
        confirmed_turn=1,
        confirmed_at_utc="2026-08-29T10:00:01+00:00",
    )
    assert state.self_side.mega_used is True
    assert state.opponent_side.mega_used is True


def test_duplicate_mega_use_same_side_fails_closed() -> None:
    state = MegaBattleState().record_use(
        side=MegaSide.SELF,
        pokemon_name="メタグロス",
        current_form="メガメタグロス",
        confirmed_turn=1,
        confirmed_at_utc="2026-08-29T10:00:00+00:00",
    )
    with pytest.raises(MegaEvolutionError, match="MEGA_ALREADY_USED:SELF"):
        state.record_use(
            side=MegaSide.SELF,
            pokemon_name="ラグラージ",
            current_form="メガラグラージ",
            confirmed_turn=4,
            confirmed_at_utc="2026-08-29T10:10:00+00:00",
        )


def test_empty_migration_json_means_no_recorded_actual_mega() -> None:
    assert mega_state_from_canonical_dict({}) == MegaBattleState()


def test_unused_side_cannot_carry_event_data() -> None:
    payload = mega_state_to_canonical_dict(MegaBattleState())
    payload["self"]["mega_pokemon"] = "メタグロス"
    with pytest.raises(MegaEvolutionError, match="UNUSED_MEGA_STATE_MUST_NOT_CARRY_EVENT_DATA"):
        mega_state_from_canonical_dict(payload)


def test_malformed_payload_fails_closed() -> None:
    with pytest.raises(MegaEvolutionError, match="MEGA_STATE_FIELDS_INVALID"):
        mega_state_from_canonical_dict({"schema_version": MEGA_STATE_SCHEMA_VERSION})
