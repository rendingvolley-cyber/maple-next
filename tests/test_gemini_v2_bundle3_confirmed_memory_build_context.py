"""Gemini V2 Bundle 3: confirmed prior battle memory + selected-three build context.

Closes the historical Battle-1 defect where a Turn N rich request carried no
confirmed record of Turns 1..N-1 and no canonical build for the three
Pokemon actually brought (only, at best, a party-six hash).

Everything here is either a pure function call or a repository-backed
read/validate. No provider is contacted: this file never builds a transport
body, never calls a transport, and never marks a job dispatched. The one
place a durable job is created (:meth:`request_rich_turn_advice`) is the
existing human-authorized application command, and the tests only inspect
its envelope.

The historical fixture reproduces the real reported match shape: seven
Turns with ``selected_three`` = マスカーニャ / ハッサム / ガブリアス, including a
confirmed switch, a confirmed faint, a confirmed status, a confirmed stat
stage, an HP change, and two Turns whose opponent action was genuinely
never confirmed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.application.turn_provider_export_bridge import (
    build_pure_rich_state_request_from_loaded_state,
    load_bundle3_turn_context,
    load_champions_rules_context,
    load_champions_rules_season_id,
    load_opponent_intel_context,
)
from maple_next.domain.battle_memory import (
    BattleMemory,
    BattleMemoryError,
    Bundle3ContextError,
    Bundle3TurnContext,
    SelectedBuildContextError,
    SelectedBuildStatus,
    build_battle_memory,
    project_selected_three_builds,
)
from maple_next.domain.champions_rules import (
    current_rules_pin_for_new_match,
)
from maple_next.domain.enums import ActionOrder, ActionType, BattleState, HpBucket
from maple_next.domain.legal_switches import LegalSwitchConfirmation, LegalSwitchStatus
from maple_next.domain.models import (
    AppliedSelectionSnapshot,
    BattleSession,
    BattleTurn,
    SelectionFacts,
)
from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
)
from maple_next.domain.turn_state import (
    ActionResultDelta,
    ConfirmationMeta,
    ConfirmedLegalActionSelection,
    ConfirmedTurnState,
    FieldDelta,
    KnowledgeStatus,
    Known,
    ProvenanceStep,
    SideDelta,
    SideState,
    TurnIdentity,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.turn_advice_rich_state import (
    RICH_STATE_REQUEST_CONTRACT_VERSION,
    RichStateTurnAdviceRequest,
    canonical_rich_request_dict,
    encode_canonical_rich_request,
)
from tests.fixtures.bundle5 import provision_fixture_generation

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)
_CARRY = (ProvenanceStep.PREVIOUS_CONFIRMED_CARRY_FORWARD,)
CONFIRMED_AT = "2026-08-16T00:00:00+00:00"

MASUKAANYA = "マスカーニャ"
HASSAMU = "ハッサム"
GABURIASU = "ガブリアス"
SELECTED_THREE: tuple[str, str, str] = (MASUKAANYA, HASSAMU, GABURIASU)
BENCHED = ("ハバタクカミ", "ドラパルト", "ボーマンダ")
SELF_TEAM_SIX = (*SELECTED_THREE, *BENCHED)
OPPONENT_TEAM_SIX = (
    "Garchomp",
    "Landorus",
    "Zamazenta",
    "Chien-Pao",
    "Iron Bundle",
    "Amoonguss",
)

#: Turn-start actives per Turn (1-based). The self side switches twice.
SELF_ACTIVE_BY_TURN = {
    1: MASUKAANYA,
    2: MASUKAANYA,
    3: MASUKAANYA,
    4: HASSAMU,
    5: HASSAMU,
    6: HASSAMU,
    7: GABURIASU,
}
OPPONENT_ACTIVE_BY_TURN = {
    1: "Garchomp",
    2: "Garchomp",
    3: "Garchomp",
    4: "Garchomp",
    5: "Amoonguss",
    6: "Amoonguss",
    7: "Amoonguss",
}

CURRENT_TURN_NUMBER = 7


def _confirmation() -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True, confirmed_at_utc=CONFIRMED_AT, provenance="HUMAN_INPUT"
    )


def _confirmed_side(active: str, *, hp: HpBucket = HpBucket.FULL) -> SideState:
    return SideState(
        active=Known.confirmed(active, provenance_chain=_HUMAN),
        hp_bucket=Known.confirmed(hp, provenance_chain=_HUMAN),
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


def _side_delta(**overrides: FieldDelta) -> SideDelta:  # type: ignore[type-arg]
    fields: dict[str, FieldDelta] = {  # type: ignore[type-arg]
        name: FieldDelta.unchanged(provenance_chain=_CARRY)
        for name in (
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
    }
    fields.update(overrides)
    return SideDelta(**fields)  # type: ignore[arg-type]


def _detailed_member(name: str, move: str) -> ChampionsPokemonBuild:
    return ChampionsPokemonBuild(
        pokemon_name=name,
        moves=(move,),
        held_item="いのちのたま",
        ability="しんりょく",
        nature="ようき",
        stat_points=ChampionsStatPoints(attack=32, speed=32),
    )


def _detailed_team_build() -> ChampionsTeamBuild:
    moves = (
        "トリックフラワー",
        "バレットパンチ",
        "じしん",
        "ムーンフォース",
        "ドラゴンアロー",
        "はかいこうせん",
    )
    return ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION,
        game=CHAMPIONS_GAME,
        name="battle-1-team",
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=tuple(
            _detailed_member(name, move) for name, move in zip(SELF_TEAM_SIX, moves, strict=True)
        ),
    )


#: One confirmed completed-Turn script per prior Turn. ``None`` opponent
#: action means the human never confirmed what the opponent did -- that is a
#: genuine UNKNOWN, never an inference.
_COMPLETED_TURNS: dict[int, dict[str, object]] = {
    1: {
        "own": (ActionType.MOVE, "トリックフラワー"),
        "opponent": (ActionType.MOVE, "じしん"),
        "order": ActionOrder.SELF_FIRST,
        "self_delta": {"hp_bucket": FieldDelta.changed(HpBucket.SIXTY_ONE_TO_SEVENTY,
                                                       provenance_chain=_HUMAN)},
        "opponent_delta": {"hp_bucket": FieldDelta.changed(HpBucket.FIFTY_ONE_TO_SIXTY,
                                                           provenance_chain=_HUMAN)},
    },
    2: {
        "own": (ActionType.MOVE, "トリックフラワー"),
        "opponent": None,
        "order": ActionOrder.UNKNOWN,
        "self_delta": {"status": FieldDelta.changed("PARALYSIS", provenance_chain=_HUMAN)},
        "opponent_delta": {"hp_bucket": FieldDelta.changed(HpBucket.TWENTY_ONE_TO_THIRTY,
                                                           provenance_chain=_HUMAN)},
    },
    3: {
        "own": (ActionType.SWITCH, HASSAMU),
        "opponent": (ActionType.MOVE, "じしん"),
        "order": ActionOrder.OPPONENT_FIRST,
        "self_delta": {
            "active": FieldDelta.changed(HASSAMU, provenance_chain=_HUMAN),
            "status": FieldDelta.changed("NONE", provenance_chain=_HUMAN),
        },
        "opponent_delta": {},
    },
    4: {
        "own": (ActionType.MOVE, "バレットパンチ"),
        "opponent": (ActionType.MOVE, "まもる"),
        "order": ActionOrder.SELF_FIRST,
        "self_delta": {},
        # Confirmed faint, then the confirmed replacement -- represented
        # entirely through the existing canonical deltas.
        "opponent_delta": {
            "hp_bucket": FieldDelta.changed(HpBucket.ZERO, provenance_chain=_HUMAN),
            "active": FieldDelta.changed("Amoonguss", provenance_chain=_HUMAN),
        },
    },
    5: {
        "own": (ActionType.MOVE, "バレットパンチ"),
        "opponent": None,
        "order": ActionOrder.UNKNOWN,
        "self_delta": {"attack_stage": FieldDelta.changed(2, provenance_chain=_HUMAN)},
        "opponent_delta": {"defense_stage": FieldDelta.changed(-1, provenance_chain=_HUMAN)},
    },
    6: {
        "own": (ActionType.SWITCH, GABURIASU),
        "opponent": (ActionType.MOVE, "キノコのほうし"),
        "order": ActionOrder.OPPONENT_FIRST,
        "self_delta": {
            "active": FieldDelta.changed(GABURIASU, provenance_chain=_HUMAN),
            "status": FieldDelta.changed("SLEEP", provenance_chain=_HUMAN),
        },
        "opponent_delta": {},
    },
}


class Bundle3Fixture:
    """A seven-Turn historical-like match: Turns 1-6 completed, Turn 7 current."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        detailed_build: bool = True,
        completion_order: tuple[int, ...] = (1, 2, 3, 4, 5, 6),
        db_name: str = "bundle3.db",
        pin_rules: bool = True,
        intel_snapshot: dict[str, object] | None = None,
    ) -> None:
        self.db_path = tmp_path / db_name
        self.repository = SQLiteRepository(self.db_path)
        # Bundle 5 (Gemini V2): a per-fixture, throwaway opponent-INTEL
        # directory. Never the operator's provisioned runtime artifact --
        # ``intel_snapshot`` (when given) provisions hermetic fixture bytes
        # into it and pins the resulting immutable generation to this
        # match, exactly as ``new_match`` would.
        self.intel_directory = tmp_path / f"{db_name}-intel-db"
        self.application = MatchApplication(
            self.repository,
            tmp_path / "exports",
            opponent_intel_directory=self.intel_directory,
        )
        self.session_id = "session-b3"
        self.match_id = "match-b3"
        self.generation = 9
        self.reviewed_selection_id = "selection-b3"
        self.applied_selection_id = "applied-b3"
        self.advice_id = "advice-b3"
        self.advice_job_id = "job-advice-b3"

        # Bundle 4 (Gemini V2): a real match pin, exactly what
        # ``BattleApplication.new_match`` would have written -- this
        # historical fixture predates Bundle 4, but its session must still
        # be provider-ready under the Bundle 4 fail-closed gate. Some
        # Bundle 4 tests deliberately pass ``pin_rules=False`` to prove an
        # unpinned match fails closed instead of silently binding to the
        # latest rules.
        rules_pin = current_rules_pin_for_new_match() if pin_rules else None
        intel_pin = (
            provision_fixture_generation(self.intel_directory, intel_snapshot)
            if intel_snapshot is not None
            else None
        )
        session = BattleSession(
            session_id=self.session_id,
            match_id=self.match_id,
            generation=self.generation,
            state=BattleState.TURN_REVIEWED,
            battle_revision=CURRENT_TURN_NUMBER,
            current_reviewed_selection_id=self.reviewed_selection_id,
            current_applied_selection_id=self.applied_selection_id,
            current_turn_id=self.turn_id(CURRENT_TURN_NUMBER),
            rules_ruleset_id=rules_pin.ruleset_id if rules_pin else None,
            rules_ruleset_version=rules_pin.ruleset_version if rules_pin else None,
            rules_snapshot_id=rules_pin.rules_snapshot_id if rules_pin else None,
            rules_facts_sha256=rules_pin.rules_facts_sha256 if rules_pin else None,
            opponent_intel_pin_status="PINNED" if intel_pin else None,
            opponent_intel_generation_id=intel_pin.generation_id if intel_pin else None,
            opponent_intel_snapshot_sha256=(
                intel_pin.snapshot_sha256 if intel_pin else None
            ),
        )
        self.repository.insert_session(session)

        build = _detailed_team_build() if detailed_build else None
        self.repository.append_selection_facts(
            self.session_id,
            SelectionFacts(
                reviewed_selection_id=self.reviewed_selection_id,
                self_team=SELF_TEAM_SIX,
                opponent_team=OPPONENT_TEAM_SIX,
                self_team_build=build,
                self_team_build_sha256=build.sha256() if build is not None else None,
            ),
        )
        self._seed_selection_advice_binding()
        self.repository.append_applied_selection(
            self.session_id,
            AppliedSelectionSnapshot(
                applied_selection_id=self.applied_selection_id,
                selected_three=SELECTED_THREE,
                lead=MASUKAANYA,
                backline=(HASSAMU, GABURIASU),
                source_advice_id=self.advice_id,
            ),
        )

        for turn_number in range(1, CURRENT_TURN_NUMBER + 1):
            self.repository.append_turn(
                self.session_id,
                BattleTurn(turn_id=self.turn_id(turn_number), turn_number=turn_number),
            )
            self.repository.append_confirmed_turn_state(self.confirmed_state(turn_number))
        self.repository.connection.commit()

        for turn_number in completion_order:
            self.record_completion(turn_number)

        self.repository.append_confirmed_legal_action_selection(
            ConfirmedLegalActionSelection(
                confirmation_id="legal-move-7",
                identity=self.identity(CURRENT_TURN_NUMBER),
                action_type=ActionType.MOVE,
                action_name="じしん",
                confirmation=_confirmation(),
            )
        )
        for index, name in enumerate((MASUKAANYA, HASSAMU), start=1):
            self.repository.append_confirmed_legal_action_selection(
                ConfirmedLegalActionSelection(
                    confirmation_id=f"legal-switch-{index}-7",
                    identity=self.identity(CURRENT_TURN_NUMBER),
                    action_type=ActionType.SWITCH,
                    action_name=name,
                    confirmation=_confirmation(),
                )
            )
        self.repository.upsert_legal_switch_confirmation(
            LegalSwitchConfirmation(
                confirmation_id="switch-confirm-7",
                identity=self.identity(CURRENT_TURN_NUMBER),
                based_on_confirmed_state_id=self.confirmed_state_id(CURRENT_TURN_NUMBER),
                applied_selection_id=self.applied_selection_id,
                legal_switches=(MASUKAANYA, HASSAMU),
                status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
                confirmation=_confirmation(),
            )
        )
        self.repository.connection.commit()

    # --- identities ------------------------------------------------------

    @staticmethod
    def turn_id(turn_number: int) -> str:
        return f"turn-{turn_number}"

    @staticmethod
    def confirmed_state_id(turn_number: int) -> str:
        return f"state-{turn_number}"

    def identity(self, turn_number: int, **overrides: object) -> TurnIdentity:
        kwargs: dict[str, object] = dict(
            session_id=self.session_id,
            match_id=self.match_id,
            generation=self.generation,
            turn_id=self.turn_id(turn_number),
            turn_number=turn_number,
            battle_revision=turn_number,
        )
        kwargs.update(overrides)
        return TurnIdentity(**kwargs)  # type: ignore[arg-type]

    # --- durable seeding --------------------------------------------------

    def _seed_selection_advice_binding(self) -> None:
        from datetime import UTC, datetime

        from maple_next.domain.enums import JobStatus, JobType
        from maple_next.workers.contracts.models import JobEnvelope

        self.repository.insert_job(
            JobEnvelope(
                contract_version="maple-worker.v1",
                job_id=self.advice_job_id,
                command_id="command-selection",
                job_type=JobType.SELECTION_ADVICE,
                session_id=self.session_id,
                match_id=self.match_id,
                generation=self.generation,
                turn_number=None,
                base_battle_revision=1,
                expected_state=BattleState.SELECTION_OPEN,
                input_snapshot_id=self.reviewed_selection_id,
                request_payload_hash="0" * 64,
                human_authorized_at=datetime.now(UTC),
                status=JobStatus.SUCCEEDED,
            )
        )
        self.repository.append_selection_advice(
            self.advice_id,
            self.session_id,
            self.advice_job_id,
            SELECTED_THREE,
            MASUKAANYA,
            (HASSAMU, GABURIASU),
        )

    def confirmed_state(self, turn_number: int) -> ConfirmedTurnState:
        return ConfirmedTurnState(
            confirmed_state_id=self.confirmed_state_id(turn_number),
            identity=self.identity(turn_number),
            previous_confirmed_state_id=(
                self.confirmed_state_id(turn_number - 1) if turn_number > 1 else None
            ),
            self_side=_confirmed_side(SELF_ACTIVE_BY_TURN[turn_number]),
            opponent_side=_confirmed_side(OPPONENT_ACTIVE_BY_TURN[turn_number]),
            weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
            terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
            confirmation=_confirmation(),
        )

    def delta(self, turn_number: int, *, delta_id: str | None = None) -> ActionResultDelta:
        script = _COMPLETED_TURNS[turn_number]
        return ActionResultDelta(
            delta_id=delta_id or f"delta-{turn_number}",
            identity=self.identity(turn_number),
            based_on_confirmed_state_id=self.confirmed_state_id(turn_number),
            self_side=_side_delta(**script["self_delta"]),  # type: ignore[arg-type]
            opponent_side=_side_delta(**script["opponent_delta"]),  # type: ignore[arg-type]
            weather=FieldDelta.unchanged(provenance_chain=_CARRY),
            terrain=FieldDelta.unchanged(provenance_chain=_CARRY),
            confirmation=_confirmation(),
        )

    def record_completion(self, turn_number: int) -> None:
        script = _COMPLETED_TURNS[turn_number]
        own_type, own_name = script["own"]  # type: ignore[misc]
        opponent = script["opponent"]
        opponent_type, opponent_name = opponent if opponent is not None else (None, None)  # type: ignore[misc]
        self.repository.record_rich_action_completion(
            transaction_id=f"completion-{turn_number}",
            identity=self.identity(turn_number),
            own_action_type=own_type,
            own_action_name=own_name,
            opponent_action_type=opponent_type,
            opponent_action_name=opponent_name,
            action_order=script["order"],  # type: ignore[arg-type]
            delta=self.delta(turn_number),
        )

    # --- request assembly -------------------------------------------------

    def bundle3_context(self) -> Bundle3TurnContext:
        session = self.repository.load_active_session()
        assert session is not None
        applied = self.repository.get_applied_selection(self.applied_selection_id)
        return load_bundle3_turn_context(
            self.repository,
            session=session,
            current_identity=self.identity(CURRENT_TURN_NUMBER),
            current_confirmed_state=self.repository.get_confirmed_turn_state(
                self.confirmed_state_id(CURRENT_TURN_NUMBER)
            ),
            applied=applied,
        )

    def rules_context(self) -> dict[str, object]:
        session = self.repository.load_active_session()
        assert session is not None
        return load_champions_rules_context(session)

    def rules_season_id(self) -> str:
        """Bundle 5 R1: the canonical pinned season, a validation-only input.

        Threaded into request assembly exactly as production does, so an
        ``AVAILABLE`` context's ``MATCHED`` compatibility claim is
        revalidated here against *both* the pinned season and the pinned
        battle format rather than only the format.
        """

        session = self.repository.load_active_session()
        assert session is not None
        return load_champions_rules_season_id(session)

    def opponent_intel_context(self, turn_number: int = CURRENT_TURN_NUMBER) -> dict[str, Any]:
        session = self.repository.load_active_session()
        assert session is not None
        return load_opponent_intel_context(
            session,
            confirmed_state=self.repository.get_confirmed_turn_state(
                self.confirmed_state_id(turn_number)
            ),
            intel_directory=self.intel_directory,
        )

    def build_request_with_intel_context(
        self, opponent_intel_context: dict[str, Any]
    ) -> RichStateTurnAdviceRequest:
        """Bundle 5: assemble a request with a caller-supplied INTEL context.

        Used by the Bundle 5 fail-closed tests to feed a deliberately
        forged/stale/malformed context through the *real* request builder,
        rather than asserting against a hand-rolled parallel code path.
        """

        return self.build_request(opponent_intel_context=opponent_intel_context)

    def build_request(
        self,
        opponent_intel_context: dict[str, Any] | None = None,
        *,
        rules_season_id: str | None = None,
    ) -> RichStateTurnAdviceRequest:
        identity = self.identity(CURRENT_TURN_NUMBER)
        state = self.repository.get_confirmed_turn_state(
            self.confirmed_state_id(CURRENT_TURN_NUMBER)
        )
        facts = self.repository.get_selection_facts(self.reviewed_selection_id)
        return build_pure_rich_state_request_from_loaded_state(
            current_identity=identity,
            latest_confirmed_state=state,
            confirmed_legal_actions=(
                self.repository.list_confirmed_legal_action_selections_for_identity(identity)
            ),
            latest_open_draft=None,
            legal_switch_confirmation=self.repository.get_legal_switch_confirmation(
                identity=identity,
                based_on_confirmed_state_id=state.confirmed_state_id,
                applied_selection_id=self.applied_selection_id,
            ),
            selected_three=SELECTED_THREE,
            self_active=SELF_ACTIVE_BY_TURN[CURRENT_TURN_NUMBER],
            bundle3_context=self.bundle3_context(),
            rules_context=self.rules_context(),
            opponent_intel_context=(
                opponent_intel_context
                if opponent_intel_context is not None
                else self.opponent_intel_context()
            ),
            rules_season_id=(
                rules_season_id if rules_season_id is not None else self.rules_season_id()
            ),
            self_team_build_sha256=facts.self_team_build_sha256,
        )

    def close(self) -> None:
        self.repository.close()


