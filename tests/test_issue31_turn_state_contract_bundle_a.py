"""Issue #31 Bundle A: Turn state / action-result delta / next-draft contract.

Focused tests for the new, additive domain + persistence + runtime-evidence
slice. Legacy Turn flow regression lives in ``test_turn_lifecycle.py`` and is
run alongside this file, unmodified.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from maple_next.application.turn_state_recovery import hydrate_turn_state
from maple_next.domain.enums import ActionOrder, ActionType, HpBucket
from maple_next.domain.models import BattleTurn, TurnFactsSnapshot
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ChangeObservation,
    ConfirmationMeta,
    ConfirmedTurnState,
    FieldDelta,
    KnowledgeStatus,
    Known,
    LegalActionPrefillDraft,
    NextTurnStateDraft,
    SideDelta,
    SideState,
    TurnIdentity,
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
    EvidenceValidationStatus,
    FixedEvidenceRuntime,
)
from maple_next.persistence.schema import SCHEMA_VERSION, migrate
from maple_next.persistence.sqlite import SQLiteRepository

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


def _confirmed_side(*, active: str = "Dondozo", stage: int = 0) -> SideState:
    return SideState(
        active=Known.confirmed(active),
        hp_bucket=Known.confirmed(HpBucket.FULL),
        status=Known.confirmed("NONE"),
        attack_stage=Known.confirmed(stage),
        defense_stage=Known.confirmed(stage),
        special_attack_stage=Known.confirmed(stage),
        special_defense_stage=Known.confirmed(stage),
        speed_stage=Known.confirmed(stage),
        accuracy_stage=Known.confirmed(stage),
        evasion_stage=Known.confirmed(stage),
        side_effects=Known.confirmed(()),
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
        attack_stage=FieldDelta.changed(value),
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
        weather=Known.confirmed("NONE"),
        terrain=Known.confirmed("NONE"),
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
    confirmed_none_text = Known.confirmed("NONE")
    confirmed_unknown_text = Known.confirmed("UNKNOWN")

    assert unknown.status is KnowledgeStatus.UNKNOWN
    assert confirmed_none_text.status is KnowledgeStatus.CONFIRMED
    assert confirmed_none_text.value == "NONE"
    assert confirmed_unknown_text.status is KnowledgeStatus.CONFIRMED
    assert confirmed_unknown_text.value == "UNKNOWN"
    assert unknown != confirmed_unknown_text

    changed = FieldDelta.changed("PARALYSIS")
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

    with repository.transaction():
        repository.record_rich_action_completion(
            transaction_id="rac-1",
            session_id=previous.identity.session_id,
            turn_id=previous.identity.turn_id,
            turn_number=previous.identity.turn_number,
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
    repository.close()


def test_failed_action_delta_transaction_commits_neither(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "atomic_fail.db")
    previous = _confirmed_state()
    with repository.transaction():
        repository.append_confirmed_turn_state(previous)
    delta = _delta()

    with pytest.raises(sqlite3.IntegrityError), repository.transaction():
        repository.record_rich_action_completion(
            transaction_id="rac-1",
            session_id=previous.identity.session_id,
            turn_id=previous.identity.turn_id,
            turn_number=previous.identity.turn_number,
            own_action_type=ActionType.MOVE,
            own_action_name="Wave Crash",
            opponent_action_type=ActionType.MOVE,
            opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
            delta=delta,
        )
        # Same turn_id violates the UNIQUE constraint -> whole transaction rolls back.
        repository.record_rich_action_completion(
            transaction_id="rac-2",
            session_id=previous.identity.session_id,
            turn_id=previous.identity.turn_id,
            turn_number=previous.identity.turn_number,
            own_action_type=ActionType.MOVE,
            own_action_name="Protect",
            opponent_action_type=ActionType.MOVE,
            opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
            delta=_delta(delta_id="delta-2"),
        )

    with pytest.raises(KeyError):
        repository.get_action_result_delta("delta-1")
    assert repository.get_rich_action_completion_by_turn(previous.identity.turn_id) is None
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

    assert draft.self_side.attack_stage == Known.confirmed(3)


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

    assert draft.self_side.attack_stage == Known.confirmed(2)
    assert draft.self_side.active == previous.self_side.active


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
    changed = FieldDelta.changed("PARALYSIS")
    payload = field_delta_to_json(changed)
    assert field_delta_from_json(payload) == changed

    known = Known.confirmed("NONE")
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
    with pytest.raises(TurnStateStaleError):
        confirm_next_turn_state(
            draft,
            new_confirmed_state_id="cs-2",
            latest_confirmed_state_id="cs-not-current",
            confirmation=_confirmation(),
        )

    confirmed = confirm_next_turn_state(
        draft,
        new_confirmed_state_id="cs-2",
        latest_confirmed_state_id="cs-1",
        confirmation=_confirmation(),
    )
    assert confirmed.previous_confirmed_state_id == "cs-1"
    assert confirmed.self_side == draft.self_side


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
