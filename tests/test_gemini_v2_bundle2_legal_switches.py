"""Gemini V2 Bundle 2: explicit legal-switch confirmation status.

Closes the historical Battle-1 defect where ``legal_switches = []`` appeared
on every real Turn 1-7 request even though the selected roster had two
backline members -- "not yet captured/confirmed" must never be
indistinguishable from "confirmed zero". Domain/persistence/application/gate
level coverage; no provider dispatch anywhere in this file (every assertion
below is either a pure function call, a repository-backed application
command, or a direct :func:`evaluate_provider_ready_gate` call -- nothing
here builds a ``JobEnvelope`` or touches a transport).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.domain.enums import ActionType, BattleState, HpBucket
from maple_next.domain.legal_switches import (
    LegalSwitchConfirmation,
    LegalSwitchError,
    LegalSwitchStatus,
    confirm_legal_switches,
    derive_legal_switch_candidates,
)
from maple_next.domain.models import (
    AppliedSelectionSnapshot,
    BattleSession,
    BattleTurn,
    SelectionFacts,
    TurnFactsSnapshot,
)
from maple_next.domain.turn_state import (
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    Known,
    PokemonLocalMemory,
    ProvenanceStep,
    SideState,
    TurnIdentity,
)
from maple_next.domain.turn_state_projection import GateDenialReason, evaluate_provider_ready_gate
from maple_next.persistence.sqlite import SQLiteRepository

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)
CONFIRMED_AT = "2026-08-15T00:00:00+00:00"


def _confirmation() -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True, confirmed_at_utc=CONFIRMED_AT, provenance="HUMAN_INPUT"
    )


def _identity(**overrides: object) -> TurnIdentity:
    kwargs: dict[str, object] = dict(
        session_id="session-b2",
        match_id="match-b2",
        generation=9,
        turn_id="turn-1",
        turn_number=1,
        battle_revision=1,
    )
    kwargs.update(overrides)
    return TurnIdentity(**kwargs)  # type: ignore[arg-type]


def _applied(**overrides: object) -> AppliedSelectionSnapshot:
    kwargs: dict[str, object] = dict(
        applied_selection_id="applied-b2-1",
        selected_three=("マスカーニャ", "ハッサム", "ガブリアス"),
        lead="マスカーニャ",
        backline=("ハッサム", "ガブリアス"),
        source_advice_id="advice-b2-1",
    )
    kwargs.update(overrides)
    return AppliedSelectionSnapshot(**kwargs)  # type: ignore[arg-type]


def _memory(name: str, hp: Known[HpBucket]) -> PokemonLocalMemory:
    return PokemonLocalMemory(pokemon_name=name, hp_bucket=hp, status=Known.unknown())


def _fainted(name: str) -> PokemonLocalMemory:
    return _memory(name, Known.confirmed(HpBucket.ZERO, provenance_chain=_HUMAN))


def _healthy(name: str) -> PokemonLocalMemory:
    return _memory(name, Known.confirmed(HpBucket.FULL, provenance_chain=_HUMAN))


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


def _legal_action(identity: TurnIdentity) -> ConfirmedLegalActionSelection:
    return ConfirmedLegalActionSelection(
        confirmation_id="legal-move-1",
        identity=identity,
        action_type=ActionType.MOVE,
        action_name="Flower Trick",
        confirmation=_confirmation(),
    )

def _legal_switch_action(
    identity: TurnIdentity, *, action_name: str, index: int
) -> ConfirmedLegalActionSelection:
    return ConfirmedLegalActionSelection(
        confirmation_id=f"legal-switch-{index}",
        identity=identity,
        action_type=ActionType.SWITCH,
        action_name=action_name,
        confirmation=_confirmation(),
    )


# --- A/B/C/D/P: candidate derivation -----------------------------------------


def test_a_two_healthy_unknown_backline_candidates_both_visible() -> None:
    applied = _applied()
    candidates = derive_legal_switch_candidates(
        applied=applied, current_active_name="マスカーニャ", local_memory_by_name={}
    )
    assert candidates == ("ハッサム", "ガブリアス")


def test_b_one_confirmed_fainted_backline_excluded() -> None:
    applied = _applied()
    memory = {"ハッサム": _fainted("ハッサム")}
    candidates = derive_legal_switch_candidates(
        applied=applied, current_active_name="マスカーニャ", local_memory_by_name=memory
    )
    assert candidates == ("ガブリアス",)


def test_c_unknown_health_backline_remains_visible_candidate() -> None:
    applied = _applied()
    memory = {"ハッサム": _memory("ハッサム", Known.unknown())}
    candidates = derive_legal_switch_candidates(
        applied=applied, current_active_name="マスカーニャ", local_memory_by_name=memory
    )
    assert "ハッサム" in candidates


def test_d_all_backline_confirmed_fainted_candidate_list_empty() -> None:
    applied = _applied()
    memory = {"ハッサム": _fainted("ハッサム"), "ガブリアス": _fainted("ガブリアス")}
    candidates = derive_legal_switch_candidates(
        applied=applied, current_active_name="マスカーニャ", local_memory_by_name=memory
    )
    assert candidates == ()


def test_p_known_healthy_and_unknown_health_backline_both_candidates_no_forced_guess() -> None:
    applied = _applied()
    memory = {"ガブリアス": _healthy("ガブリアス")}
    candidates = derive_legal_switch_candidates(
        applied=applied, current_active_name="マスカーニャ", local_memory_by_name=memory
    )
    assert candidates == ("ハッサム", "ガブリアス")


# --- G/H/I/J/K: hard invalidity / fail-closed construction --------------------


def test_e_explicit_confirm_none_is_a_valid_construction() -> None:
    confirmation = confirm_legal_switches(
        confirmation_id="c-1",
        identity=_identity(),
        based_on_confirmed_state_id="state-1",
        applied=_applied(),
        current_active_name="マスカーニャ",
        local_memory_by_name={},
        legal_switches=(),
        status=LegalSwitchStatus.CONFIRMED_NONE,
        confirmation=_confirmation(),
    )
    assert confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert confirmation.legal_switches == ()


def test_g_empty_legal_switches_with_confirmed_nonempty_fails_closed() -> None:
    with pytest.raises(LegalSwitchError):
        LegalSwitchConfirmation(
            confirmation_id="c-1",
            identity=_identity(),
            based_on_confirmed_state_id="state-1",
            applied_selection_id="applied-1",
            legal_switches=(),
            status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
            confirmation=_confirmation(),
        )


def test_h_nonempty_legal_switches_with_confirmed_none_fails_closed() -> None:
    with pytest.raises(LegalSwitchError):
        LegalSwitchConfirmation(
            confirmation_id="c-1",
            identity=_identity(),
            based_on_confirmed_state_id="state-1",
            applied_selection_id="applied-1",
            legal_switches=("ハッサム",),
            status=LegalSwitchStatus.CONFIRMED_NONE,
            confirmation=_confirmation(),
        )


def test_i_current_active_included_fails_closed() -> None:
    with pytest.raises(LegalSwitchError):
        confirm_legal_switches(
            confirmation_id="c-1",
            identity=_identity(),
            based_on_confirmed_state_id="state-1",
            applied=_applied(),
            current_active_name="マスカーニャ",
            local_memory_by_name={},
            legal_switches=("マスカーニャ",),
            status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
            confirmation=_confirmation(),
        )


def test_j_outside_selected_three_included_fails_closed() -> None:
    with pytest.raises(LegalSwitchError):
        confirm_legal_switches(
            confirmation_id="c-1",
            identity=_identity(),
            based_on_confirmed_state_id="state-1",
            applied=_applied(),
            current_active_name="マスカーニャ",
            local_memory_by_name={},
            legal_switches=("イルカマン",),
            status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
            confirmation=_confirmation(),
        )


def test_k_confirmed_hp_zero_member_included_fails_closed() -> None:
    memory = {"ハッサム": _fainted("ハッサム")}
    with pytest.raises(LegalSwitchError):
        confirm_legal_switches(
            confirmation_id="c-1",
            identity=_identity(),
            based_on_confirmed_state_id="state-1",
            applied=_applied(),
            current_active_name="マスカーニャ",
            local_memory_by_name=memory,
            legal_switches=("ハッサム",),
            status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
            confirmation=_confirmation(),
        )


def test_duplicate_legal_switch_name_fails_closed() -> None:
    with pytest.raises(LegalSwitchError):
        confirm_legal_switches(
            confirmation_id="c-1",
            identity=_identity(),
            based_on_confirmed_state_id="state-1",
            applied=_applied(),
            current_active_name="マスカーニャ",
            local_memory_by_name={},
            legal_switches=("ハッサム", "ハッサム"),
            status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
            confirmation=_confirmation(),
        )


# --- F: unresolved (no confirmation at all) blocks provider-ready -------------


def test_f_missing_confirmation_blocks_provider_ready() -> None:
    identity = _identity()
    state = ConfirmedTurnState(
        confirmed_state_id="state-1",
        identity=identity,
        previous_confirmed_state_id=None,
        self_side=_confirmed_side("マスカーニャ"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(identity),),
        current_identity=identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=None,
        selected_three=("マスカーニャ", "ハッサム", "ガブリアス"),
    )
    assert not result.allowed
    assert GateDenialReason.LEGAL_SWITCHES_UNRESOLVED in result.denial_reasons


# --- L/M: identity/staleness invalidation via the gate ------------------------


def test_l_wrong_turn_identity_blocks_provider_ready() -> None:
    identity = _identity()
    other_identity = _identity(turn_id="turn-2", turn_number=2)
    state = ConfirmedTurnState(
        confirmed_state_id="state-1",
        identity=identity,
        previous_confirmed_state_id=None,
        self_side=_confirmed_side("マスカーニャ"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    confirmation = LegalSwitchConfirmation(
        confirmation_id="c-1",
        identity=other_identity,
        based_on_confirmed_state_id=state.confirmed_state_id,
        applied_selection_id="applied-1",
        legal_switches=(),
        status=LegalSwitchStatus.CONFIRMED_NONE,
        confirmation=_confirmation(),
    )
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(identity),),
        current_identity=identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=confirmation,
        selected_three=("マスカーニャ", "ハッサム", "ガブリアス"),
    )
    assert not result.allowed
    assert GateDenialReason.LEGAL_SWITCHES_IDENTITY_MISMATCH in result.denial_reasons


def test_m_stale_confirmed_state_binding_blocks_provider_ready() -> None:
    identity = _identity()
    state = ConfirmedTurnState(
        confirmed_state_id="state-2",
        identity=identity,
        previous_confirmed_state_id="state-1",
        self_side=_confirmed_side("マスカーニャ"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    stale_confirmation = LegalSwitchConfirmation(
        confirmation_id="c-1",
        identity=identity,
        based_on_confirmed_state_id="state-1",  # superseded revision
        applied_selection_id="applied-1",
        legal_switches=(),
        status=LegalSwitchStatus.CONFIRMED_NONE,
        confirmation=_confirmation(),
    )
    result = evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=(_legal_action(identity),),
        current_identity=identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=stale_confirmation,
        selected_three=("マスカーニャ", "ハッサム", "ガブリアス"),
    )
    assert not result.allowed
    assert GateDenialReason.LEGAL_SWITCHES_STALE_BINDING in result.denial_reasons


# --- Application-level end-to-end (persistence + restart) ---------------------


class _AppFixture:
    """A minimal BATTLE_READY -> TURN_REVIEWED session for
    ``BattleApplication.confirm_legal_switches``/``derive_legal_switch_candidates_for_current_turn``.
    """

    def __init__(self, tmp_path: Path, *, db_name: str = "b2.db") -> None:
        self.db_path = tmp_path / db_name
        self.repository = SQLiteRepository(self.db_path)
        self.application = MatchApplication(self.repository, tmp_path / "exports")
        self.session_id = "session-b2-app"
        self.match_id = "match-b2-app"
        self.generation = 9
        self.turn_id = "turn-1"
        self.turn_number = 1
        self.battle_revision = 1
        self.confirmed_state_id = "state-b2-1"

        session = BattleSession(
            session_id=self.session_id,
            match_id=self.match_id,
            generation=self.generation,
            state=BattleState.TURN_REVIEWED,
            battle_revision=self.battle_revision,
            current_reviewed_selection_id="selection-b2-1",
            current_applied_selection_id="applied-b2-1",
            current_turn_id=self.turn_id,
        )
        self.repository.insert_session(session)
        self.repository.append_turn(
            self.session_id, BattleTurn(turn_id=self.turn_id, turn_number=self.turn_number)
        )
        self.repository.append_selection_facts(
            self.session_id,
            SelectionFacts(
                reviewed_selection_id="selection-b2-1",
                self_team=(
                    "マスカーニャ", "ハッサム", "ガブリアス",
                    "ハバタクカミ", "ドラパルト", "ボーマンダ",
                ),
                opponent_team=(
                    "Garchomp", "Landorus", "Zamazenta", "Chien-Pao", "Iron Bundle", "Amoonguss",
                ),
            ),
        )
        self.repository.append_applied_selection(self.session_id, _applied())
        self.repository.append_turn_facts(
            self.session_id,
            TurnFactsSnapshot(
                turn_facts_id="facts-b2-1",
                turn_id=self.turn_id,
                turn_number=self.turn_number,
                self_active="マスカーニャ",
                opponent_active="Garchomp",
                self_hp=HpBucket.FULL,
                opponent_hp=HpBucket.FULL,
                legal_moves=("Flower Trick",),
                legal_switches=(),
            ),
        )
        self.repository.append_confirmed_turn_state(
            ConfirmedTurnState(
                confirmed_state_id=self.confirmed_state_id,
                identity=self.identity(),
                previous_confirmed_state_id=None,
                self_side=_confirmed_side("マスカーニャ"),
                opponent_side=_confirmed_side("Garchomp"),
                weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
                terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
                confirmation=_confirmation(),
            )
        )
        self.repository.connection.commit()

    def identity(self, **overrides: object) -> TurnIdentity:
        kwargs: dict[str, object] = dict(
            session_id=self.session_id,
            match_id=self.match_id,
            generation=self.generation,
            turn_id=self.turn_id,
            turn_number=self.turn_number,
            battle_revision=self.battle_revision,
        )
        kwargs.update(overrides)
        return TurnIdentity(**kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        self.repository.close()


def test_historical_masukaanya_hassam_gabriasu_before_and_after_confirmation(
    tmp_path: Path,
) -> None:
    """Direct Bundle 2 regression reproducing the original defect (section 12)."""

    fixture = _AppFixture(tmp_path)

    # -- Before operator confirmation: derived candidates only, unresolved. --
    candidates = fixture.application.derive_legal_switch_candidates_for_current_turn()
    assert candidates == ("ハッサム", "ガブリアス")

    identity = fixture.identity()
    state = fixture.repository.get_confirmed_turn_state(fixture.confirmed_state_id)
    unresolved_lookup = fixture.repository.get_legal_switch_confirmation(
        identity=identity,
        based_on_confirmed_state_id=state.confirmed_state_id,
        applied_selection_id="applied-b2-1",
    )
    assert unresolved_lookup is None  # NOT_CAPTURED_OR_UNRESOLVED, durably

    # -- After explicit operator confirmation: both names, CONFIRMED_NONEMPTY. --
    confirmed = fixture.application.confirm_legal_switches(
        legal_switches=("ハッサム", "ガブリアス"),
        status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
        human_confirmed=True,
    )
    assert confirmed.legal_switches == ("ハッサム", "ガブリアス")
    assert confirmed.status is LegalSwitchStatus.CONFIRMED_NONEMPTY

    persisted = fixture.repository.get_legal_switch_confirmation(
        identity=identity,
        based_on_confirmed_state_id=state.confirmed_state_id,
        applied_selection_id="applied-b2-1",
    )
    assert persisted is not None
    assert persisted.legal_switches == ("ハッサム", "ガブリアス")
    assert persisted.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    fixture.close()


def test_confirm_legal_switches_requires_human_confirmed_flag(tmp_path: Path) -> None:
    fixture = _AppFixture(tmp_path)
    with pytest.raises(DomainError):
        fixture.application.confirm_legal_switches(
            legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE, human_confirmed=False
        )
    fixture.close()


def test_confirm_legal_switches_rejects_current_active_at_application_layer(
    tmp_path: Path,
) -> None:
    fixture = _AppFixture(tmp_path)
    with pytest.raises(DomainError, match="LEGAL_SWITCH_INCLUDES_CURRENT_ACTIVE"):
        fixture.application.confirm_legal_switches(
            legal_switches=("マスカーニャ",),
            status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
            human_confirmed=True,
        )
    fixture.close()


# --- N/O: restart / hydration -------------------------------------------------


def test_n_restart_same_binding_hydrates_exact_confirmation(tmp_path: Path) -> None:
    fixture = _AppFixture(tmp_path)
    identity = fixture.identity()
    confirmed = fixture.application.confirm_legal_switches(
        legal_switches=("ハッサム",),
        status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
        human_confirmed=True,
    )
    fixture.close()

    restarted = SQLiteRepository(fixture.db_path)
    hydrated = restarted.get_legal_switch_confirmation(
        identity=identity,
        based_on_confirmed_state_id=confirmed.based_on_confirmed_state_id,
        applied_selection_id=confirmed.applied_selection_id,
    )
    assert hydrated is not None
    assert hydrated.legal_switches == ("ハッサム",)
    assert hydrated.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    restarted.close()


def test_o_restart_stale_binding_is_unresolved(tmp_path: Path) -> None:
    fixture = _AppFixture(tmp_path)
    identity = fixture.identity()
    confirmed = fixture.application.confirm_legal_switches(
        legal_switches=("ハッサム",),
        status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
        human_confirmed=True,
    )
    fixture.close()

    restarted = SQLiteRepository(fixture.db_path)
    mismatched = restarted.get_legal_switch_confirmation(
        identity=identity,
        based_on_confirmed_state_id="a-different-confirmed-state-id",
        applied_selection_id=confirmed.applied_selection_id,
    )
    assert mismatched is None
    restarted.close()


# --- Q/R: missing/inconsistent factual-state prerequisites ---------------------


def test_q_no_applied_selection_blocks_candidate_derivation(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "q.db")
    application = MatchApplication(repository, tmp_path / "exports")
    session = BattleSession(
        session_id="session-q",
        match_id="match-q",
        generation=9,
        state=BattleState.TURN_REVIEWED,
        battle_revision=1,
        current_turn_id="turn-1",
    )
    repository.insert_session(session)
    repository.append_turn(session.session_id, BattleTurn(turn_id="turn-1", turn_number=1))
    repository.connection.commit()
    with pytest.raises(DomainError, match="APPLIED_SELECTION_REQUIRED"):
        application.derive_legal_switch_candidates_for_current_turn()
    repository.close()


# --- R3-C: provider-boundary defense-in-depth (forged/corrupted confirmations) -


def _r3c_state(*, active: str = "A") -> ConfirmedTurnState:
    return ConfirmedTurnState(
        confirmed_state_id="state-r3c",
        identity=_identity(),
        previous_confirmed_state_id=None,
        self_side=_confirmed_side(active),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )


def _r3c_confirmation(
    *, legal_switches: tuple[str, ...], status: LegalSwitchStatus
) -> LegalSwitchConfirmation:
    """Constructed directly -- bypasses confirm_legal_switches()'s own
    contextual validation, simulating a forged/corrupted-but-correctly-
    bound row for R3-C defense-in-depth."""

    return LegalSwitchConfirmation(
        confirmation_id="c-r3c",
        identity=_identity(),
        based_on_confirmed_state_id="state-r3c",
        applied_selection_id="applied-r3c",
        legal_switches=legal_switches,
        status=status,
        confirmation=_confirmation(),
    )


def _r3c_gate(
    *,
    state: ConfirmedTurnState,
    confirmation: LegalSwitchConfirmation | None,
    selected_three: tuple[str, str, str] | None = ("A", "B", "C"),
    confirmed_fainted_members: frozenset[str] = frozenset(),
    confirmed_switch_action_names: tuple[str, ...] | None = None,
):
    if confirmed_switch_action_names is None:
        confirmed_switch_action_names = (
            confirmation.legal_switches
            if confirmation is not None
            and confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
            else ()
        )
    confirmed_actions = (
        _legal_action(state.identity),
        *(
            _legal_switch_action(state.identity, action_name=name, index=index)
            for index, name in enumerate(confirmed_switch_action_names, start=1)
        ),
    )
    return evaluate_provider_ready_gate(
        confirmed_state=state,
        confirmed_legal_actions=confirmed_actions,
        current_identity=state.identity,
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=confirmation,
        selected_three=selected_three,
        confirmed_fainted_members=confirmed_fainted_members,
    )


def test_r3c_a_confirmation_includes_current_active_blocks_ready() -> None:
    state = _r3c_state(active="A")
    confirmation = _r3c_confirmation(
        legal_switches=("A", "B"), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    result = _r3c_gate(state=state, confirmation=confirmation)
    assert result.allowed is False
    assert GateDenialReason.LEGAL_SWITCHES_MEMBER_IS_ACTIVE in result.denial_reasons


def test_r3c_b_confirmation_includes_member_outside_selected_three_blocks_ready() -> None:
    state = _r3c_state(active="A")
    confirmation = _r3c_confirmation(
        legal_switches=("B", "X"), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    result = _r3c_gate(state=state, confirmation=confirmation)
    assert result.allowed is False
    assert GateDenialReason.LEGAL_SWITCHES_MEMBER_OUTSIDE_SELECTED_THREE in result.denial_reasons


def test_r3c_c_confirmation_includes_confirmed_fainted_member_blocks_ready() -> None:
    state = _r3c_state(active="A")
    confirmation = _r3c_confirmation(
        legal_switches=("B", "C"), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    result = _r3c_gate(
        state=state, confirmation=confirmation, confirmed_fainted_members=frozenset({"B"})
    )
    assert result.allowed is False
    assert GateDenialReason.LEGAL_SWITCHES_MEMBER_CONFIRMED_FAINTED in result.denial_reasons


def test_r3c_d_active_outside_selected_three_blocks_ready_even_if_confirmation_well_formed() -> (
    None
):
    state = _r3c_state(active="X")
    confirmation = _r3c_confirmation(
        legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE
    )
    result = _r3c_gate(state=state, confirmation=confirmation)
    assert result.allowed is False
    assert GateDenialReason.LEGAL_SWITCHES_ACTIVE_OUTSIDE_SELECTED_THREE in result.denial_reasons


def test_r3c_e_valid_nonempty_confirmation_reaches_ready() -> None:
    state = _r3c_state(active="A")
    confirmation = _r3c_confirmation(
        legal_switches=("B", "C"), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    result = _r3c_gate(state=state, confirmation=confirmation)
    assert result.allowed is True
    assert result.denial_reasons == ()


def test_r3c_h_nonempty_confirmation_without_switch_actions_fails_closed() -> None:
    """A confirmed switch set must be represented in provider legal_actions."""

    state = _r3c_state(active="A")
    confirmation = _r3c_confirmation(
        legal_switches=("B", "C"), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    result = _r3c_gate(
        state=state,
        confirmation=confirmation,
        confirmed_switch_action_names=(),
    )
    assert result.allowed is False
    assert (
        GateDenialReason.LEGAL_SWITCH_ACTIONS_MISMATCH_CONFIRMATION
        in result.denial_reasons
    )


def test_r3c_f_valid_confirmed_none_reaches_ready() -> None:
    state = _r3c_state(active="A")
    confirmation = _r3c_confirmation(legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE)
    result = _r3c_gate(state=state, confirmation=confirmation)
    assert result.allowed is True
    assert result.denial_reasons == ()


def test_r3c_missing_selected_three_blocks_ready() -> None:
    state = _r3c_state(active="A")
    confirmation = _r3c_confirmation(legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE)
    result = _r3c_gate(state=state, confirmation=confirmation, selected_three=None)
    assert result.allowed is False
    assert GateDenialReason.LEGAL_SWITCHES_APPLIED_SELECTION_MISSING in result.denial_reasons


# --- R3-F: faint-state change invalidates a previously-confirmed set -----------


def test_r3f_faint_state_change_after_confirmation_blocks_ready_at_provider_boundary() -> None:
    """A legal-switch set confirmed while B was healthy/unknown must fail
    closed at the provider boundary once B is later confirmed fainted --
    even though the confirmation's own binding (identity/confirmed-state
    id) is still exactly current. Defense-in-depth, not merely staleness."""

    state = _r3c_state(active="A")
    confirmation = _r3c_confirmation(
        legal_switches=("B", "C"), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    before = _r3c_gate(state=state, confirmation=confirmation)
    assert before.allowed is True

    after = _r3c_gate(
        state=state, confirmation=confirmation, confirmed_fainted_members=frozenset({"B"})
    )
    assert after.allowed is False
    assert GateDenialReason.LEGAL_SWITCHES_MEMBER_CONFIRMED_FAINTED in after.denial_reasons


def test_r3f_faint_state_change_invalidates_at_hydration_layer_too(tmp_path: Path) -> None:
    """Same fact, proven at the application/hydration layer: re-deriving
    candidates after B becomes confirmed-fainted must exclude B, exactly
    like a fresh (never-confirmed) derivation would."""

    fixture = _AppFixture(tmp_path)
    fixture.repository.upsert_pokemon_local_state(
        session_id=fixture.session_id,
        match_id=fixture.match_id,
        generation=fixture.generation,
        side="SELF",
        memory=PokemonLocalMemory(
            pokemon_name="ハッサム", hp_bucket=Known.unknown(), status=Known.unknown()
        ),
    )
    fixture.repository.connection.commit()
    candidates_before = fixture.application.derive_legal_switch_candidates_for_current_turn()
    assert candidates_before == ("ハッサム", "ガブリアス")

    fixture.repository.upsert_pokemon_local_state(
        session_id=fixture.session_id,
        match_id=fixture.match_id,
        generation=fixture.generation,
        side="SELF",
        memory=PokemonLocalMemory(
            pokemon_name="ハッサム",
            hp_bucket=Known.confirmed(HpBucket.ZERO, provenance_chain=_HUMAN),
            status=Known.unknown(),
        ),
    )
    fixture.repository.connection.commit()
    candidates_after = fixture.application.derive_legal_switch_candidates_for_current_turn()
    assert candidates_after == ("ガブリアス",)
    fixture.close()