@pytest.fixture
def fixture(tmp_path: Path) -> Bundle3Fixture:
    return Bundle3Fixture(tmp_path)


@pytest.fixture
def names_only_fixture(tmp_path: Path) -> Bundle3Fixture:
    return Bundle3Fixture(tmp_path, detailed_build=False, db_name="bundle3-names.db")


# --- Contract v4 --------------------------------------------------------------


def test_request_contract_is_v4_with_all_four_new_fields(fixture: Bundle3Fixture) -> None:
    """The Bundle 3 fields below survive verbatim even though later bundles
    have since raised the contract to ``.v5``, ``.v6``, ``.v7``, and ``.v8`` (see
    ``test_gemini_v2_bundle4_versioned_rules_context.py`` and
    ``test_gemini_v2_bundle5_opponent_intel_context.py``)."""

    request = fixture.build_request()
    assert RICH_STATE_REQUEST_CONTRACT_VERSION == "maple-turn-advice.v8"
    assert request.contract_version == "maple-turn-advice.v8"
    canonical = canonical_rich_request_dict(request)
    for field in (
        "applied_selection_id",
        "reviewed_selection_id",
        "selected_three_builds",
        "battle_memory",
    ):
        assert field in canonical
    assert canonical["applied_selection_id"] == fixture.applied_selection_id
    assert canonical["reviewed_selection_id"] == fixture.reviewed_selection_id
    # Every v3 field survives untouched.
    for field in (
        "reviewed_state",
        "selected_three",
        "self_active",
        "legal_actions",
        "legal_switches",
        "legal_switches_status",
        "reviewed_snapshot_hash",
        "self_team_build_sha256",
    ):
        assert field in canonical


