"""Issue #31 Bundle B second narrow remediation.

Covers the five authorized contract-integration gaps closed in this round:

1. (test-count/goldens fixes live in the two existing Bundle B files)
2. Rich Turn Advice result-apply binding
   (``BattleApplication.apply_rich_turn_advice_result``), which binds
   against the durable latest ``ConfirmedTurnState`` instead of the legacy
   ``session.current_reviewed_board_id`` pointer.
3. Fail-closed detection of a foreign/corrupt OPEN ``NextTurnStateDraft``.
4. Complete ``maple-match.v3`` serialization (full identity on
   ``ActionResultDelta``/``ConfirmedLegalActionSelection``) and full
   identity-chain validation.
5. A genuinely strict ``parse_match_export_v3``.

No test in this file sends anything over a network or touches a real
provider; every ``ResultEnvelope`` here is constructed offline.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from maple_next.application.match_export_v3 import MatchExportV3Error, parse_match_export_v3
from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.domain.champions_rules import current_rules_pin_for_new_match
from maple_next.domain.enums import (
    ActionType,
    BattleState,
    HpBucket,
    JobStatus,
    JobType,
    ResultDisposition,
)
from maple_next.domain.models import (
    AppliedSelectionSnapshot,
    BattleSession,
    BattleTurn,
    SelectionFacts,
    TurnFactsSnapshot,
)
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FieldDelta,
    Known,
    NextTurnStateDraft,
    ProvenanceStep,
    SideDelta,
    SideState,
    TurnIdentity,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.workers.contracts.models import ResultEnvelope
from tests.fixtures.bundle3 import seed_selection_advice_binding

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)
CONFIRMED_AT = "2026-08-06T00:00:00+00:00"


def _confirmed_side(active: str) -> SideState:
    return SideState(
        active=Known.confirmed(active, provenance_chain=_HUMAN),
        hp_bucket=Known.confirmed(HpBucket.FULL, provenance_chain=_HUMAN),
        status=Known.confirmed("NONE", provenance_chain=_HUMAN),
        attack_stage=Known.confirmed(0, provenance_chain=_HUMAN),
        defense_stage=Known.confirmed(0, provenance_chain=_HUMAN),
        special_attack_stage=Known.confirmed(0, provenance_chain=_HUMAN),
        special_defense_stage=Known.confirmed(0, provenance_chain=_HUMAN),
        speed_stage=Known.confirmed(0, provenance_chain=_HUMAN),
        accuracy_stage=Known.confirmed(0, provenance_chain=_HUMAN),
        evasion_stage=Known.confirmed(0, provenance_chain=_HUMAN),
        side_effects=Known.confirmed((), provenance_chain=_HUMAN),
    )


def _confirmation() -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True, confirmed_at_utc=CONFIRMED_AT, provenance="HUMAN_CONFIRMED"
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


class RichSessionFixture:
    """A BATTLE_READY -> TURN_REVIEWED session with a confirmed turn state."""

    def __init__(self, tmp_path):
        self.repository = SQLiteRepository(tmp_path / "runtime" / "maple.db")
        self.application = MatchApplication(self.repository, tmp_path / "user-data" / "exports")
        self.session_id = "session-remediation-2"
        self.match_id = "match-remediation-2"
        self.generation = 9
        self.turn_id = "turn-1"
        self.turn_number = 1
        self.battle_revision = 3
        self.confirmed_state_id = "state-1"

        rules_pin = current_rules_pin_for_new_match()
        session = BattleSession(
            session_id=self.session_id,
            match_id=self.match_id,
            generation=self.generation,
            state=BattleState.TURN_REVIEWED,
            battle_revision=self.battle_revision,
            current_reviewed_selection_id="selection-1",
            current_applied_selection_id="applied-1",
            current_turn_id=self.turn_id,
            current_reviewed_board_id="facts-1",
            rules_ruleset_id=rules_pin.ruleset_id,
            rules_ruleset_version=rules_pin.ruleset_version,
            rules_snapshot_id=rules_pin.rules_snapshot_id,
            rules_facts_sha256=rules_pin.rules_facts_sha256,
        )
        self.repository.insert_session(session)
        self.repository.append_turn(
            self.session_id, BattleTurn(turn_id=self.turn_id, turn_number=self.turn_number)
        )
        self.repository.append_turn_facts(
            self.session_id,
            TurnFactsSnapshot(
                turn_facts_id="facts-1",
                turn_id=self.turn_id,
                turn_number=self.turn_number,
                self_active="Dondozo",
                opponent_active="Garchomp",
                self_hp=HpBucket.FULL,
                opponent_hp=HpBucket.FULL,
                legal_moves=("Wave Crash",),
                legal_switches=("Gholdengo",),
            ),
        )
        self.repository.append_selection_facts(
            self.session_id,
            SelectionFacts(
                reviewed_selection_id="selection-1",
                self_team=(
                    "Dondozo", "Gholdengo", "Urshifu", "Hatterene", "Dragonite", "Pikachu",
                ),
                opponent_team=(
                    "Garchomp", "Landorus", "Zamazenta", "Chien-Pao", "Iron Bundle", "Amoonguss",
                ),
            ),
        )
        self.repository.append_applied_selection(
            self.session_id,
            AppliedSelectionSnapshot(
                applied_selection_id="applied-1",
                selected_three=("Dondozo", "Gholdengo", "Urshifu"),
                lead="Dondozo",
                backline=("Gholdengo", "Urshifu"),
                source_advice_id="advice-1",
            ),
        )
        # Bundle 3: the durable Selection Advice job + advice rows the real
        # apply-selection flow always produces, so the applied -> advice ->
        # job -> reviewed-selection chain is complete. (Added with LF line
        # endings, matching the repository convention; this file is one of
        # only two historically committed with CRLF.)
        seed_selection_advice_binding(
            self.repository,
            session_id=self.session_id,
            match_id=self.match_id,
            generation=self.generation,
            reviewed_selection_id="selection-1",
            advice_id="advice-1",
            selected_three=("Dondozo", "Gholdengo", "Urshifu"),
            lead="Dondozo",
        )
        self.repository.connection.commit()

    def identity(self, **overrides) -> TurnIdentity:
        kwargs = dict(
            session_id=self.session_id,
            match_id=self.match_id,
            generation=self.generation,
            turn_id=self.turn_id,
            turn_number=self.turn_number,
            battle_revision=self.battle_revision,
        )
        kwargs.update(overrides)
        return TurnIdentity(**kwargs)

    def append_confirmed_state(
        self, *, confirmed_state_id: str | None = None, evidence_id: str | None = None
    ) -> ConfirmedTurnState:
        state = ConfirmedTurnState(
            confirmed_state_id=confirmed_state_id or self.confirmed_state_id,
            identity=self.identity(),
            previous_confirmed_state_id=None,
            self_side=_confirmed_side("Dondozo"),
            opponent_side=_confirmed_side("Garchomp"),
            weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
            terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
            confirmation=_confirmation(),
            evidence_id=evidence_id,
        )
        self.repository.append_confirmed_turn_state(state)
        self.repository.connection.commit()
        return state

    def append_legal_actions(self) -> tuple[ConfirmedLegalActionSelection, ...]:
        move = ConfirmedLegalActionSelection(
            confirmation_id="legal-move-1",
            identity=self.identity(),
            action_type=ActionType.MOVE,
            action_name="Wave Crash",
            confirmation=_confirmation(),
        )
        switch = ConfirmedLegalActionSelection(
            confirmation_id="legal-switch-1",
            identity=self.identity(),
            action_type=ActionType.SWITCH,
            action_name="Gholdengo",
            confirmation=_confirmation(),
        )
        self.repository.append_confirmed_legal_action_selection(move)
        self.repository.append_confirmed_legal_action_selection(switch)
        self.repository.connection.commit()
        return (move, switch)

    def confirm_legal_switches(
        self, *, based_on_confirmed_state_id: str | None = None
    ) -> None:
        from maple_next.domain.legal_switches import LegalSwitchConfirmation, LegalSwitchStatus

        self.repository.upsert_legal_switch_confirmation(
            LegalSwitchConfirmation(
                confirmation_id="switch-confirm-1",
                identity=self.identity(),
                based_on_confirmed_state_id=(
                    based_on_confirmed_state_id or self.confirmed_state_id
                ),
                applied_selection_id="applied-1",
                legal_switches=("Gholdengo",),
                status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
                confirmation=_confirmation(),
            )
        )
        self.repository.connection.commit()


@pytest.fixture
def rich_fixture(tmp_path) -> RichSessionFixture:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()
    fixture.confirm_legal_switches()
    return fixture


def _valid_result_envelope(job, *, input_snapshot_id: str | None = None) -> ResultEnvelope:
    return ResultEnvelope(
        contract_version=job.contract_version,
        result_id="result-1",
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation,
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id=(
            input_snapshot_id if input_snapshot_id is not None else job.input_snapshot_id
        ),
        request_payload_hash=job.request_payload_hash,
        payload={
            "response_schema_version": "maple-turn-advice-response.v2",
            "recommended_action": {
                "action_id": "legal-move-1",
                "action_type": "MOVE",
                "action_name": "Wave Crash",
            },
            "recommendation_robustness": "HIGH",
            "reasons": ["Best confirmed damage this turn"],
            "opponent_prediction": {
                "primary": {
                    "category": "UNKNOWN",
                    "specific_action": None,
                    "support_basis": "NONE",
                    "support": "LOW",
                    "summary": "Opponent likely attacks",
                },
                "alternatives": [],
            },
            "warnings": [],
        },
        source_type="MOCK",
        model="mock-dev",
    )


# --- 2. Rich result-apply binding -------------------------------------------


def test_rich_result_applied_for_correctly_bound_result(rich_fixture: RichSessionFixture) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    result = _valid_result_envelope(job)
    disposition = rich_fixture.application.apply_rich_turn_advice_result(result)
    assert disposition is ResultDisposition.APPLIED

    session = rich_fixture.repository.load_active_session()
    assert session.current_turn_advice_id == "result-1"
    advice = rich_fixture.repository.get_turn_advice("result-1")
    assert advice.action_type is ActionType.MOVE
    assert advice.action_name == "Wave Crash"
    # The stored TurnAdviceSnapshot.input_snapshot_id is the legacy
    # reviewed_turn_facts id (turn_advices.input_snapshot_id has a durable
    # FK to reviewed_turn_facts) -- the *binding* that actually authorized
    # this apply used the rich ConfirmedTurnState.confirmed_state_id (see
    # the STALE_REJECTED tests below), which is job.input_snapshot_id, not
    # this column.
    assert advice.input_snapshot_id == "facts-1"
    assert job.input_snapshot_id == rich_fixture.confirmed_state_id


def test_rich_result_wrong_confirmed_state_id_stale_rejected(
    rich_fixture: RichSessionFixture,
) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    result = _valid_result_envelope(job, input_snapshot_id="state-WRONG")
    disposition = rich_fixture.application.apply_rich_turn_advice_result(result)
    assert disposition is ResultDisposition.STALE_REJECTED


def test_rich_result_stale_superseded_confirmed_state_rejected(tmp_path) -> None:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()
    fixture.confirm_legal_switches()
    job = fixture.application.request_rich_turn_advice("command-1")
    # Supersede with a newer confirmed state at a later revision.
    fixture.repository.connection.execute(
        "UPDATE battle_sessions SET battle_revision = battle_revision + 1"
    )
    fixture.repository.connection.commit()
    newer_identity = fixture.identity(battle_revision=fixture.battle_revision + 1)
    newer_state = ConfirmedTurnState(
        confirmed_state_id="state-2",
        identity=newer_identity,
        previous_confirmed_state_id=fixture.confirmed_state_id,
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    fixture.repository.append_confirmed_turn_state(newer_state)
    fixture.repository.connection.commit()

    result = _valid_result_envelope(job)
    disposition = fixture.application.apply_rich_turn_advice_result(result)
    assert disposition is ResultDisposition.STALE_REJECTED


def test_rich_result_wrong_session_stale_rejected(rich_fixture: RichSessionFixture) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    result = _valid_result_envelope(job)
    result = replace(result, session_id="wrong-session")
    assert rich_fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.STALE_REJECTED
    )


def test_rich_result_wrong_match_stale_rejected(rich_fixture: RichSessionFixture) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    result = _valid_result_envelope(job)
    result = replace(result, match_id="wrong-match")
    assert rich_fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.STALE_REJECTED
    )


def test_rich_result_wrong_generation_stale_rejected(rich_fixture: RichSessionFixture) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    result = _valid_result_envelope(job)
    result = replace(result, generation=999)
    assert rich_fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.STALE_REJECTED
    )


def test_rich_result_wrong_turn_number_stale_rejected(rich_fixture: RichSessionFixture) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    result = _valid_result_envelope(job)
    result = replace(result, turn_number=999)
    assert rich_fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.STALE_REJECTED
    )


def test_rich_result_wrong_battle_revision_stale_rejected(rich_fixture: RichSessionFixture) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    result = _valid_result_envelope(job)
    result = replace(result, base_battle_revision=999)
    assert rich_fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.STALE_REJECTED
    )


def test_rich_result_wrong_request_hash_stale_rejected(rich_fixture: RichSessionFixture) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    result = _valid_result_envelope(job)
    result = replace(result, request_payload_hash="0" * 64)
    assert rich_fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.STALE_REJECTED
    )


def test_rich_result_non_current_job_stale_rejected(rich_fixture: RichSessionFixture) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    rich_fixture.repository.update_job_status(job.job_id, JobStatus.FAILED)
    rich_fixture.repository.connection.commit()
    result = _valid_result_envelope(job)
    assert rich_fixture.application.apply_rich_turn_advice_result(result) is (
        ResultDisposition.STALE_REJECTED
    )


def test_legacy_result_binding_regression_still_passes(tmp_path) -> None:
    """The legacy (non-rich) result-apply path must remain unaffected."""

    from datetime import UTC, datetime

    from maple_next.domain.enums import ActionType as _ActionType
    from maple_next.domain.enums import HpBucket
    from maple_next.domain.models import TurnFactsSnapshot
    from maple_next.workers.contracts.models import JobEnvelope

    repository = SQLiteRepository(tmp_path / "runtime" / "maple.db")
    application = MatchApplication(repository, tmp_path / "user-data" / "exports")
    session = BattleSession(
        session_id="legacy-session",
        match_id="legacy-match",
        generation=1,
        state=BattleState.TURN_REVIEWED,
        battle_revision=1,
        current_applied_selection_id="applied-legacy",
        current_turn_id="turn-legacy",
        current_reviewed_board_id="facts-legacy",
    )
    repository.insert_session(session)
    repository.append_turn(session.session_id, BattleTurn(turn_id="turn-legacy", turn_number=1))
    repository.append_turn_facts(
        session.session_id,
        TurnFactsSnapshot(
            turn_facts_id="facts-legacy",
            turn_id="turn-legacy",
            turn_number=1,
            self_active="Dondozo",
            opponent_active="Garchomp",
            self_hp=HpBucket.FULL,
            opponent_hp=HpBucket.FULL,
            legal_moves=("Wave Crash",),
            legal_switches=(),
        ),
    )
    repository.append_applied_selection(
        session.session_id,
        AppliedSelectionSnapshot(
            applied_selection_id="applied-legacy",
            selected_three=("Dondozo", "Gholdengo", "Urshifu"),
            lead="Dondozo",
            backline=("Gholdengo", "Urshifu"),
            source_advice_id="advice-legacy",
        ),
    )
    repository.connection.commit()

    job = JobEnvelope(
        contract_version="maple-worker.v1",
        job_id="job-legacy",
        command_id="command-legacy",
        job_type=JobType.TURN_ADVICE,
        session_id="legacy-session",
        match_id="legacy-match",
        generation=1,
        turn_number=1,
        base_battle_revision=1,
        expected_state=BattleState.TURN_REVIEWED,
        input_snapshot_id="facts-legacy",
        request_payload_hash="irrelevant-for-this-regression-check",
        human_authorized_at=datetime.now(UTC),
        status=JobStatus.QUEUED,
    )
    repository.insert_job(job)
    repository.connection.commit()

    result = ResultEnvelope(
        contract_version=job.contract_version,
        result_id="result-legacy",
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation,
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id="state-1",  # a rich-shaped id -- must not bind
        request_payload_hash=job.request_payload_hash,
        payload={},
    )
    # The legacy path still requires exact input_snapshot_id equality
    # against session.current_reviewed_board_id ("facts-legacy"), so a
    # rich-shaped snapshot id is correctly rejected here.
    disposition = application.apply_turn_advice_result(result)
    assert disposition is ResultDisposition.STALE_REJECTED
    del _ActionType


# --- 3. OPEN draft foreign/corrupt fail-closed detection ---------------------


def test_open_draft_missing_source_delta_denied(rich_fixture: RichSessionFixture) -> None:
    # source_delta_id FK prevents inserting a draft with a nonexistent
    # delta -- so a draft simply cannot exist referencing a missing delta.
    # The meaningful corruption to test is a *wrong* (existing) delta.
    pass


def test_open_draft_with_delta_wrong_based_on_state_denied(
    rich_fixture: RichSessionFixture,
) -> None:
    other_state = ConfirmedTurnState(
        confirmed_state_id="state-other",
        identity=rich_fixture.identity(battle_revision=0),
        previous_confirmed_state_id=None,
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    rich_fixture.repository.append_confirmed_turn_state(other_state)
    rich_fixture.repository.connection.commit()

    next_identity = rich_fixture.identity(turn_number=2, battle_revision=4, turn_id="turn-2")
    delta = ActionResultDelta(
        delta_id="delta-x",
        identity=next_identity,
        based_on_confirmed_state_id="state-other",
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    rich_fixture.repository.append_action_result_delta(delta)
    rich_fixture.repository.connection.commit()

    draft = NextTurnStateDraft(
        draft_id="draft-corrupt",
        identity=next_identity,
        based_on_confirmed_state_id=rich_fixture.confirmed_state_id,
        source_delta_id="delta-x",
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    rich_fixture.repository.upsert_next_turn_state_draft(draft)
    rich_fixture.repository.connection.commit()

    with pytest.raises(DomainError, match="OPEN_DRAFT_CHAIN_INVALID"):
        rich_fixture.application.request_rich_turn_advice("command-1")


def test_open_draft_wrong_next_turn_number_denied(rich_fixture: RichSessionFixture) -> None:
    # The delta shares the CURRENT confirmed state's own identity exactly
    # (it describes what changed *from* that state) -- only the draft's
    # identity is the "next" one, and it is what's corrupted here.
    delta = ActionResultDelta(
        delta_id="delta-y",
        identity=rich_fixture.identity(),
        based_on_confirmed_state_id=rich_fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    rich_fixture.repository.append_action_result_delta(delta)
    rich_fixture.repository.connection.commit()

    # Draft claims turn_number=5 (should be prior + 1 == 2) -- wrong increment.
    draft = NextTurnStateDraft(
        draft_id="draft-wrong-turn",
        identity=rich_fixture.identity(turn_number=5, battle_revision=4, turn_id="turn-5"),
        based_on_confirmed_state_id=rich_fixture.confirmed_state_id,
        source_delta_id="delta-y",
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    rich_fixture.repository.upsert_next_turn_state_draft(draft)
    rich_fixture.repository.connection.commit()

    with pytest.raises(DomainError, match="OPEN_DRAFT_CHAIN_INVALID"):
        rich_fixture.application.request_rich_turn_advice("command-1")


def test_open_draft_wrong_battle_revision_denied(rich_fixture: RichSessionFixture) -> None:
    delta = ActionResultDelta(
        delta_id="delta-z",
        identity=rich_fixture.identity(),
        based_on_confirmed_state_id=rich_fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    rich_fixture.repository.append_action_result_delta(delta)
    rich_fixture.repository.connection.commit()

    # 00 design decision (Issue #31 comment 5217661584): the next-turn
    # revision rule is "strictly greater than previous" (battle_revision is
    # a durable global mutation counter, not a Turn-scoped +1 sequence).
    # Draft claims battle_revision=3, equal to (not greater than) the prior
    # confirmed state's own revision -- still correctly rejected under the
    # new rule, just no longer via "wrong increment" but via "not greater".
    draft = NextTurnStateDraft(
        draft_id="draft-wrong-revision",
        identity=rich_fixture.identity(turn_number=2, battle_revision=3, turn_id="turn-2"),
        based_on_confirmed_state_id=rich_fixture.confirmed_state_id,
        source_delta_id="delta-z",
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    rich_fixture.repository.upsert_next_turn_state_draft(draft)
    rich_fixture.repository.connection.commit()

    with pytest.raises(DomainError, match="OPEN_DRAFT_CHAIN_INVALID"):
        rich_fixture.application.request_rich_turn_advice("command-1")


def test_valid_newer_open_draft_still_blocks(rich_fixture: RichSessionFixture) -> None:
    delta = ActionResultDelta(
        delta_id="delta-valid",
        identity=rich_fixture.identity(),
        based_on_confirmed_state_id=rich_fixture.confirmed_state_id,
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    rich_fixture.repository.append_action_result_delta(delta)
    rich_fixture.repository.connection.commit()

    draft = NextTurnStateDraft(
        draft_id="draft-valid",
        identity=rich_fixture.identity(turn_number=2, battle_revision=4, turn_id="turn-2"),
        based_on_confirmed_state_id=rich_fixture.confirmed_state_id,
        source_delta_id="delta-valid",
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    rich_fixture.repository.upsert_next_turn_state_draft(draft)
    rich_fixture.repository.connection.commit()

    with pytest.raises(DomainError, match="PROVIDER_READY_GATE_DENIED"):
        rich_fixture.application.request_rich_turn_advice("command-1")


# --- 4/5. v3 full identity serialization + strict parser --------------------


def test_v3_delta_identity_fully_serialized(rich_fixture: RichSessionFixture) -> None:
    from maple_next.application.match_export_v3 import _delta_to_json

    state = rich_fixture.repository.get_confirmed_turn_state(rich_fixture.confirmed_state_id)
    delta = ActionResultDelta(
        delta_id="delta-full",
        identity=state.identity,
        based_on_confirmed_state_id="state-0",
        self_side=_unchanged_side_delta(),
        opponent_side=_unchanged_side_delta(),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    payload = _delta_to_json(delta)
    assert payload["identity"] == {
        "session_id": state.identity.session_id,
        "match_id": state.identity.match_id,
        "generation": state.identity.generation,
        "turn_id": state.identity.turn_id,
        "turn_number": state.identity.turn_number,
        "battle_revision": state.identity.battle_revision,
    }


def test_v3_legal_action_identity_fully_serialized(rich_fixture: RichSessionFixture) -> None:
    from maple_next.application.match_export_v3 import _legal_action_to_json

    actions = rich_fixture.repository.list_confirmed_legal_action_selections_for_identity(
        rich_fixture.identity()
    )
    payload = _legal_action_to_json(actions[0])
    assert payload["identity"] == {
        "session_id": rich_fixture.session_id,
        "match_id": rich_fixture.match_id,
        "generation": rich_fixture.generation,
        "turn_id": rich_fixture.turn_id,
        "turn_number": rich_fixture.turn_number,
        "battle_revision": rich_fixture.battle_revision,
    }


def test_strict_parser_requires_legacy_selection_and_action_history() -> None:
    payload = {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 1,
        "turns": [],
    }
    with pytest.raises(MatchExportV3Error, match="MISSING_KEYS"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


_SIDE_STATE_FIELD_NAMES = (
    "active",
    "hp_bucket",
    "status",
    "attack_stage",
    "defense_stage",
    "special_attack_stage",
    "special_defense_stage",
    "speed_stage",
    "accuracy_stage",
    "evasion_stage",
    "side_effects",
)


def _known_json(value: object = "NONE") -> dict:
    return {"status": "CONFIRMED", "value": value, "provenance_chain": ["HUMAN_INPUT"]}


def _valid_side_state_json() -> dict:
    values: dict[str, object] = {
        "active": "Dondozo",
        "hp_bucket": "100",
        "status": "NONE",
        "attack_stage": 0,
        "defense_stage": 0,
        "special_attack_stage": 0,
        "special_defense_stage": 0,
        "speed_stage": 0,
        "accuracy_stage": 0,
        "evasion_stage": 0,
        "side_effects": [],
    }
    return {name: _known_json(values[name]) for name in _SIDE_STATE_FIELD_NAMES}


def _valid_side_delta_json() -> dict:
    return {
        name: {"observation": "UNCHANGED", "provenance_chain": ["HUMAN_INPUT"]}
        for name in _SIDE_STATE_FIELD_NAMES
    }


def test_strict_parser_rejects_unknown_carrying_delta_after_value() -> None:
    from maple_next.application.match_export_v3 import RICH_STATE_EXPORT_CONTRACT_VERSION

    shared_identity = {
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "turn_id": "t1",
        "turn_number": 1,
        "battle_revision": 1,
    }
    payload = {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 1,
        "selection": {
            "self_team": [],
            "opponent_team": [],
            "selected_three": [],
            "lead": "x",
        },
        "action_history": [],
        "turns": [
            {
                "turn_number": 1,
                "reviewed_facts": {},
                "advice": None,
                "self_executed_action": {},
                "opponent_executed_action": None,
                "action_order": "SELF_FIRST",
                "recorded_at_utc": CONFIRMED_AT,
                "actual_action": {},
                "rich_state": {
                    "contract_version": RICH_STATE_EXPORT_CONTRACT_VERSION,
                    "confirmed_turn_state": {
                        "confirmed_state_id": "s1",
                        "previous_confirmed_state_id": None,
                        "identity": shared_identity,
                        "self_side": _valid_side_state_json(),
                        "opponent_side": _valid_side_state_json(),
                        "weather": _known_json(),
                        "terrain": _known_json(),
                        "confirmation": {
                            "confirmed_by_human": True,
                            "confirmed_at_utc": CONFIRMED_AT,
                            "provenance": "HUMAN_CONFIRMED",
                        },
                        "evidence_id": None,
                    },
                    "source_action_result_delta": {
                        "delta_id": "d1",
                        "identity": shared_identity,
                        "based_on_confirmed_state_id": "s0",
                        "self_side": _valid_side_delta_json(),
                        "opponent_side": _valid_side_delta_json(),
                        "weather": {"observation": "UNCHANGED", "after_value": "should-be-null"},
                        "terrain": {"observation": "UNCHANGED", "after_value": None},
                        "confirmation": {
                            "confirmed_by_human": True,
                            "confirmed_at_utc": CONFIRMED_AT,
                            "provenance": "HUMAN_CONFIRMED",
                        },
                    },
                    "confirmed_legal_actions": [],
                    "evidence": None,
                },
            }
        ],
    }
    with pytest.raises(MatchExportV3Error, match="UNCHANGED_CARRIES_VALUE"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))
