"""Issue #31 Bundle A: Turn state / action-result delta / next-draft contract.

Focused tests for the new, additive domain + persistence + runtime-evidence
slice. Legacy Turn flow regression lives in ``test_turn_lifecycle.py`` and is
run alongside this file, unmodified.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maple_next.application.turn_legal_action_boundary import (
    build_confirmed_legal_actions_input,
)
from maple_next.application.turn_state_recovery import hydrate_turn_state
from maple_next.domain.enums import ActionOrder, ActionType, HpBucket
from maple_next.domain.models import BattleTurn, TurnFactsSnapshot
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ChangeObservation,
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FieldDelta,
    KnowledgeStatus,
    Known,
    LegalActionPrefillDraft,
    NextTurnStateDraft,
    ProvenanceStep,
    SideDelta,
    SideState,
    TurnIdentity,
    TurnStateError,
    TurnStateIdentityError,
    TurnStateStaleError,
    confirm_legal_action_selection,
    confirm_next_turn_state,
    derive_next_turn_state_draft,
    field_delta_from_json,
    field_delta_to_json,
    known_from_json,
    known_to_json,
)
from maple_next.persistence.fixed_evidence_runtime import (
    EvidencePathError,
    EvidenceValidationStatus,
    FixedEvidenceRuntime,
)
from maple_next.persistence.schema import SCHEMA_VERSION, migrate
from maple_next.persistence.sqlite import SQLiteRepository

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)

CONFIRMED_AT = "2026-08-06T00:00:00+00:00"


def _identity(
    *,
    turn_number: int = 1,
    battle_revision: int = 0,
    session_id: str = "session-1",
    match_id: str = "match-1",
    generation: int = 9,
    turn_id: str = "turn-1",
) -> TurnIdentity:
    return TurnIdentity(
        session_id=session_id,
        match_id=match_id,
        generation=generation,
        turn_id=turn_id,
        turn_number=turn_number,
        battle_revision=battle_revision,
    )


def _identity_kwargs(identity: TurnIdentity) -> dict[str, object]:
    return {
        "session_id": identity.session_id,
        "match_id": identity.match_id,
        "generation": identity.generation,
        "turn_id": identity.turn_id,
        "turn_number": identity.turn_number,
        "battle_revision": identity.battle_revision,
    }


def _confirmed_side(*, active: str = "Dondozo", stage: int = 0) -> SideState:
    return SideState(
        active=Known.confirmed(active, provenance_chain=_HUMAN),
        hp_bucket=Known.confirmed(HpBucket.FULL, provenance_chain=_HUMAN),
        status=Known.confirmed("NONE", provenance_chain=_HUMAN),
        attack_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        defense_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        special_attack_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        special_defense_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        speed_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        accuracy_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        evasion_stage=Known.confirmed(stage, provenance_chain=_HUMAN),
        side_effects=Known.confirmed((), provenance_chain=_HUMAN),
    )


def _unknown_side() -> SideState:
    return SideState(
        active=Known.unknown(),
        hp_bucket=Known.unknown(),
        status=Known.unknown(),
        attack_stage=Known.unknown(),
        defense_stage=Known.unknown(),
        special_attack_stage=Known.unknown(),
        special_defense_stage=Known.unknown(),
        speed_stage=Known.unknown(),
        accuracy_stage=Known.unknown(),
        evasion_stage=Known.unknown(),
        side_effects=Known.unknown(),
    )


def _unchanged_side_delta() -> SideDelta:
    return SideDelta(
        active=FieldDelta.unchanged(),
        hp_bucket=FieldDelta.unchanged(),
        status=FieldDelta.unchanged(),
        attack_stage=FieldDelta.unchanged(),
        defense_stage=FieldDelta.unchanged(),
        special_attack_stage=FieldDelta.unchanged(),
        special_defense_stage=FieldDelta.unchanged(),
        speed_stage=FieldDelta.unchanged(),
        accuracy_stage=FieldDelta.unchanged(),
        evasion_stage=FieldDelta.unchanged(),
        side_effects=FieldDelta.unchanged(),
    )


def _unknown_side_delta() -> SideDelta:
    return SideDelta(
        active=FieldDelta.unknown(),
        hp_bucket=FieldDelta.unknown(),
        status=FieldDelta.unknown(),
        attack_stage=FieldDelta.unknown(),
        defense_stage=FieldDelta.unknown(),
        special_attack_stage=FieldDelta.unknown(),
        special_defense_stage=FieldDelta.unknown(),
        speed_stage=FieldDelta.unknown(),
        accuracy_stage=FieldDelta.unknown(),
        evasion_stage=FieldDelta.unknown(),
        side_effects=FieldDelta.unknown(),
    )


def _changed_attack_stage_delta(value: int) -> SideDelta:
    delta = _unchanged_side_delta()
    return SideDelta(
        active=delta.active,
        hp_bucket=delta.hp_bucket,
        status=delta.status,
        attack_stage=FieldDelta.changed(value, provenance_chain=_HUMAN),
        defense_stage=delta.defense_stage,
        special_attack_stage=delta.special_attack_stage,
        special_defense_stage=delta.special_defense_stage,
        speed_stage=delta.speed_stage,
        accuracy_stage=delta.accuracy_stage,
        evasion_stage=delta.evasion_stage,
        side_effects=delta.side_effects,
    )


def _confirmation() -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True, confirmed_at_utc=CONFIRMED_AT, provenance="human_review"
    )


def _confirmed_state(
    *,
    identity: TurnIdentity | None = None,
    confirmed_state_id: str = "cs-1",
    previous: str | None = None,
    self_side: SideState | None = None,
    opponent_side: SideState | None = None,
    evidence_id: str | None = None,
) -> ConfirmedTurnState:
    return ConfirmedTurnState(
        confirmed_state_id=confirmed_state_id,
        identity=identity or _identity(),
        previous_confirmed_state_id=previous,
        self_side=self_side or _confirmed_side(),
        opponent_side=opponent_side or _confirmed_side(active="Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
        evidence_id=evidence_id,
    )


def _delta(
    *,
    identity: TurnIdentity | None = None,
    delta_id: str = "delta-1",
    based_on: str = "cs-1",
    self_side: SideDelta | None = None,
    opponent_side: SideDelta | None = None,
) -> ActionResultDelta:
    return ActionResultDelta(
        delta_id=delta_id,
        identity=identity or _identity(),
        based_on_confirmed_state_id=based_on,
        self_side=self_side or _unchanged_side_delta(),
        opponent_side=opponent_side or _unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )


def _next_identity(base: TurnIdentity) -> TurnIdentity:
    return TurnIdentity(
        session_id=base.session_id,
        match_id=base.match_id,
        generation=base.generation,
        turn_id="turn-2",
        turn_number=base.turn_number + 1,
        battle_revision=base.battle_revision + 1,
    )


# --- 1-4: migration -------------------------------------------------------


def test_fresh_database_migration_creates_new_tables(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "fresh.db")
    tables = {
        str(row[0])
        for row in repository.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "confirmed_turn_states",
        "action_result_deltas",
        "next_turn_state_drafts",
        "legal_action_prefill_drafts",
        "confirmed_legal_action_selections",
        "fixed_evidence_metadata",
        "rich_action_completions",
        "reviewed_turn_facts",
        "battle_turns",
    }.issubset(tables)
    version = repository.connection.execute(
        "SELECT schema_version FROM schema_meta WHERE singleton_id = 1"
    ).fetchone()[0]
    assert version == SCHEMA_VERSION
    repository.close()


def test_existing_database_migration_is_additive(tmp_path: Path) -> None:
    database_path = tmp_path / "existing.db"
    repository = SQLiteRepository(database_path)
    repository.connection.execute(
        "INSERT INTO battle_sessions "
        "(session_id, match_id, generation, state, battle_revision, "
        "metadata_revision, active_slot) VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("s-legacy", "m-legacy", 1, "BATTLE_READY", 0, 0),
    )
    repository.connection.commit()
    repository.close()

    reopened = SQLiteRepository(database_path)
    row = reopened.connection.execute(
        "SELECT session_id FROM battle_sessions WHERE session_id = 's-legacy'"
    ).fetchone()
    assert row is not None
    tables = {
        str(r[0])
        for r in reopened.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "confirmed_turn_states" in tables
    reopened.close()


def test_repeated_migration_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "repeat.db"
    connection = sqlite3.connect(database_path)
    migrate(connection)
    migrate(connection)
    migrate(connection)
    version = connection.execute(
        "SELECT schema_version FROM schema_meta WHERE singleton_id = 1"
    ).fetchone()[0]
    assert version == SCHEMA_VERSION
    connection.close()


def test_legacy_reviewed_turn_facts_row_read_write_regression(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "legacy.db")
    repository.connection.execute(
        "INSERT INTO battle_sessions "
        "(session_id, match_id, generation, state, battle_revision, "
        "metadata_revision, active_slot) VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("s1", "m1", 1, "TURN_CAPTURE_PENDING", 0, 0),
    )
    repository.append_turn("s1", BattleTurn(turn_id="t1", turn_number=1))
    snapshot = TurnFactsSnapshot(
        turn_facts_id="tf-1",
        turn_id="t1",
        turn_number=1,
        self_active="Dondozo",
        opponent_active="Garchomp",
        self_hp=HpBucket.FULL,
        opponent_hp=HpBucket.FULL,
        legal_moves=("Protect",),
        legal_switches=("Urshifu",),
    )
    repository.append_turn_facts("s1", snapshot)
    repository.connection.commit()
    assert repository.get_turn_facts("tf-1") == snapshot
    repository.close()


# --- 5: UNKNOWN / NONE / UNCHANGED / omission stay distinct ---------------


def test_unknown_none_unchanged_and_omission_remain_distinct() -> None:
    unknown = Known[str].unknown()
    confirmed_none_text = Known.confirmed("NONE", provenance_chain=_HUMAN)
    confirmed_unknown_text = Known.confirmed("UNKNOWN", provenance_chain=_HUMAN)

    assert unknown.status is KnowledgeStatus.UNKNOWN
    assert confirmed_none_text.status is KnowledgeStatus.CONFIRMED
    assert confirmed_none_text.value == "NONE"
    assert confirmed_unknown_text.status is KnowledgeStatus.CONFIRMED
    assert confirmed_unknown_text.value == "UNKNOWN"
    assert unknown != confirmed_unknown_text

    changed = FieldDelta.changed("PARALYSIS", provenance_chain=_HUMAN)
    unchanged = FieldDelta[str].unchanged()
    field_unknown = FieldDelta[str].unknown()
    assert {changed.observation, unchanged.observation, field_unknown.observation} == {
        ChangeObservation.CHANGED,
        ChangeObservation.UNCHANGED,
        ChangeObservation.UNKNOWN,
    }

    # Omission is not representable: a required field cannot be left out.
    with pytest.raises(TypeError):
        SideDelta(  # type: ignore[call-arg]
            active=FieldDelta.unchanged(),
            hp_bucket=FieldDelta.unchanged(),
            status=FieldDelta.unchanged(),
            attack_stage=FieldDelta.unchanged(),
            defense_stage=FieldDelta.unchanged(),
            special_attack_stage=FieldDelta.unchanged(),
            special_defense_stage=FieldDelta.unchanged(),
            speed_stage=FieldDelta.unchanged(),
            accuracy_stage=FieldDelta.unchanged(),
            # evasion_stage omitted
            side_effects=FieldDelta.unchanged(),
        )


# --- 6: unconfirmed stat stages never become zero --------------------------


def test_unconfirmed_stat_stage_does_not_become_zero() -> None:
    previous = _confirmed_state(self_side=_confirmed_side(stage=2))
    delta = _delta(self_side=_unknown_side_delta())

    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-1",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )

    assert draft.self_side.attack_stage.status is KnowledgeStatus.UNKNOWN
    assert draft.self_side.attack_stage.value is None


# --- 7-10: identity / revision fail-closed ---------------------------------


def test_delta_session_mismatch_fails_closed() -> None:
    previous = _confirmed_state()
    bad_delta = _delta(identity=_identity(session_id="other-session"))
    with pytest.raises(TurnStateIdentityError):
        derive_next_turn_state_draft(
            previous,
            bad_delta,
            draft_id="d",
            next_identity=_next_identity(previous.identity),
            derived_at_utc=CONFIRMED_AT,
        )


def test_delta_revision_mismatch_fails_closed() -> None:
    previous = _confirmed_state()
    bad_delta = _delta(identity=_identity(battle_revision=5))
    with pytest.raises(TurnStateIdentityError):
        derive_next_turn_state_draft(
            previous,
            bad_delta,
            draft_id="d",
            next_identity=_next_identity(previous.identity),
            derived_at_utc=CONFIRMED_AT,
        )


def test_delta_not_based_on_confirmed_state_fails_closed() -> None:
    previous = _confirmed_state(confirmed_state_id="cs-1")
    bad_delta = _delta(based_on="cs-other")
    with pytest.raises(TurnStateStaleError):
        derive_next_turn_state_draft(
            previous,
            bad_delta,
            draft_id="d",
            next_identity=_next_identity(previous.identity),
            derived_at_utc=CONFIRMED_AT,
        )


def test_next_identity_turn_mismatch_fails_closed() -> None:
    previous = _confirmed_state()
    delta = _delta()
    wrong_next = _identity(turn_number=5, battle_revision=1)
    with pytest.raises(TurnStateIdentityError):
        derive_next_turn_state_draft(
            previous,
            delta,
            draft_id="d",
            next_identity=wrong_next,
            derived_at_utc=CONFIRMED_AT,
        )


# --- 11-12: action + delta transaction atomicity ---------------------------


def test_action_and_delta_transaction_is_atomic(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "atomic.db")
    previous = _confirmed_state()
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
    delta = _delta()

    repository.record_rich_action_completion(
        transaction_id="rac-1",
        identity=previous.identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Wave Crash",
        opponent_action_type=ActionType.MOVE,
        opponent_action_name="Earthquake",
        action_order=ActionOrder.SELF_FIRST,
        delta=delta,
    )

    assert repository.get_action_result_delta("delta-1") == delta
    completion = repository.get_rich_action_completion_by_turn(previous.identity.turn_id)
    assert completion is not None
    assert completion["own_action_name"] == "Wave Crash"
    assert completion["match_id"] == previous.identity.match_id
    assert completion["battle_revision"] == previous.identity.battle_revision
    assert completion["based_on_confirmed_state_id"] == previous.confirmed_state_id
    repository.close()


def test_failed_action_delta_transaction_commits_neither(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "atomic_fail.db")
    previous = _confirmed_state()
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
    delta = _delta()

    repository.record_rich_action_completion(
        transaction_id="rac-1",
        identity=previous.identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Wave Crash",
        opponent_action_type=ActionType.MOVE,
        opponent_action_name="Earthquake",
        action_order=ActionOrder.SELF_FIRST,
        delta=delta,
    )
    # Same turn_id violates the UNIQUE constraint -> the second call's own
    # transaction rolls back entirely, without touching the first call's
    # already-committed row.
    with pytest.raises(sqlite3.IntegrityError):
        repository.record_rich_action_completion(
            transaction_id="rac-2",
            identity=previous.identity,
            own_action_type=ActionType.MOVE,
            own_action_name="Protect",
            opponent_action_type=ActionType.MOVE,
            opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
            delta=_delta(delta_id="delta-2"),
        )

    with pytest.raises(KeyError):
        repository.get_action_result_delta("delta-2")
    completion = repository.get_rich_action_completion_by_turn(previous.identity.turn_id)
    assert completion is not None
    assert completion["transaction_id"] == "rac-1"
    repository.close()


def test_second_insert_failure_rolls_back_delta_too(tmp_path: Path) -> None:
    """A fresh transaction_id/turn combo still rolls back delta+completion together
    when the completion INSERT itself fails (e.g. duplicate delta_id)."""

    repository = SQLiteRepository(tmp_path / "atomic_fail2.db")
    previous = _confirmed_state()
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
    delta = _delta()
    repository.record_rich_action_completion(
        transaction_id="rac-1",
        identity=previous.identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Wave Crash",
        opponent_action_type=ActionType.MOVE,
        opponent_action_name="Earthquake",
        action_order=ActionOrder.SELF_FIRST,
        delta=delta,
    )

    other_identity = _identity(turn_id="turn-9")
    other_state = _confirmed_state(confirmed_state_id="cs-9", identity=other_identity)
    with repository.transaction():
        repository.append_confirmed_turn_state(other_state)

    # Reusing delta_id="delta-1" makes the delta INSERT itself violate the
    # PRIMARY KEY -> the whole second attempt (delta + completion) rolls back.
    with pytest.raises(sqlite3.IntegrityError):
        repository.record_rich_action_completion(
            transaction_id="rac-3",
            identity=other_identity,
            own_action_type=ActionType.MOVE,
            own_action_name="Protect",
            opponent_action_type=ActionType.MOVE,
            opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
            delta=_delta(delta_id="delta-1", identity=other_identity, based_on="cs-9"),
        )

    assert repository.get_rich_action_completion_by_turn("turn-9") is None
    repository.close()


@pytest.mark.parametrize(
    "mismatch_kwargs",
    [
        {"session_id": "other-session"},
        {"match_id": "other-match"},
        {"generation": 99},
        {"turn_id": "other-turn"},
        {"turn_number": 7},
        {"battle_revision": 7},
    ],
)
def test_action_completion_identity_mismatch_saves_nothing(
    tmp_path: Path, mismatch_kwargs: dict[str, object]
) -> None:
    repository = SQLiteRepository(tmp_path / "atomic_mismatch.db")
    previous = _confirmed_state()
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
    delta = _delta()
    mismatched_identity = _identity(**{**_identity_kwargs(previous.identity), **mismatch_kwargs})

    with pytest.raises(TurnStateIdentityError):
        repository.record_rich_action_completion(
            transaction_id="rac-mismatch",
            identity=mismatched_identity,
            own_action_type=ActionType.MOVE,
            own_action_name="Wave Crash",
            opponent_action_type=ActionType.MOVE,
            opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
            delta=delta,
        )

    with pytest.raises(KeyError):
        repository.get_action_result_delta("delta-1")
    assert repository.get_rich_action_completion_by_turn(previous.identity.turn_id) is None
    repository.close()


def test_action_completion_missing_confirmed_state_saves_nothing(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "atomic_missing_state.db")
    delta = _delta(based_on="cs-does-not-exist")

    with pytest.raises(TurnStateStaleError):
        repository.record_rich_action_completion(
            transaction_id="rac-missing",
            identity=delta.identity,
            own_action_type=ActionType.MOVE,
            own_action_name="Wave Crash",
            opponent_action_type=ActionType.MOVE,
            opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
            delta=delta,
        )

    with pytest.raises(KeyError):
        repository.get_action_result_delta("delta-1")
    assert repository.get_rich_action_completion_by_turn(delta.identity.turn_id) is None
    repository.close()


def test_action_completion_public_api_owns_its_own_transaction(tmp_path: Path) -> None:
    """Calling the public API without an outer transaction() still commits atomically."""

    repository = SQLiteRepository(tmp_path / "atomic_owns_txn.db")
    previous = _confirmed_state()
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
    delta = _delta()

    # No surrounding `with repository.transaction():` -- the public API is
    # itself the transaction owner.
    repository.record_rich_action_completion(
        transaction_id="rac-owns-1",
        identity=previous.identity,
        own_action_type=ActionType.MOVE,
        own_action_name="Wave Crash",
        opponent_action_type=ActionType.MOVE,
        opponent_action_name="Earthquake",
        action_order=ActionOrder.SELF_FIRST,
        delta=delta,
    )

    assert repository.get_action_result_delta("delta-1") == delta
    repository.close()


# --- 13-16: lifecycle derivation semantics ----------------------------------


def test_next_draft_changed_field_takes_explicit_after_value() -> None:
    previous = _confirmed_state(self_side=_confirmed_side(stage=0))
    delta = _delta(self_side=_changed_attack_stage_delta(3))

    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-changed",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )

    assert draft.self_side.attack_stage == Known.confirmed(3, provenance_chain=_HUMAN)


def test_next_draft_unchanged_field_carries_forward_prior_value() -> None:
    previous = _confirmed_state(self_side=_confirmed_side(stage=2))
    delta = _delta(self_side=_unchanged_side_delta())

    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-unchanged",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )

    assert draft.self_side.attack_stage == Known(
        status=KnowledgeStatus.CONFIRMED,
        value=2,
        provenance_chain=(*_HUMAN, ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD),
    )
    assert draft.self_side.active == Known(
        status=KnowledgeStatus.CONFIRMED,
        value=previous.self_side.active.value,
        provenance_chain=(*_HUMAN, ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD),
    )


def test_next_draft_unknown_field_becomes_explicitly_unknown() -> None:
    previous = _confirmed_state(self_side=_confirmed_side(active="Dondozo"))
    delta = _delta(self_side=_unknown_side_delta())

    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-unknown",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )

    assert draft.self_side.active == Known.unknown()


def test_omitted_delta_field_is_not_representable_as_unchanged() -> None:
    from maple_next.domain.turn_state import TurnStateError

    with pytest.raises(TypeError):
        FieldDelta()  # type: ignore[call-arg]

    with pytest.raises(TurnStateError):
        field_delta_from_json({})  # missing "observation" -> fails closed


def test_field_delta_json_round_trip_and_missing_key_fails_closed() -> None:
    changed = FieldDelta.changed("PARALYSIS", provenance_chain=_HUMAN)
    payload = field_delta_to_json(changed)
    assert field_delta_from_json(payload) == changed

    known = Known.confirmed("NONE", provenance_chain=_HUMAN)
    known_payload = known_to_json(known)
    assert known_from_json(known_payload) == known

    from maple_next.domain.turn_state import TurnStateError

    with pytest.raises(TurnStateError):
        known_from_json({"status": "CONFIRMED"})  # "value" omitted


# --- 17: draft is never provider-ready --------------------------------------


def test_draft_is_never_provider_ready() -> None:
    previous = _confirmed_state()
    delta = _delta()
    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-x",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    assert draft.provider_ready is False

    with pytest.raises(ValueError):
        NextTurnStateDraft(
            draft_id="bad",
            identity=_next_identity(previous.identity),
            based_on_confirmed_state_id=previous.confirmed_state_id,
            source_delta_id=delta.delta_id,
            self_side=draft.self_side,
            opponent_side=draft.opponent_side,
            weather=draft.weather,
            terrain=draft.terrain,
            derived_at_utc=CONFIRMED_AT,
            provider_ready=True,
        )


# --- 18-20: prefill / human confirmation contract ---------------------------


def test_prefill_is_not_a_confirmed_legal_action() -> None:
    from maple_next.domain.turn_state import ConfirmedLegalActionSelection

    identity = _identity()
    prefill = LegalActionPrefillDraft(
        prefill_id="pf-1",
        identity=identity,
        based_on_confirmed_state_id="cs-1",
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        derived_at_utc=CONFIRMED_AT,
        confidence=0.98,
    )
    # A prefill -- even a high-confidence one -- is never itself a confirmed
    # legal action; it carries no ConfirmationMeta and is a distinct type.
    assert not isinstance(prefill, ConfirmedLegalActionSelection)
    assert not hasattr(prefill, "confirmation")


def test_explicit_human_confirmation_alone_creates_final_legal_action() -> None:
    identity = _identity()
    selection = confirm_legal_action_selection(
        confirmation_id="conf-1",
        identity=identity,
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        confirmation=_confirmation(),
    )
    assert selection.action_name == "Wave Crash"
    assert selection.source_prefill_id is None

    with pytest.raises(ValueError):
        ConfirmationMeta(confirmed_by_human=False, confirmed_at_utc=CONFIRMED_AT, provenance="x")


def test_stale_prefill_confirmation_fails_closed() -> None:
    identity = _identity()
    prefill = LegalActionPrefillDraft(
        prefill_id="pf-1",
        identity=identity,
        based_on_confirmed_state_id="cs-1",
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        derived_at_utc=CONFIRMED_AT,
    )
    with pytest.raises(TurnStateStaleError):
        confirm_legal_action_selection(
            confirmation_id="conf-2",
            identity=identity,
            action_type=ActionType.MOVE,
            action_name="Wave Crash",
            confirmation=_confirmation(),
            prefill=prefill,
            latest_confirmed_state_id="cs-2-newer",
        )


# --- 21-22: restart hydration -----------------------------------------------


def test_restart_hydration_recovers_confirmed_state_delta_and_draft(tmp_path: Path) -> None:
    database_path = tmp_path / "hydrate.db"
    repository = SQLiteRepository(database_path)
    previous = _confirmed_state()
    delta = _delta()
    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-1",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
        repository.append_action_result_delta(delta)
        repository.upsert_next_turn_state_draft(draft)
    repository.close()

    restarted = SQLiteRepository(database_path)
    recovery = hydrate_turn_state(restarted, previous.identity.session_id)

    assert recovery.latest_confirmed_state == previous
    assert recovery.latest_delta == delta
    assert recovery.latest_draft == draft
    restarted.close()


def test_stale_hydrated_draft_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "hydrate_stale.db"
    repository = SQLiteRepository(database_path)
    stale_previous = _confirmed_state(confirmed_state_id="cs-0")
    previous = _confirmed_state(
        confirmed_state_id="cs-1",
        identity=_identity(turn_number=2, battle_revision=1, turn_id="turn-2"),
        previous=None,
    )
    delta = _delta(based_on="cs-0", identity=stale_previous.identity)
    stale_draft = derive_next_turn_state_draft(
        stale_previous,
        delta,
        draft_id="draft-stale",
        next_identity=_next_identity(stale_previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    with repository.transaction():
        repository.append_confirmed_turn_state(stale_previous)
        repository.append_action_result_delta(delta)
        repository.append_confirmed_turn_state(previous)
        repository.upsert_next_turn_state_draft(stale_draft)
    repository.close()

    restarted = SQLiteRepository(database_path)
    with pytest.raises(TurnStateStaleError):
        hydrate_turn_state(restarted, previous.identity.session_id)
    restarted.close()


# --- confirm_next_turn_state staleness (supports 21/22 and draft->state) ----


def test_confirm_next_turn_state_rejects_stale_confirmation() -> None:
    previous = _confirmed_state(confirmed_state_id="cs-1")
    delta = _delta()
    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-1",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    stale_confirmed = _confirmed_state(confirmed_state_id="cs-not-current")
    with pytest.raises(TurnStateStaleError):
        confirm_next_turn_state(
            draft,
            new_confirmed_state_id="cs-2",
            latest_confirmed_state=stale_confirmed,
            source_delta=delta,
            confirmation=_confirmation(),
        )

    confirmed = confirm_next_turn_state(
        draft,
        new_confirmed_state_id="cs-2",
        latest_confirmed_state=previous,
        source_delta=delta,
        confirmation=_confirmation(),
    )
    assert confirmed.previous_confirmed_state_id == "cs-1"
    assert confirmed.self_side == draft.self_side
    assert confirmed.confirmed_state_id == "cs-2"
    assert confirmed.identity == draft.identity


def test_confirm_next_turn_state_rejects_wrong_source_delta() -> None:
    previous = _confirmed_state(confirmed_state_id="cs-1")
    delta = _delta()
    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-1",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    wrong_delta = _delta(delta_id="delta-other")
    with pytest.raises(TurnStateStaleError):
        confirm_next_turn_state(
            draft,
            new_confirmed_state_id="cs-2",
            latest_confirmed_state=previous,
            source_delta=wrong_delta,
            confirmation=_confirmation(),
        )


def test_confirm_next_turn_state_rejects_superseded_confirmed_state() -> None:
    previous = _confirmed_state(confirmed_state_id="cs-1")
    delta = _delta()
    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-1",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    # A confirmed state with the right id but a different identity payload
    # (e.g. it was superseded and re-created) must still fail closed.
    superseded = _confirmed_state(
        confirmed_state_id="cs-1",
        identity=_identity(battle_revision=5),
    )
    with pytest.raises(TurnStateIdentityError):
        confirm_next_turn_state(
            draft,
            new_confirmed_state_id="cs-2",
            latest_confirmed_state=superseded,
            source_delta=delta,
            confirmation=_confirmation(),
        )


# --- 23-26: fixed image evidence --------------------------------------------


def test_evidence_valid_sha_round_trips(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime_evidence"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    runtime = FixedEvidenceRuntime(runtime_root, repo_root)
    metadata = runtime.write_evidence(b"fixed-frame-bytes")
    result = runtime.validate(metadata)
    assert result.is_valid
    assert result.status is EvidenceValidationStatus.VALID


def test_evidence_sha_mismatch_is_reported_invalid(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime_evidence"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    runtime = FixedEvidenceRuntime(runtime_root, repo_root)
    metadata = runtime.write_evidence(b"original-bytes")
    tampered_path = runtime_root / metadata.relative_path
    tampered_path.write_bytes(b"tampered-bytes")

    result = runtime.validate(metadata)
    assert result.status is EvidenceValidationStatus.SHA_MISMATCH
    assert not result.is_valid


def test_evidence_missing_file_is_manual_safe(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime_evidence"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    runtime = FixedEvidenceRuntime(runtime_root, repo_root)
    metadata = runtime.write_evidence(b"will-be-deleted")
    (runtime_root / metadata.relative_path).unlink()

    result = runtime.validate(metadata)
    assert result.status is EvidenceValidationStatus.MISSING
    assert not result.is_valid


def test_evidence_unreadable_is_manual_safe(tmp_path: Path) -> None:
    from maple_next.domain.turn_state import FixedEvidenceMetadata

    runtime_root = tmp_path / "runtime_evidence"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    runtime = FixedEvidenceRuntime(runtime_root, repo_root)
    # A relative_path pointing at a directory is deterministically unreadable
    # as a file on every platform (IsADirectoryError is an OSError).
    (runtime_root / "not-a-file").mkdir(parents=True)
    metadata = FixedEvidenceMetadata(
        evidence_id="ev-1",
        relative_path="not-a-file",
        sha256="0" * 64,
        recorded_at_utc=CONFIRMED_AT,
    )
    result = runtime.validate(metadata)
    assert result.status is EvidenceValidationStatus.UNREADABLE
    assert not result.is_valid


def test_evidence_root_must_be_outside_repository(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    inside = repo_root / "evidence"
    with pytest.raises(ValueError):
        FixedEvidenceRuntime(inside, repo_root)


# --- Provenance chains --------------------------------------------------------


def test_known_provenance_chain_json_round_trip() -> None:
    known = Known.confirmed("PARALYSIS", provenance_chain=(ProvenanceStep.HUMAN_INPUT,))
    payload = known_to_json(known)
    assert payload["provenance_chain"] == ["HUMAN_INPUT"]
    assert known_from_json(payload) == known


def test_field_delta_provenance_chain_json_round_trip() -> None:
    delta = FieldDelta.changed(
        "PARALYSIS",
        provenance_chain=(ProvenanceStep.OCR_CANDIDATE, ProvenanceStep.HUMAN_CORRECTION),
    )
    payload = field_delta_to_json(delta)
    assert payload["provenance_chain"] == ["OCR_CANDIDATE", "HUMAN_CORRECTION"]
    assert field_delta_from_json(payload) == delta


def test_provenance_chain_preserves_ocr_then_human_correction_order() -> None:
    delta = FieldDelta.changed(
        "PARALYSIS",
        provenance_chain=(ProvenanceStep.OCR_CANDIDATE, ProvenanceStep.HUMAN_CORRECTION),
    )
    assert delta.provenance_chain == (
        ProvenanceStep.OCR_CANDIDATE,
        ProvenanceStep.HUMAN_CORRECTION,
    )
    round_tripped = field_delta_from_json(field_delta_to_json(delta))
    assert round_tripped.provenance_chain == delta.provenance_chain


def test_provenance_chain_must_be_non_empty() -> None:
    with pytest.raises(ValueError):
        Known(KnowledgeStatus.CONFIRMED, "x", ())
    with pytest.raises(ValueError):
        FieldDelta(ChangeObservation.CHANGED, "x", ())


def test_changed_field_takes_delta_provenance_chain() -> None:
    previous = _confirmed_state(self_side=_confirmed_side(stage=0))
    ocr_then_correction = (ProvenanceStep.OCR_CANDIDATE, ProvenanceStep.HUMAN_CORRECTION)
    base = _unchanged_side_delta()
    delta_side = SideDelta(
        active=base.active,
        hp_bucket=base.hp_bucket,
        status=base.status,
        attack_stage=FieldDelta.changed(3, provenance_chain=ocr_then_correction),
        defense_stage=base.defense_stage,
        special_attack_stage=base.special_attack_stage,
        special_defense_stage=base.special_defense_stage,
        speed_stage=base.speed_stage,
        accuracy_stage=base.accuracy_stage,
        evasion_stage=base.evasion_stage,
        side_effects=base.side_effects,
    )
    delta = _delta(self_side=delta_side)

    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-prov-changed",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    assert draft.self_side.attack_stage.provenance_chain == ocr_then_correction


def test_unchanged_field_carries_forward_provenance_with_marker() -> None:
    previous = _confirmed_state(self_side=_confirmed_side(active="Dondozo"))
    delta = _delta(self_side=_unchanged_side_delta())

    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-prov-unchanged",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    assert draft.self_side.active.provenance_chain == (
        *previous.self_side.active.provenance_chain,
        ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD,
    )


def test_unknown_field_gets_unknown_provenance() -> None:
    previous = _confirmed_state(self_side=_confirmed_side(active="Dondozo"))
    delta = _delta(self_side=_unknown_side_delta())

    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-prov-unknown",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    assert draft.self_side.active.provenance_chain == (ProvenanceStep.UNKNOWN,)


def test_known_json_missing_provenance_chain_fails_closed() -> None:
    with pytest.raises(TurnStateError):
        known_from_json({"status": "CONFIRMED", "value": "NONE"})
    with pytest.raises(TurnStateError):
        known_from_json({"status": "UNKNOWN"})


def test_field_delta_json_missing_provenance_chain_fails_closed() -> None:
    with pytest.raises(TurnStateError):
        field_delta_from_json({"observation": "UNCHANGED"})
    with pytest.raises(TurnStateError):
        field_delta_from_json({"observation": "CHANGED", "after_value": "x"})


def test_provenance_chain_round_trips_through_sqlite(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "provenance.db")
    state = _confirmed_state()
    with repository.transaction():
        repository.append_confirmed_turn_state(state)
    fetched = repository.get_confirmed_turn_state(state.confirmed_state_id)
    assert fetched.self_side.active.provenance_chain == _HUMAN
    assert fetched.weather.provenance_chain == _HUMAN
    repository.close()


def test_provenance_chain_preserved_across_restart_hydration(tmp_path: Path) -> None:
    database_path = tmp_path / "hydrate_provenance.db"
    repository = SQLiteRepository(database_path)
    previous = _confirmed_state()
    ocr_then_correction = (ProvenanceStep.OCR_CANDIDATE, ProvenanceStep.HUMAN_CORRECTION)
    base = _unchanged_side_delta()
    delta_side = SideDelta(
        active=FieldDelta.changed("Garchomp", provenance_chain=ocr_then_correction),
        hp_bucket=base.hp_bucket,
        status=base.status,
        attack_stage=base.attack_stage,
        defense_stage=base.defense_stage,
        special_attack_stage=base.special_attack_stage,
        special_defense_stage=base.special_defense_stage,
        speed_stage=base.speed_stage,
        accuracy_stage=base.accuracy_stage,
        evasion_stage=base.evasion_stage,
        side_effects=base.side_effects,
    )
    delta = _delta(self_side=delta_side)
    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-prov-restart",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
        repository.append_action_result_delta(delta)
        repository.upsert_next_turn_state_draft(draft)
    repository.close()

    restarted = SQLiteRepository(database_path)
    recovery = hydrate_turn_state(restarted, previous.identity.session_id)
    assert recovery.latest_draft is not None
    assert recovery.latest_draft.self_side.active.provenance_chain == ocr_then_correction
    restarted.close()


# --- Hydration / promotion full-chain corruption ------------------------------


def test_hydration_rejects_draft_with_corrupted_delta_identity(tmp_path: Path) -> None:
    database_path = tmp_path / "hydrate_delta_corrupt.db"
    repository = SQLiteRepository(database_path)
    previous = _confirmed_state()
    good_next_identity = _next_identity(previous.identity)
    corrupted_delta = _delta(identity=_identity(session_id="corrupted-session"))
    draft = NextTurnStateDraft(
        draft_id="draft-delta-corrupt",
        identity=good_next_identity,
        based_on_confirmed_state_id=previous.confirmed_state_id,
        source_delta_id=corrupted_delta.delta_id,
        self_side=previous.self_side,
        opponent_side=previous.opponent_side,
        weather=previous.weather,
        terrain=previous.terrain,
        derived_at_utc=CONFIRMED_AT,
    )
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
        repository.append_action_result_delta(corrupted_delta)
        repository.upsert_next_turn_state_draft(draft)
    repository.close()

    restarted = SQLiteRepository(database_path)
    with pytest.raises(TurnStateIdentityError):
        hydrate_turn_state(restarted, previous.identity.session_id)
    restarted.close()


@pytest.mark.parametrize(
    "field,corrupt_value",
    [
        # session_id is excluded: it is also the persistence-layer partition
        # key (both confirmed-state and draft lookups are scoped by it), so
        # corrupting it makes the row invisible under the original session
        # rather than producing a same-session identity mismatch. That case
        # is exercised directly at the domain layer instead, e.g. in
        # test_delta_session_mismatch_fails_closed.
        ("match_id", "corrupted-match"),
        ("generation", 999),
        ("turn_id", "turn-1"),
        ("turn_number", 99),
        ("battle_revision", 99),
    ],
)
def test_hydration_rejects_corrupted_draft_identity(
    tmp_path: Path, field: str, corrupt_value: object
) -> None:
    database_path = tmp_path / f"hydrate_draft_corrupt_{field}.db"
    repository = SQLiteRepository(database_path)
    previous = _confirmed_state()
    delta = _delta()
    good_next_identity = _next_identity(previous.identity)
    kwargs = _identity_kwargs(good_next_identity)
    kwargs[field] = corrupt_value
    corrupted_identity = TurnIdentity(**kwargs)  # type: ignore[arg-type]
    corrupted_draft = NextTurnStateDraft(
        draft_id="draft-identity-corrupt",
        identity=corrupted_identity,
        based_on_confirmed_state_id=previous.confirmed_state_id,
        source_delta_id=delta.delta_id,
        self_side=previous.self_side,
        opponent_side=previous.opponent_side,
        weather=previous.weather,
        terrain=previous.terrain,
        derived_at_utc=CONFIRMED_AT,
    )
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
        repository.append_action_result_delta(delta)
        repository.upsert_next_turn_state_draft(corrupted_draft)
    repository.close()

    restarted = SQLiteRepository(database_path)
    with pytest.raises(TurnStateIdentityError):
        hydrate_turn_state(restarted, previous.identity.session_id)
    restarted.close()


def test_hydration_rejects_wrong_source_delta_id(tmp_path: Path) -> None:
    """A draft referencing a nonexistent delta id cannot even be persisted:
    the FOREIGN KEY on next_turn_state_drafts.source_delta_id fails closed
    at the storage layer before hydration would ever see it."""

    database_path = tmp_path / "hydrate_wrong_source.db"
    repository = SQLiteRepository(database_path)
    previous = _confirmed_state()
    real_delta = _delta(delta_id="delta-real")
    wrong_source_draft = NextTurnStateDraft(
        draft_id="draft-wrong-source",
        identity=_next_identity(previous.identity),
        based_on_confirmed_state_id=previous.confirmed_state_id,
        source_delta_id="delta-does-not-exist",
        self_side=previous.self_side,
        opponent_side=previous.opponent_side,
        weather=previous.weather,
        terrain=previous.terrain,
        derived_at_utc=CONFIRMED_AT,
    )
    with pytest.raises(sqlite3.IntegrityError), repository.transaction():
        repository.append_confirmed_turn_state(previous)
        repository.append_action_result_delta(real_delta)
        repository.upsert_next_turn_state_draft(wrong_source_draft)
    repository.close()

    # Nothing committed: the whole transaction rolled back.
    restarted = SQLiteRepository(database_path)
    assert restarted.get_latest_confirmed_turn_state(previous.identity.session_id) is None
    restarted.close()


def test_confirm_next_turn_state_rejects_wrong_source_delta_id_string(tmp_path: Path) -> None:
    """A draft's source_delta_id mismatching the delta actually passed to
    confirm_next_turn_state fails closed at the domain layer, independent of
    the persistence-layer FOREIGN KEY covered above."""

    previous = _confirmed_state(confirmed_state_id="cs-1")
    delta = _delta(delta_id="delta-real")
    draft = derive_next_turn_state_draft(
        previous,
        delta,
        draft_id="draft-1",
        next_identity=_next_identity(previous.identity),
        derived_at_utc=CONFIRMED_AT,
    )
    mismatched_source_draft = NextTurnStateDraft(
        draft_id=draft.draft_id,
        identity=draft.identity,
        based_on_confirmed_state_id=draft.based_on_confirmed_state_id,
        source_delta_id="delta-does-not-match",
        self_side=draft.self_side,
        opponent_side=draft.opponent_side,
        weather=draft.weather,
        terrain=draft.terrain,
        derived_at_utc=draft.derived_at_utc,
    )
    with pytest.raises(TurnStateStaleError):
        confirm_next_turn_state(
            mismatched_source_draft,
            new_confirmed_state_id="cs-2",
            latest_confirmed_state=previous,
            source_delta=delta,
            confirmation=_confirmation(),
        )


# --- Evidence path confinement (fail closed) ----------------------------------


def _runtime(tmp_path: Path) -> FixedEvidenceRuntime:
    runtime_root = tmp_path / "runtime_evidence"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return FixedEvidenceRuntime(runtime_root, repo_root)


@pytest.mark.parametrize(
    "evidence_id",
    [
        "../escape",
        "/etc/passwd",
        "C:\\Windows\\System32",
        "a/b",
        "a\\b",
        "",
        "   ",
        ".",
        "..",
    ],
)
def test_write_evidence_rejects_unsafe_evidence_id(tmp_path: Path, evidence_id: str) -> None:
    runtime = _runtime(tmp_path)
    with pytest.raises(EvidencePathError):
        runtime.write_evidence(b"bytes", evidence_id=evidence_id)


@pytest.mark.parametrize(
    "relative_path",
    [
        "../escape.bin",
        "/etc/passwd",
        "C:\\Windows\\System32\\evil.bin",
        "\\\\server\\share\\evil.bin",
        "a/b.bin",
        "a\\b.bin",
        "",
        ".",
        "..",
    ],
)
def test_validate_rejects_unsafe_relative_path(tmp_path: Path, relative_path: str) -> None:
    from maple_next.domain.turn_state import FixedEvidenceMetadata

    runtime = _runtime(tmp_path)
    # An empty/blank relative_path is itself rejected by FixedEvidenceMetadata's
    # own constructor (ValueError) before ever reaching runtime.validate();
    # every other unsafe shape reaches the runtime's own confinement check
    # (EvidencePathError, a ValueError subclass) instead. Both are fail-closed.
    with pytest.raises(ValueError):
        metadata = FixedEvidenceMetadata(
            evidence_id="ev-1",
            relative_path=relative_path,
            sha256="0" * 64,
            recorded_at_utc=CONFIRMED_AT,
        )
        runtime.validate(metadata)


def test_validate_rejects_symlink_root_escape(tmp_path: Path) -> None:
    import hashlib

    from maple_next.domain.turn_state import FixedEvidenceMetadata

    runtime = _runtime(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside-bytes")
    link_path = runtime.runtime_root / "evil.bin"
    try:
        link_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unsupported in this environment: {exc}")

    metadata = FixedEvidenceMetadata(
        evidence_id="ev-1",
        relative_path="evil.bin",
        sha256=hashlib.sha256(b"outside-bytes").hexdigest(),
        recorded_at_utc=CONFIRMED_AT,
    )
    with pytest.raises(EvidencePathError):
        runtime.validate(metadata)


# --- Legal-action boundary adapter --------------------------------------------


def _confirmed_legal_selection(
    *,
    confirmation_id: str,
    identity: TurnIdentity,
    action_type: ActionType,
    action_name: str,
) -> ConfirmedLegalActionSelection:
    return confirm_legal_action_selection(
        confirmation_id=confirmation_id,
        identity=identity,
        action_type=action_type,
        action_name=action_name,
        confirmation=_confirmation(),
    )


def test_legal_action_boundary_rejects_prefill_draft() -> None:
    identity = _identity()
    prefill = LegalActionPrefillDraft(
        prefill_id="pf-1",
        identity=identity,
        based_on_confirmed_state_id="cs-1",
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        derived_at_utc=CONFIRMED_AT,
    )
    with pytest.raises(TurnStateIdentityError):
        build_confirmed_legal_actions_input(identity, (prefill,))  # type: ignore[arg-type]


def test_legal_action_boundary_rejects_unconfirmed_object() -> None:
    identity = _identity()
    with pytest.raises(TurnStateIdentityError):
        build_confirmed_legal_actions_input(identity, ("not-a-selection",))  # type: ignore[arg-type]


def test_legal_action_boundary_rejects_mixed_identity() -> None:
    identity = _identity()
    other_identity = _identity(session_id="other-session")
    selection = _confirmed_legal_selection(
        confirmation_id="conf-1",
        identity=other_identity,
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
    )
    with pytest.raises(TurnStateIdentityError):
        build_confirmed_legal_actions_input(identity, (selection,))


def test_legal_action_boundary_rejects_stale_revision() -> None:
    identity = _identity(battle_revision=1)
    stale_selection = _confirmed_legal_selection(
        confirmation_id="conf-1",
        identity=_identity(battle_revision=0),
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
    )
    with pytest.raises(TurnStateIdentityError):
        build_confirmed_legal_actions_input(identity, (stale_selection,))


def test_legal_action_boundary_classifies_moves_and_switches() -> None:
    identity = _identity()
    move = _confirmed_legal_selection(
        confirmation_id="conf-move",
        identity=identity,
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
    )
    switch = _confirmed_legal_selection(
        confirmation_id="conf-switch",
        identity=identity,
        action_type=ActionType.SWITCH,
        action_name="Urshifu",
    )
    result = build_confirmed_legal_actions_input(identity, (move, switch))
    assert result.legal_moves == ("Wave Crash",)
    assert result.legal_switches == ("Urshifu",)


def test_legal_action_boundary_rejects_duplicate_moves() -> None:
    identity = _identity()
    first = _confirmed_legal_selection(
        confirmation_id="conf-1",
        identity=identity,
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
    )
    duplicate = _confirmed_legal_selection(
        confirmation_id="conf-2",
        identity=identity,
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
    )
    with pytest.raises(ValueError):
        build_confirmed_legal_actions_input(identity, (first, duplicate))


def test_legal_action_boundary_rejects_blank_action_name() -> None:
    identity = _identity()
    selection = _confirmed_legal_selection(
        confirmation_id="conf-blank",
        identity=identity,
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
    )
    object.__setattr__(selection, "action_name", "   ")
    with pytest.raises(ValueError):
        build_confirmed_legal_actions_input(identity, (selection,))


def test_legal_action_boundary_never_imports_provider_or_network_code() -> None:
    """Pure value transform: no provider/OCR/capture import, no request/network code."""

    import ast
    import inspect

    import maple_next.application.turn_legal_action_boundary as boundary_module

    source_path = Path(inspect.getfile(boundary_module))
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any("provider" in module for module in imported_modules)
    assert not any("ocr" in module for module in imported_modules)
    assert not any("capture" in module for module in imported_modules)
    assert "requests" not in imported_modules
    assert "httpx" not in imported_modules


# --- Schema migration: rich_action_completions additive upgrade ---------------


def test_legacy_rich_action_completions_schema_migrates_additively(tmp_path: Path) -> None:
    """A pre-upgrade DB with the old (narrower) rich_action_completions table
    migrates additively and idempotently, without guessed backfill."""

    database_path = tmp_path / "legacy_rac_schema.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            schema_version INTEGER NOT NULL
        );
        INSERT INTO schema_meta(singleton_id, schema_version) VALUES (1, 13);

        CREATE TABLE rich_action_completions (
            transaction_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL UNIQUE,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            own_action_type TEXT NOT NULL,
            own_action_name TEXT NOT NULL,
            opponent_action_type TEXT NOT NULL,
            opponent_action_name TEXT NOT NULL,
            action_order TEXT NOT NULL,
            delta_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        INSERT INTO rich_action_completions VALUES (
            'rac-legacy', 's-legacy', 't-legacy', 1, 'MOVE', 'Tackle',
            'MOVE', 'Tackle', 'UNKNOWN', 'delta-legacy', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    migrate(sqlite3.connect(database_path))
    reopened = sqlite3.connect(database_path)
    reopened.row_factory = sqlite3.Row
    row = reopened.execute(
        "SELECT * FROM rich_action_completions WHERE transaction_id = 'rac-legacy'"
    ).fetchone()
    assert row["session_id"] == "s-legacy"
    assert row["match_id"] is None
    assert row["based_on_confirmed_state_id"] is None
    version = reopened.execute(
        "SELECT schema_version FROM schema_meta WHERE singleton_id = 1"
    ).fetchone()[0]
    assert version == SCHEMA_VERSION

    # Idempotent: migrating again does not error or duplicate columns.
    migrate(reopened)
    migrate(sqlite3.connect(database_path))
    reopened.close()