# --- 1/2/3: selected-three build projection ----------------------------------


def test_1_detailed_six_member_build_projects_exactly_selected_three(
    fixture: Bundle3Fixture,
) -> None:
    request = fixture.build_request()
    assert len(request.selected_three_builds) == 3
    assert tuple(m.pokemon_name for m in request.selected_three_builds) == SELECTED_THREE
    assert all(
        m.build_status is SelectedBuildStatus.CONFIRMED for m in request.selected_three_builds
    )
    assert request.selected_three_builds[0].build is not None
    assert request.selected_three_builds[0].build.moves == ("トリックフラワー",)


def test_2_unselected_party_members_are_absent_from_the_request(
    fixture: Bundle3Fixture,
) -> None:
    payload = encode_canonical_rich_request(fixture.build_request()).decode("utf-8")
    for benched in BENCHED:
        assert benched not in payload
    for selected in SELECTED_THREE:
        assert selected in payload


def test_3_names_only_facts_yield_three_unknown_builds_and_stay_provider_ready(
    names_only_fixture: Bundle3Fixture,
) -> None:
    request = names_only_fixture.build_request()
    assert tuple(m.pokemon_name for m in request.selected_three_builds) == SELECTED_THREE
    assert all(
        m.build_status is SelectedBuildStatus.UNKNOWN for m in request.selected_three_builds
    )
    assert all(m.build is None for m in request.selected_three_builds)
    # Non-blocking: the request is still fully assembled and hashed.
    assert len(request.request_hash) == 64


def test_3b_names_only_request_hash_differs_from_detailed_request_hash(
    fixture: Bundle3Fixture, names_only_fixture: Bundle3Fixture
) -> None:
    assert fixture.build_request().request_hash != names_only_fixture.build_request().request_hash


# --- 4: wrong applied/advice/job/reviewed binding fails closed ---------------


def test_4a_missing_source_advice_fails_closed(fixture: Bundle3Fixture) -> None:
    fixture.repository.connection.execute(
        "UPDATE applied_selections SET source_advice_id = 'ghost-advice'"
    )
    fixture.repository.connection.commit()
    with pytest.raises(Bundle3ContextError):
        fixture.bundle3_context()


def test_4b_advice_from_a_foreign_session_fails_closed(fixture: Bundle3Fixture) -> None:
    """An advice row belonging to another session can never bind this bring."""

    fixture.repository.insert_session(
        BattleSession(
            session_id="session-other",
            match_id="match-other",
            generation=10,
            state=BattleState.SELECTION_OPEN,
            battle_revision=1,
            active_slot=None,
        )
    )
    fixture.repository.append_selection_advice(
        "advice-other",
        "session-other",
        fixture.advice_job_id + "-other",
        SELECTED_THREE,
        MASUKAANYA,
        (HASSAMU, GABURIASU),
    )
    fixture.repository.connection.execute(
        "UPDATE applied_selections SET source_advice_id = 'advice-other'"
    )
    fixture.repository.connection.commit()
    with pytest.raises(Bundle3ContextError):
        fixture.bundle3_context()


def test_4c_advice_job_bound_to_a_different_reviewed_selection_fails_closed(
    fixture: Bundle3Fixture,
) -> None:
    fixture.repository.connection.execute(
        "UPDATE async_jobs SET input_snapshot_id = 'other-selection' WHERE job_id = ?",
        (fixture.advice_job_id,),
    )
    fixture.repository.connection.commit()
    with pytest.raises(Bundle3ContextError):
        fixture.bundle3_context()


def test_4d_advice_job_from_a_foreign_match_fails_closed(fixture: Bundle3Fixture) -> None:
    fixture.repository.connection.execute(
        "UPDATE async_jobs SET match_id = 'other-match' WHERE job_id = ?",
        (fixture.advice_job_id,),
    )
    fixture.repository.connection.commit()
    with pytest.raises(Bundle3ContextError):
        fixture.bundle3_context()


def test_4e_session_without_a_reviewed_selection_fails_closed(fixture: Bundle3Fixture) -> None:
    session = fixture.repository.load_active_session()
    assert session is not None
    session.current_reviewed_selection_id = None
    fixture.repository.save_session(session)
    fixture.repository.connection.commit()
    with pytest.raises(Bundle3ContextError):
        fixture.bundle3_context()


def test_4f_applied_member_outside_reviewed_self_team_fails_closed() -> None:
    with pytest.raises(SelectedBuildContextError):
        project_selected_three_builds(
            selected_three=(MASUKAANYA, HASSAMU, "ミミッキュ"),
            reviewed_self_team=SELF_TEAM_SIX,
            reviewed_self_team_build=None,
            reviewed_self_team_build_sha256=None,
        )


# --- 5: detailed build hash / name mismatch fails closed ---------------------


def test_5a_detailed_build_sha256_mismatch_fails_closed() -> None:
    build = _detailed_team_build()
    with pytest.raises(SelectedBuildContextError):
        project_selected_three_builds(
            selected_three=SELECTED_THREE,
            reviewed_self_team=SELF_TEAM_SIX,
            reviewed_self_team_build=build,
            reviewed_self_team_build_sha256="f" * 64,
        )


def test_5b_detailed_build_names_not_matching_reviewed_self_team_fails_closed() -> None:
    build = _detailed_team_build()
    shuffled_team = (*SELECTED_THREE, "ミミッキュ", *BENCHED[1:])
    with pytest.raises(SelectedBuildContextError):
        project_selected_three_builds(
            selected_three=SELECTED_THREE,
            reviewed_self_team=shuffled_team,
            reviewed_self_team_build=build,
            reviewed_self_team_build_sha256=build.sha256(),
        )


def test_5c_build_without_hash_and_hash_without_build_both_fail_closed() -> None:
    build = _detailed_team_build()
    with pytest.raises(SelectedBuildContextError):
        project_selected_three_builds(
            selected_three=SELECTED_THREE,
            reviewed_self_team=SELF_TEAM_SIX,
            reviewed_self_team_build=build,
            reviewed_self_team_build_sha256=None,
        )
    with pytest.raises(SelectedBuildContextError):
        project_selected_three_builds(
            selected_three=SELECTED_THREE,
            reviewed_self_team=SELF_TEAM_SIX,
            reviewed_self_team_build=None,
            reviewed_self_team_build_sha256="f" * 64,
        )


# --- 6/7/8/10: battle memory content ----------------------------------------


def test_6_turn_seven_memory_is_exactly_turns_one_to_six(fixture: Bundle3Fixture) -> None:
    memory = fixture.build_request().battle_memory
    assert memory.turn_numbers == (1, 2, 3, 4, 5, 6)
    assert all(row.identity.match_id == fixture.match_id for row in memory.turns)


def test_7_confirmed_own_actions_are_chronological_and_complete(
    fixture: Bundle3Fixture,
) -> None:
    memory = fixture.build_request().battle_memory
    observed = [
        (row.own_action.action_type, row.own_action.action_name) for row in memory.turns
    ]
    assert observed == [
        (ActionType.MOVE, "トリックフラワー"),
        (ActionType.MOVE, "トリックフラワー"),
        (ActionType.SWITCH, HASSAMU),
        (ActionType.MOVE, "バレットパンチ"),
        (ActionType.MOVE, "バレットパンチ"),
        (ActionType.SWITCH, GABURIASU),
    ]
    assert all(
        row.own_action.knowledge_status is KnowledgeStatus.CONFIRMED for row in memory.turns
    )


def test_8_confirmed_opponent_actions_included_unrecorded_ones_typed_unknown(
    fixture: Bundle3Fixture,
) -> None:
    memory = fixture.build_request().battle_memory
    by_turn = {row.identity.turn_number: row.opponent_action for row in memory.turns}
    assert by_turn[1].knowledge_status is KnowledgeStatus.CONFIRMED
    assert (by_turn[1].action_type, by_turn[1].action_name) == (ActionType.MOVE, "じしん")
    assert by_turn[6].action_name == "キノコのほうし"
    for unknown_turn in (2, 5):
        assert by_turn[unknown_turn].knowledge_status is KnowledgeStatus.UNKNOWN
        assert by_turn[unknown_turn].action_type is None
        assert by_turn[unknown_turn].action_name is None


def test_8b_unknown_action_order_is_preserved_as_unknown(fixture: Bundle3Fixture) -> None:
    memory = fixture.build_request().battle_memory
    by_turn = {row.identity.turn_number: row.action_order for row in memory.turns}
    assert by_turn[2] is ActionOrder.UNKNOWN
    assert by_turn[1] is ActionOrder.SELF_FIRST
    assert by_turn[3] is ActionOrder.OPPONENT_FIRST


def test_10_reviewed_delta_consequences_are_preserved_verbatim(
    fixture: Bundle3Fixture,
) -> None:
    canonical = canonical_rich_request_dict(fixture.build_request())
    memory = {row["turn_number"]: row for row in canonical["battle_memory"]}

    # HP change (Turn 1) and the turn-start actives that framed it.
    assert memory[1]["result"]["opponent_side"]["hp_bucket"]["observation"] == "CHANGED"
    assert memory[1]["result"]["opponent_side"]["hp_bucket"]["after_value"] == "51-60"
    assert memory[1]["turn_start_self_active"]["value"] == MASUKAANYA
    assert memory[1]["turn_start_opponent_active"]["value"] == "Garchomp"

    # Status change (Turn 2).
    assert memory[2]["result"]["self_side"]["status"]["after_value"] == "PARALYSIS"

    # Switch result (Turn 3) -- represented by the existing active delta.
    assert memory[3]["result"]["self_side"]["active"]["after_value"] == HASSAMU

    # Faint result (Turn 4) -- hp_bucket CHANGED to ZERO plus the confirmed
    # replacement active. No redundant event system.
    assert memory[4]["result"]["opponent_side"]["hp_bucket"]["after_value"] == "0"
    assert memory[4]["result"]["opponent_side"]["active"]["after_value"] == "Amoonguss"

    # Stage change (Turn 5) and an UNCHANGED field that stays UNCHANGED.
    assert memory[5]["result"]["self_side"]["attack_stage"]["after_value"] == 2
    assert memory[5]["result"]["self_side"]["speed_stage"]["observation"] == "UNCHANGED"

    # Reviewed provenance survives untouched.
    assert memory[5]["result"]["self_side"]["attack_stage"]["provenance_chain"] == ["HUMAN_INPUT"]
    assert memory[6]["result"]["confirmation"]["confirmed_by_human"] is True


# --- 9: nothing unconfirmed leaks into the request ---------------------------


def test_9_prior_advice_predictions_and_ocr_prefills_are_absent(
    fixture: Bundle3Fixture,
) -> None:
    from maple_next.domain.turn_state import LegalActionPrefillDraft

    fixture.repository.append_legal_action_prefill_draft(
        LegalActionPrefillDraft(
            prefill_id="prefill-leak-probe",
            identity=fixture.identity(CURRENT_TURN_NUMBER),
            based_on_confirmed_state_id=fixture.confirmed_state_id(CURRENT_TURN_NUMBER),
            action_type=ActionType.MOVE,
            action_name="OCR_CANDIDATE_LEAK_PROBE",
            derived_at_utc=CONFIRMED_AT,
        )
    )
    fixture.repository.connection.commit()

    payload = encode_canonical_rich_request(fixture.build_request()).decode("utf-8")
    for forbidden in ("prefill-leak-probe", "OCR_CANDIDATE_LEAK_PROBE"):
        assert forbidden not in payload

    canonical = canonical_rich_request_dict(fixture.build_request())
    # ``requested_output_schema`` legitimately names the *response* fields;
    # the Bundle 3 memory itself must contain none of them.
    memory_text = json.dumps(canonical["battle_memory"], ensure_ascii=False)
    for forbidden in ("opponent_prediction", "recommended_action", "rationale", "prefill"):
        assert forbidden not in memory_text
    allowed_memory_keys = {
        "session_id",
        "match_id",
        "generation",
        "turn_id",
        "turn_number",
        "battle_revision",
        "reviewed_confirmed_state_id",
        "turn_start_self_active",
        "turn_start_opponent_active",
        "own_action",
        "opponent_action",
        "action_order",
        "result",
    }
    for row in canonical["battle_memory"]:
        assert set(row) == allowed_memory_keys


# --- 11/12/13/14: fail-closed history integrity ------------------------------


def _foreign_delta_row(fixture: Bundle3Fixture, *, delta_id: str, session_id: str) -> None:
    """Append one extra delta based on Turn 2's reviewed state."""

    fixture.repository.append_action_result_delta(
        ActionResultDelta(
            delta_id=delta_id,
            identity=fixture.identity(2, session_id=session_id),
            based_on_confirmed_state_id=fixture.confirmed_state_id(2),
            self_side=_side_delta(),
            opponent_side=_side_delta(),
            weather=FieldDelta.unchanged(provenance_chain=_CARRY),
            terrain=FieldDelta.unchanged(provenance_chain=_CARRY),
            confirmation=_confirmation(),
        )
    )
    fixture.repository.connection.commit()


def test_11_foreign_session_completion_row_fails_closed(fixture: Bundle3Fixture) -> None:
    _foreign_delta_row(fixture, delta_id="delta-foreign", session_id="other-session")
    fixture.repository.connection.execute(
        """
        INSERT INTO rich_action_completions (
            transaction_id, session_id, match_id, generation, turn_id, turn_number,
            battle_revision, based_on_confirmed_state_id, own_action_type,
            own_action_name, opponent_action_type, opponent_action_name,
            action_order, delta_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "completion-foreign",
            "other-session",
            fixture.match_id,
            fixture.generation,
            "turn-foreign",
            2,
            2,
            fixture.confirmed_state_id(2),
            "MOVE",
            "foreign",
            "UNKNOWN",
            "UNKNOWN",
            "UNKNOWN",
            "delta-foreign",
            CONFIRMED_AT,
        ),
    )
    fixture.repository.connection.commit()
    with pytest.raises(BattleMemoryError):
        fixture.bundle3_context()


def test_11b_foreign_generation_delta_alone_is_not_silently_filtered(
    fixture: Bundle3Fixture,
) -> None:
    _foreign_delta_row(fixture, delta_id="delta-foreign-gen", session_id=fixture.session_id)
    with pytest.raises(BattleMemoryError):
        fixture.bundle3_context()


def test_12_completion_for_the_current_turn_fails_closed(fixture: Bundle3Fixture) -> None:
    fixture.repository.record_rich_action_completion(
        transaction_id="completion-current",
        identity=fixture.identity(CURRENT_TURN_NUMBER),
        own_action_type=ActionType.MOVE,
        own_action_name="じしん",
        opponent_action_type=None,
        opponent_action_name=None,
        action_order=ActionOrder.UNKNOWN,
        delta=ActionResultDelta(
            delta_id="delta-current",
            identity=fixture.identity(CURRENT_TURN_NUMBER),
            based_on_confirmed_state_id=fixture.confirmed_state_id(CURRENT_TURN_NUMBER),
            self_side=_side_delta(),
            opponent_side=_side_delta(),
            weather=FieldDelta.unchanged(provenance_chain=_CARRY),
            terrain=FieldDelta.unchanged(provenance_chain=_CARRY),
            confirmation=_confirmation(),
        ),
    )
    with pytest.raises(BattleMemoryError):
        fixture.bundle3_context()


def test_12b_future_turn_completion_fails_closed(fixture: Bundle3Fixture) -> None:
    future_identity = TurnIdentity(
        session_id=fixture.session_id,
        match_id=fixture.match_id,
        generation=fixture.generation,
        turn_id="turn-8",
        turn_number=8,
        battle_revision=8,
    )
    fixture.repository.append_confirmed_turn_state(
        ConfirmedTurnState(
            confirmed_state_id="state-8",
            identity=future_identity,
            previous_confirmed_state_id=fixture.confirmed_state_id(CURRENT_TURN_NUMBER),
            self_side=_confirmed_side(GABURIASU),
            opponent_side=_confirmed_side("Amoonguss"),
            weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
            terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
            confirmation=_confirmation(),
        )
    )
    fixture.repository.connection.commit()
    fixture.repository.record_rich_action_completion(
        transaction_id="completion-8",
        identity=future_identity,
        own_action_type=ActionType.MOVE,
        own_action_name="じしん",
        opponent_action_type=None,
        opponent_action_name=None,
        action_order=ActionOrder.UNKNOWN,
        delta=ActionResultDelta(
            delta_id="delta-8",
            identity=future_identity,
            based_on_confirmed_state_id="state-8",
            self_side=_side_delta(),
            opponent_side=_side_delta(),
            weather=FieldDelta.unchanged(provenance_chain=_CARRY),
            terrain=FieldDelta.unchanged(provenance_chain=_CARRY),
            confirmation=_confirmation(),
        ),
    )
    with pytest.raises(BattleMemoryError):
        fixture.bundle3_context()


def test_13_duplicate_turn_completion_fails_closed(fixture: Bundle3Fixture) -> None:
    _foreign_delta_row(fixture, delta_id="delta-2-dup", session_id=fixture.session_id)
    fixture.repository.connection.execute(
        """
        INSERT INTO rich_action_completions (
            transaction_id, session_id, match_id, generation, turn_id, turn_number,
            battle_revision, based_on_confirmed_state_id, own_action_type,
            own_action_name, opponent_action_type, opponent_action_name,
            action_order, delta_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "completion-2-dup",
            fixture.session_id,
            fixture.match_id,
            fixture.generation,
            "turn-2-dup",
            2,
            2,
            fixture.confirmed_state_id(2),
            "MOVE",
            "duplicate",
            "UNKNOWN",
            "UNKNOWN",
            "UNKNOWN",
            "delta-2-dup",
            CONFIRMED_AT,
        ),
    )
    fixture.repository.connection.commit()
    with pytest.raises(BattleMemoryError):
        fixture.bundle3_context()


def test_14a_missing_prior_completion_fails_closed(fixture: Bundle3Fixture) -> None:
    fixture.repository.connection.execute(
        "DELETE FROM rich_action_completions WHERE turn_id = ?", (fixture.turn_id(3),)
    )
    fixture.repository.connection.commit()
    with pytest.raises(BattleMemoryError):
        fixture.bundle3_context()


def test_14b_missing_linked_reviewed_delta_fails_closed(fixture: Bundle3Fixture) -> None:
    """The linked delta is required; its absence is never treated as "no result"."""

    states = fixture.repository.list_confirmed_turn_states_for_match(
        session_id=fixture.session_id,
        match_id=fixture.match_id,
        generation=fixture.generation,
    )
    completions = fixture.repository.list_rich_action_completion_candidates_for_confirmed_states(
        tuple(state.confirmed_state_id for state in states)
    )
    deltas = fixture.repository.list_action_result_delta_candidates_for_confirmed_states(
        tuple(state.confirmed_state_id for state in states)
    )
    surviving = tuple(delta for delta in deltas if delta.delta_id != "delta-3")
    with pytest.raises(BattleMemoryError):
        build_battle_memory(
            current_identity=fixture.identity(CURRENT_TURN_NUMBER),
            current_confirmed_state=fixture.repository.get_confirmed_turn_state(
                fixture.confirmed_state_id(CURRENT_TURN_NUMBER)
            ),
            match_confirmed_states=states,
            completion_candidates=completions,
            delta_candidates=surviving,
        )


def test_14c_broken_confirmed_state_chain_fails_closed(fixture: Bundle3Fixture) -> None:
    fixture.repository.connection.execute(
        "UPDATE confirmed_turn_states SET previous_confirmed_state_id = NULL "
        "WHERE confirmed_state_id = ?",
        (fixture.confirmed_state_id(5),),
    )
    fixture.repository.connection.commit()
    with pytest.raises(BattleMemoryError):
        fixture.bundle3_context()


# --- 15/16/17/18: determinism and rebinding ----------------------------------


def test_15_database_insertion_order_does_not_change_memory_order_or_hash(
    tmp_path: Path,
) -> None:
    forward = Bundle3Fixture(tmp_path, db_name="forward.db")
    shuffled = Bundle3Fixture(
        tmp_path, db_name="shuffled.db", completion_order=(6, 3, 1, 5, 2, 4)
    )
    forward_request = forward.build_request()
    shuffled_request = shuffled.build_request()
    assert forward_request.battle_memory.turn_numbers == (1, 2, 3, 4, 5, 6)
    assert shuffled_request.battle_memory.turn_numbers == (1, 2, 3, 4, 5, 6)
    assert encode_canonical_rich_request(forward_request) == encode_canonical_rich_request(
        shuffled_request
    )
    assert forward_request.request_hash == shuffled_request.request_hash
    forward.close()
    shuffled.close()


def test_16_memory_semantic_change_changes_the_request_hash(tmp_path: Path) -> None:
    baseline = Bundle3Fixture(tmp_path, db_name="baseline.db")
    changed = Bundle3Fixture(tmp_path, db_name="changed.db")
    changed.repository.connection.execute(
        "UPDATE rich_action_completions SET opponent_action_type = 'MOVE', "
        "opponent_action_name = 'まもる' WHERE turn_id = ?",
        (changed.turn_id(2),),
    )
    changed.repository.connection.commit()
    assert baseline.build_request().request_hash != changed.build_request().request_hash
    baseline.close()
    changed.close()


def test_17_restart_rebuild_reproduces_identical_request_bytes_and_hash(
    fixture: Bundle3Fixture,
) -> None:
    job = fixture.application.request_rich_turn_advice("command-b3")
    authorized = fixture.build_request()
    assert job.request_payload_hash == authorized.request_hash

    fixture.repository.close()
    restarted_repository = SQLiteRepository(fixture.db_path)
    restarted = MatchApplication(restarted_repository, fixture.db_path.parent / "exports")
    rebuilt = restarted.build_rich_turn_advice_transport_request(job)
    assert rebuilt.request_hash == job.request_payload_hash
    assert encode_canonical_rich_request(rebuilt) == encode_canonical_rich_request(authorized)
    assert rebuilt.battle_memory.turn_numbers == (1, 2, 3, 4, 5, 6)
    restarted_repository.close()


def test_18_applied_selection_rebind_invalidates_the_previously_built_context(
    fixture: Bundle3Fixture,
) -> None:
    """A changed bring must never be silently rebuilt as the authorized one."""

    job = fixture.application.request_rich_turn_advice("command-b3")
    # ``applied_selections`` holds exactly one row per session, so a real
    # re-APPLY rebinds that row's contents rather than appending a second.
    fixture.repository.connection.execute(
        "UPDATE applied_selections SET selected_three_json = ?, backline_json = ?",
        (
            json.dumps([MASUKAANYA, HASSAMU, "ハバタクカミ"], ensure_ascii=False),
            json.dumps([HASSAMU, "ハバタクカミ"], ensure_ascii=False),
        ),
    )
    fixture.repository.connection.commit()
    with pytest.raises(DomainError):
        fixture.application.build_rich_turn_advice_transport_request(job)


def test_18b_applied_selection_sourced_from_a_foreign_advice_fails_closed(
    fixture: Bundle3Fixture,
) -> None:
    job = fixture.application.request_rich_turn_advice("command-b3")
    fixture.repository.insert_session(
        BattleSession(
            session_id="session-other",
            match_id="match-other",
            generation=10,
            state=BattleState.SELECTION_OPEN,
            battle_revision=1,
            active_slot=None,
        )
    )
    fixture.repository.append_selection_advice(
        "advice-other",
        "session-other",
        fixture.advice_job_id + "-other",
        SELECTED_THREE,
        MASUKAANYA,
        (HASSAMU, GABURIASU),
    )
    fixture.repository.connection.execute(
        "UPDATE applied_selections SET source_advice_id = 'advice-other'"
    )
    fixture.repository.connection.commit()
    with pytest.raises(DomainError):
        fixture.application.build_rich_turn_advice_transport_request(job)


# --- 19/20: Bundle 1 / Bundle 2 regressions stay green -----------------------


def test_19_bundle1_action_result_completions_still_round_trip(
    fixture: Bundle3Fixture,
) -> None:
    """Bundle 1 completion + linked delta remain readable and unmodified."""

    completion = fixture.repository.get_rich_action_completion_by_turn(fixture.turn_id(4))
    assert completion is not None
    assert completion["own_action_name"] == "バレットパンチ"
    assert completion["delta_id"] == "delta-4"
    delta = fixture.repository.get_action_result_delta("delta-4")
    assert delta.opponent_side.hp_bucket.after_value is HpBucket.ZERO
    memory = fixture.build_request().battle_memory
    assert memory.turns[3].result == delta


def test_20_bundle2_legal_switches_and_status_survive_in_v4(
    fixture: Bundle3Fixture,
) -> None:
    request = fixture.build_request()
    assert request.legal_switches == (MASUKAANYA, HASSAMU)
    assert request.legal_switches_status == "CONFIRMED_NONEMPTY"
    canonical = canonical_rich_request_dict(request)
    assert canonical["legal_switches"] == sorted((MASUKAANYA, HASSAMU))
    assert canonical["legal_switches_status"] == "CONFIRMED_NONEMPTY"


def test_20b_explicit_send_gate_still_consumes_exactly_one_attempt(
    fixture: Bundle3Fixture,
) -> None:
    fixture.application.request_rich_turn_advice("command-b3")
    with pytest.raises(DomainError):
        fixture.application.request_rich_turn_advice("command-b3-second")


# --- Application gate: broken Bundle 3 context blocks the request ------------


def test_broken_history_blocks_the_application_request_command(
    fixture: Bundle3Fixture,
) -> None:
    """Bundle 3 validation runs after the existing gate, inside request assembly."""

    fixture.repository.connection.execute(
        "DELETE FROM rich_action_completions WHERE turn_id = ?", (fixture.turn_id(5),)
    )
    fixture.repository.connection.commit()
    with pytest.raises(DomainError):
        fixture.application.request_rich_turn_advice("command-b3")


def test_unreviewed_next_turn_draft_never_enters_battle_memory(
    fixture: Bundle3Fixture,
) -> None:
    """A draft is not a confirmation and can never become memory."""

    from maple_next.domain.turn_state import NextTurnStateDraft

    fixture.repository.upsert_next_turn_state_draft(
        NextTurnStateDraft(
            draft_id="draft-leak-probe",
            identity=fixture.identity(3),
            based_on_confirmed_state_id=fixture.confirmed_state_id(2),
            source_delta_id="delta-2",
            self_side=_confirmed_side("DRAFT_LEAK_PROBE"),
            opponent_side=_confirmed_side("Garchomp"),
            weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
            terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
            derived_at_utc=CONFIRMED_AT,
        )
    )
    fixture.repository.connection.commit()
    payload = encode_canonical_rich_request(fixture.build_request()).decode("utf-8")
    assert "draft-leak-probe" not in payload
    assert "DRAFT_LEAK_PROBE" not in payload


# --- Turn 1: empty memory is legitimate, never a send blocker ----------------


def test_turn_one_has_empty_memory_and_is_not_blocked() -> None:
    identity = TurnIdentity(
        session_id="s",
        match_id="m",
        generation=9,
        turn_id="turn-1",
        turn_number=1,
        battle_revision=1,
    )
    state = ConfirmedTurnState(
        confirmed_state_id="state-1",
        identity=identity,
        previous_confirmed_state_id=None,
        self_side=_confirmed_side(MASUKAANYA),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    memory = build_battle_memory(
        current_identity=identity,
        current_confirmed_state=state,
        match_confirmed_states=(state,),
        completion_candidates=(),
        delta_candidates=(),
    )
    assert memory == BattleMemory()
    assert memory.turns == ()
