"""Issue #31 Bundle B narrow remediation: forge resistance, rebuild, export selection.

Companion to ``test_issue31_turn_state_provider_export_bundle_b.py`` (kept
unmodified in count/behavior beyond the required forge-resistance
migration documented in that file). This file adds focused coverage for
the remediation work: the complete canonical rich request contract, legacy
byte-for-byte equality goldens, the forge-resistant
``BattleApplication.request_rich_turn_advice`` application API, the
offline rebuild boundary, OPEN-draft full-chain validation, hash coverage
per field, and repository-backed ``maple-match.v3`` export selection.

No test in this file sends anything over a network or touches a real
provider.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from maple_next.application.match_export_v3 import MatchExportV3Error, parse_match_export_v3
from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.domain.enums import ActionType, BattleState, HpBucket, MatchOutcome
from maple_next.domain.legal_switches import LegalSwitchConfirmation, LegalSwitchStatus
from maple_next.domain.models import (
    AppliedSelectionSnapshot,
    BattleTurn,
    RecordedAction,
    ReviewedBoardSnapshot,
    SelectionFacts,
    StatStages,
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
    FixedEvidenceMetadata,
    Known,
    NextTurnStateDraft,
    ProvenanceStep,
    SideDelta,
    SideState,
    TurnIdentity,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers import turn_request as legacy_turn_request
from maple_next.providers.turn_advice_rich_state import (
    RICH_STATE_REQUEST_CONTRACT_VERSION,
    build_rich_provider_prompt,
    build_rich_provider_request_body,
    build_rich_state_turn_advice_request,
    canonical_rich_request_dict,
)
from maple_next.providers.turn_response import TurnAdviceSchemaError, turn_advice_body_from_dict
from maple_next.workers.contracts.models import JobEnvelope, JobType
from tests.fixtures.bundle3 import (
    default_rules_context,
    names_only_bundle3_context,
    seed_selection_advice_binding,
)
from tests.fixtures.turn_advice import build_sample_request

_HUMAN = (ProvenanceStep.HUMAN_INPUT,)
CONFIRMED_AT = "2026-08-06T00:00:00+00:00"


# --- Fixture builder: a minimal end-to-end rich-state-contract session -----


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


class RichSessionFixture:
    """Builds one BATTLE_READY -> TURN_REVIEWED session with a confirmed turn state."""

    def __init__(self, tmp_path):
        self.repository = SQLiteRepository(tmp_path / "runtime" / "maple.db")
        self.application = MatchApplication(self.repository, tmp_path / "user-data" / "exports")
        self.session_id = "session-remediation-1"
        self.match_id = "match-remediation-1"
        self.generation = 9
        self.turn_id = "turn-1"
        self.turn_number = 1
        self.battle_revision = 3
        self.confirmed_state_id = "state-1"

        from maple_next.domain.champions_rules import current_rules_pin_for_new_match
        from maple_next.domain.models import BattleSession

        # Bundle 4 (Gemini V2): pin the real bundled rules snapshot -- exactly
        # what ``BattleApplication.new_match`` would have written -- so this
        # pre-Bundle-4 fixture's session stays provider-ready under the
        # Bundle 4 fail-closed gate.
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
            rules_ruleset_id=rules_pin.ruleset_id,
            rules_ruleset_version=rules_pin.ruleset_version,
            rules_snapshot_id=rules_pin.rules_snapshot_id,
            rules_facts_sha256=rules_pin.rules_facts_sha256,
        )
        self.repository.insert_session(session)
        self.repository.append_turn(
            self.session_id, BattleTurn(turn_id=self.turn_id, turn_number=self.turn_number)
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
        # job -> reviewed-selection chain is complete.
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
        self, *, evidence_id: str | None = None, previous_confirmed_state_id: str | None = None
    ) -> ConfirmedTurnState:
        state = ConfirmedTurnState(
            confirmed_state_id=self.confirmed_state_id,
            identity=self.identity(),
            previous_confirmed_state_id=previous_confirmed_state_id,
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

    def legal_switch_confirmation(
        self,
        *,
        legal_switches: tuple[str, ...] = ("Gholdengo",),
        status: LegalSwitchStatus | None = None,
        based_on_confirmed_state_id: str | None = None,
    ) -> LegalSwitchConfirmation:
        resolved_status = (
            status
            if status is not None
            else (
                LegalSwitchStatus.CONFIRMED_NONEMPTY
                if legal_switches
                else LegalSwitchStatus.CONFIRMED_NONE
            )
        )
        return LegalSwitchConfirmation(
            confirmation_id="switch-confirm-1",
            identity=self.identity(),
            based_on_confirmed_state_id=based_on_confirmed_state_id or self.confirmed_state_id,
            applied_selection_id="applied-1",
            legal_switches=legal_switches,
            status=resolved_status,
            confirmation=_confirmation(),
        )


@pytest.fixture
def rich_fixture(tmp_path) -> RichSessionFixture:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    fixture.append_legal_actions()
    fixture.repository.upsert_legal_switch_confirmation(fixture.legal_switch_confirmation())
    fixture.repository.connection.commit()
    return fixture


# --- 1. Legacy byte-equality goldens ----------------------------------------
#
# All values below were originally captured by directly loading
# ``src/maple_next/providers/turn_request.py`` as it existed at the accepted
# Bundle A base commit ``4f428a3730631015aedbf981d89f6540d40475ac`` (via
# ``git show <sha>:<path>`` and ``importlib``, never through the modified
# implementation) and hashing its actual output. They are therefore golden
# evidence of the accepted legacy behavior, not newly generated expected
# output from this remediation's code.
#
# Bundle 4 (Gemini V2) deliberately rewrote the shared
# ``_TURN_INITIAL_PROMPT`` "general Pokémon Champions knowledge" authority
# sentence (the Bundle 4 PROMPT AUTHORITY FIX: rules_context, when present,
# is now authoritative for Champions-specific battle rules, and general
# knowledge is explicitly non-authoritative background only). That text is
# shared by the legacy v1/v2 prompt and the rich v4->v5 prompt alike, so the
# five hashes below that are derived from ``_TURN_INITIAL_PROMPT`` content
# (``_TURN_INITIAL_PROMPT_sha256``, ``v1_prompt_sha256``, ``v1_body_sha256``,
# ``v2_prompt_sha256``, ``v2_body_sha256``) were recomputed against the new
# text via the actual (modified) implementation and are pinned here as the
# new goldens. ``REQUESTED_OUTPUT_SCHEMA_sha256`` and the two
# ``*_canonical_dict_bytes_sha256`` values are untouched by that prompt
# text change and remain the original Bundle A goldens.
_GOLDEN = {
    "_TURN_INITIAL_PROMPT_sha256": (
        "c9cca378178603aa7f08de3dd01a43a1d911e22a3fc8f8a300534e4b9408cb4a"
    ),
    "REQUESTED_OUTPUT_SCHEMA_sha256": (
        "2d7310f91b45e5a6997c516d344649927c5729df01023d9dcbdb52872a9bb32a"
    ),
    "v1_canonical_dict_bytes_sha256": (
        "ba25ea9416cef7aab9b7069ef5043a7d9252de20ccc0ae43edb4047103b463ae"
    ),
    "v1_prompt_sha256": "abc35a54473c491d9e3046d861c30ae4d5729bee26000352266012732e9f4bd5",
    "v1_body_sha256": "380f4d7c3b697e17e78ee07ba6dd8ae140698d09c40e20b45b07a25fb7a332fb",
    "v2_canonical_dict_bytes_sha256": (
        "fa149601cd8b65256b821d54f65c871d3cd651c76e3089c28a28900e91f6099c"
    ),
    "v2_prompt_sha256": "61de56d0ee27aebf5581b4922772ef1647e902af4c105b59fe33d1d0df9c558f",
    "v2_body_sha256": "04221393dc3b7dd022a14c10e0854d57515e1e627a3f1bfa39b5a234a18a14b4",
}

_V2_SELF_TEAM = ("Pikachu", "Gholdengo", "Dragonite", "Dondozo", "Hatterene", "Urshifu")


def _v2_member(name: str, index: int) -> ChampionsPokemonBuild:
    return ChampionsPokemonBuild(
        pokemon_name=name,
        moves=(f"{name}-A", f"{name}-B"),
        held_item=None if index == 0 else f"{name}-item",
        ability=f"{name}-ability",
        nature="Serious",
        stat_points=ChampionsStatPoints(hp=10, speed=10),
    )


def _build_sample_v2_request():
    """A real, complete v2 request built with an actual ``ChampionsTeamBuild`` fixture.

    Not a mocked or partial v2 shape -- every field
    ``build_turn_advice_request`` requires for a detailed (v2) request is
    populated exactly as the accepted Bundle A base would build it.
    """

    team_build = ChampionsTeamBuild(
        schema_version=CHAMPIONS_SCHEMA_VERSION,
        game=CHAMPIONS_GAME,
        name="Golden team",
        battle_format=CHAMPIONS_BATTLE_FORMAT,
        members=tuple(_v2_member(name, index) for index, name in enumerate(_V2_SELF_TEAM)),
    )
    snapshot = ReviewedBoardSnapshot(
        reviewed_board_id="board-v2",
        turn_id="turn-1",
        self_active=_V2_SELF_TEAM[0],
        opponent_active="Garchomp",
        self_hp=HpBucket.FIFTY_ONE_TO_SIXTY,
        opponent_hp=HpBucket.UNKNOWN,
        self_status="NONE",
        opponent_status="UNKNOWN",
        self_stages=StatStages(speed=1),
        opponent_stages=StatStages(defense=-1),
        weather="RAIN",
        terrain="UNKNOWN",
        self_side_effects=("REFLECT",),
        opponent_side_effects=("STEALTH_ROCK",),
    )
    return legacy_turn_request.build_turn_advice_request(
        session_id="session-v2",
        match_id="match-v2",
        generation=7,
        turn_number=1,
        battle_revision=12,
        reviewed_snapshot_id=snapshot.reviewed_board_id,
        reviewed_snapshot=snapshot,
        self_active=_V2_SELF_TEAM[0],
        selected_three=(_V2_SELF_TEAM[0], _V2_SELF_TEAM[1], _V2_SELF_TEAM[2]),
        legal_actions=(
            legacy_turn_request.LegalAction(
                action_id="move-1",
                action_type=ActionType.MOVE,
                action_name="Thunderbolt",
                owner_active=_V2_SELF_TEAM[0],
            ),
            legacy_turn_request.LegalAction(
                action_id="switch-1",
                action_type=ActionType.SWITCH,
                action_name=_V2_SELF_TEAM[1],
                switch_target=_V2_SELF_TEAM[1],
            ),
        ),
        self_team_build=team_build,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_legacy_initial_prompt_byte_identical_to_golden() -> None:
    assert _sha256(legacy_turn_request._TURN_INITIAL_PROMPT.encode("utf-8")) == _GOLDEN[
        "_TURN_INITIAL_PROMPT_sha256"
    ]


def test_legacy_requested_output_schema_byte_identical_to_golden() -> None:
    encoded = json.dumps(
        legacy_turn_request.REQUESTED_OUTPUT_SCHEMA, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert _sha256(encoded) == _GOLDEN["REQUESTED_OUTPUT_SCHEMA_sha256"]


def test_legacy_v1_canonical_prompt_body_byte_identical_to_golden() -> None:
    request = build_sample_request()
    assert _sha256(legacy_turn_request.encode_canonical_request(request)) == _GOLDEN[
        "v1_canonical_dict_bytes_sha256"
    ]
    assert _sha256(legacy_turn_request.build_provider_prompt(request).encode("utf-8")) == _GOLDEN[
        "v1_prompt_sha256"
    ]
    body_bytes = json.dumps(
        legacy_turn_request.build_provider_request_body(request),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert _sha256(body_bytes) == _GOLDEN["v1_body_sha256"]


def test_legacy_v2_canonical_prompt_body_byte_identical_to_golden() -> None:
    request = _build_sample_v2_request()
    assert _sha256(legacy_turn_request.encode_canonical_request(request)) == _GOLDEN[
        "v2_canonical_dict_bytes_sha256"
    ]
    assert _sha256(legacy_turn_request.build_provider_prompt(request).encode("utf-8")) == _GOLDEN[
        "v2_prompt_sha256"
    ]
    body_bytes = json.dumps(
        legacy_turn_request.build_provider_request_body(request),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert _sha256(body_bytes) == _GOLDEN["v2_body_sha256"]


# --- 2. Canonical rich request completeness ----------------------------------


def test_rich_request_contains_required_fields(rich_fixture: RichSessionFixture) -> None:
    state = rich_fixture.repository.get_confirmed_turn_state(rich_fixture.confirmed_state_id)
    actions = rich_fixture.repository.list_confirmed_legal_action_selections_for_identity(
        rich_fixture.identity()
    )
    request = build_rich_state_turn_advice_request(
        confirmed_state=state,
        confirmed_legal_actions=actions,
        current_identity=rich_fixture.identity(),
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=rich_fixture.legal_switch_confirmation(
            based_on_confirmed_state_id=state.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
    )
    assert request.contract_version == RICH_STATE_REQUEST_CONTRACT_VERSION
    assert request.prompt_version == legacy_turn_request.TURN_PROMPT_VERSION
    assert request.job_type == "TURN_ADVICE"
    assert request.identity == rich_fixture.identity()
    assert request.reviewed_confirmed_state_id == state.confirmed_state_id
    assert request.previous_confirmed_state_id == state.previous_confirmed_state_id
    assert request.selected_three == ("Dondozo", "Gholdengo", "Urshifu")
    assert request.self_active == "Dondozo"
    assert {a.action_id for a in request.legal_actions} == {"legal-move-1", "legal-switch-1"}
    move_action = next(a for a in request.legal_actions if a.action_type is ActionType.MOVE)
    assert move_action.owner_active == "Dondozo"
    switch_action = next(a for a in request.legal_actions if a.action_type is ActionType.SWITCH)
    assert switch_action.switch_target == "Gholdengo"
    assert request.requested_output_schema == legacy_turn_request.REQUESTED_OUTPUT_SCHEMA
    assert request.state_confirmation == state.confirmation
    assert len(request.request_hash) == 64


def test_rich_request_prompt_uses_shared_renderer_not_a_copy() -> None:
    """The rich prompt must be built through the same private renderer as legacy."""

    import inspect

    from maple_next.providers.turn_advice_rich_state import build_rich_provider_prompt
    from maple_next.providers.turn_request import (
        _render_provider_prompt_from_canonical_request as shared_renderer,
    )

    source = inspect.getsource(build_rich_provider_prompt)
    assert "_render_provider_prompt_from_canonical_request" in source
    assert shared_renderer.__module__ == "maple_next.providers.turn_request"


def test_rich_prompt_requires_japanese_only_for_human_facing_text(
    rich_fixture: RichSessionFixture,
) -> None:
    state = rich_fixture.repository.get_confirmed_turn_state(rich_fixture.confirmed_state_id)
    actions = rich_fixture.repository.list_confirmed_legal_action_selections_for_identity(
        rich_fixture.identity()
    )
    request = build_rich_state_turn_advice_request(
        confirmed_state=state,
        confirmed_legal_actions=actions,
        current_identity=rich_fixture.identity(),
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=rich_fixture.legal_switch_confirmation(
            based_on_confirmed_state_id=state.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
    )
    canonical_before = canonical_rich_request_dict(request)
    request_hash_before = request.request_hash

    prompt = build_rich_provider_prompt(request)

    assert "opponent_prediction.summary" in prompt
    assert "decision-oriented, non-repetitive natural Japanese" in prompt
    assert "these human-facing fields must be Japanese" in prompt
    assert "Use at most two reasons" in prompt
    assert "actionable current-turn risk" in prompt
    assert "otherwise return an empty warnings" in prompt
    assert "Do not translate or alter machine/contract values" in prompt
    assert "action_id" in prompt
    assert "action_type" in prompt
    assert "action_name" in prompt
    assert canonical_rich_request_dict(request) == canonical_before
    assert request.request_hash == request_hash_before


def test_rich_japanese_instruction_preserves_strict_response_schema(
    rich_fixture: RichSessionFixture,
) -> None:
    state = rich_fixture.repository.get_confirmed_turn_state(rich_fixture.confirmed_state_id)
    actions = rich_fixture.repository.list_confirmed_legal_action_selections_for_identity(
        rich_fixture.identity()
    )
    request = build_rich_state_turn_advice_request(
        confirmed_state=state,
        confirmed_legal_actions=actions,
        current_identity=rich_fixture.identity(),
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=rich_fixture.legal_switch_confirmation(
            based_on_confirmed_state_id=state.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
    )

    body = build_rich_provider_request_body(request)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] == request.requested_output_schema
    assert request.requested_output_schema["additionalProperties"] is False

    move = next(action for action in request.legal_actions if action.action_type is ActionType.MOVE)
    response = {
        "recommended_action": {
            "action_id": move.action_id,
            "action_type": move.action_type.value,
            "action_name": move.action_name,
        },
        "reasons": ["現在の盤面で最も安定した価値があります。"],
        "warnings": ["相手の持ち物は未確認です。"],
        "opponent_prediction": {
            "category": "UNKNOWN",
            "predicted_action": None,
            "summary": "情報が不足しているため、相手の行動は断定できません。",
            "confidence": 0.25,
        },
    }
    parsed = turn_advice_body_from_dict(response)
    assert parsed.reasons == ("現在の盤面で最も安定した価値があります。",)
    assert parsed.opponent_prediction.category == "UNKNOWN"

    invalid = {**response, "unexpected": "日本語でも追加fieldは禁止"}
    with pytest.raises(TurnAdviceSchemaError, match="top_level_unknown_fields"):
        turn_advice_body_from_dict(invalid)


# --- 3. Forge resistance: request_rich_turn_advice ---------------------------


def test_request_rich_turn_advice_creates_job_from_durable_state_only(
    rich_fixture: RichSessionFixture,
) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    assert job.job_type is JobType.TURN_ADVICE
    assert job.session_id == rich_fixture.session_id
    assert job.input_snapshot_id == rich_fixture.confirmed_state_id
    assert len(job.request_payload_hash) == 64


def test_request_rich_turn_advice_denies_pending_job(rich_fixture: RichSessionFixture) -> None:
    rich_fixture.application.request_rich_turn_advice("command-1")
    with pytest.raises(DomainError):
        rich_fixture.application.request_rich_turn_advice("command-2")


def test_request_rich_turn_advice_denies_second_attempt_after_job_terminal(
    rich_fixture: RichSessionFixture,
) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    from maple_next.domain.enums import JobStatus

    rich_fixture.repository.update_job_status(job.job_id, JobStatus.FAILED)
    rich_fixture.repository.connection.commit()
    with pytest.raises(DomainError, match="DENY_ATTEMPT_ALREADY_CONSUMED"):
        rich_fixture.application.request_rich_turn_advice("command-2")


def test_request_rich_turn_advice_rejects_stale_confirmed_state(
    rich_fixture: RichSessionFixture,
) -> None:
    """A superseded confirmed state (wrong turn binding) must be rejected."""

    stale_repository = rich_fixture.repository
    # Overwrite current_turn_id to point at a turn with no confirmed state.
    session = stale_repository.load_active_session()
    stale_repository.append_turn(session.session_id, BattleTurn(turn_id="turn-2", turn_number=2))
    session.current_turn_id = "turn-2"
    stale_repository.save_session(session)
    stale_repository.connection.commit()
    with pytest.raises(DomainError, match="CONFIRMED_STATE_NOT_CURRENT_BINDING"):
        rich_fixture.application.request_rich_turn_advice("command-1")


def test_request_rich_turn_advice_rejects_open_draft_with_wrong_based_on_state(
    rich_fixture: RichSessionFixture,
) -> None:
    """An OPEN draft whose source delta points at the wrong based-on state must fail closed."""

    wrong_based_on = ConfirmedTurnState(
        confirmed_state_id="state-wrong-base",
        identity=rich_fixture.identity(battle_revision=0),
        previous_confirmed_state_id=None,
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    rich_fixture.repository.append_confirmed_turn_state(wrong_based_on)
    rich_fixture.repository.connection.commit()

    next_identity = rich_fixture.identity(turn_number=2, battle_revision=4, turn_id="turn-2")
    delta = ActionResultDelta(
        delta_id="delta-x",
        identity=next_identity,
        based_on_confirmed_state_id="state-wrong-base",
        self_side=SideDelta(
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
        ),
        opponent_side=SideDelta(
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
        ),
        weather=FieldDelta.unchanged(),
        terrain=FieldDelta.unchanged(),
        confirmation=_confirmation(),
    )
    rich_fixture.repository.append_action_result_delta(delta)
    rich_fixture.repository.connection.commit()
    corrupt_draft = NextTurnStateDraft(
        draft_id="draft-corrupt",
        identity=next_identity,
        based_on_confirmed_state_id=rich_fixture.confirmed_state_id,
        source_delta_id="delta-x",
        self_side=_confirmed_side("Foo"),
        opponent_side=_confirmed_side("Bar"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        derived_at_utc=CONFIRMED_AT,
    )
    rich_fixture.repository.upsert_next_turn_state_draft(corrupt_draft)
    rich_fixture.repository.connection.commit()
    with pytest.raises(DomainError, match="OPEN_DRAFT_CHAIN_INVALID"):
        rich_fixture.application.request_rich_turn_advice("command-1")


def test_request_rich_turn_advice_accepts_boundary_rejects_blank_action(
    tmp_path,
) -> None:
    fixture = RichSessionFixture(tmp_path)
    fixture.append_confirmed_state()
    blank = ConfirmedLegalActionSelection(
        confirmation_id="legal-blank",
        identity=fixture.identity(),
        action_type=ActionType.MOVE,
        action_name="Wave Crash",
        confirmation=_confirmation(),
    )
    fixture.repository.append_confirmed_legal_action_selection(blank)
    fixture.repository.connection.commit()
    # Corrupt the persisted row to be blank -- simulates a corrupted store,
    # since the domain object itself would refuse to construct blank.
    fixture.repository.connection.execute(
        "UPDATE confirmed_legal_action_selections SET action_name = '' WHERE confirmation_id = ?",
        ("legal-blank",),
    )
    fixture.repository.connection.commit()
    with pytest.raises((DomainError, ValueError)):
        fixture.application.request_rich_turn_advice("command-1")


# --- 4. Offline rebuild boundary ---------------------------------------------


def test_build_rich_turn_advice_transport_request_matches_stored_hash(
    rich_fixture: RichSessionFixture,
) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    rebuilt = rich_fixture.application.build_rich_turn_advice_transport_request(job)
    assert rebuilt.request_hash == job.request_payload_hash


def test_rebuild_does_not_reserve_another_attempt_or_job(
    rich_fixture: RichSessionFixture,
) -> None:
    job = rich_fixture.application.request_rich_turn_advice("command-1")
    before = rich_fixture.repository.turn_advice_attempt_reserved(
        session_id=rich_fixture.session_id,
        match_id=rich_fixture.match_id,
        generation=rich_fixture.generation,
        turn_number=rich_fixture.turn_number,
        reviewed_snapshot_id=rich_fixture.confirmed_state_id,
    )
    rich_fixture.application.build_rich_turn_advice_transport_request(job)
    after = rich_fixture.repository.turn_advice_attempt_reserved(
        session_id=rich_fixture.session_id,
        match_id=rich_fixture.match_id,
        generation=rich_fixture.generation,
        turn_number=rich_fixture.turn_number,
        reviewed_snapshot_id=rich_fixture.confirmed_state_id,
    )
    assert before is True and after is True


def test_rebuild_rejects_job_of_wrong_type(rich_fixture: RichSessionFixture) -> None:
    from datetime import UTC, datetime

    from maple_next.workers.contracts.models import JobStatus as WorkerJobStatus

    fake_job = JobEnvelope(
        contract_version="maple-worker.v1",
        job_id="job-fake",
        command_id="command-fake",
        job_type=JobType.SELECTION_ADVICE,
        session_id=rich_fixture.session_id,
        match_id=rich_fixture.match_id,
        generation=rich_fixture.generation,
        turn_number=rich_fixture.turn_number,
        base_battle_revision=rich_fixture.battle_revision,
        expected_state=BattleState.TURN_REVIEWED,
        input_snapshot_id=rich_fixture.confirmed_state_id,
        request_payload_hash="0" * 64,
        human_authorized_at=datetime.now(UTC),
        status=WorkerJobStatus.QUEUED,
    )
    with pytest.raises(DomainError, match="JOB_TYPE_NOT_TURN_ADVICE"):
        rich_fixture.application.build_rich_turn_advice_transport_request(fake_job)


def test_rebuild_rejects_legacy_only_match(tmp_path) -> None:
    """A legacy (non-rich) match's TURN_ADVICE job must not rebuild as rich."""

    from datetime import UTC, datetime

    from maple_next.domain.models import BattleSession
    from maple_next.workers.contracts.models import JobStatus as WorkerJobStatus

    repository = SQLiteRepository(tmp_path / "runtime" / "maple.db")
    application = MatchApplication(repository, tmp_path / "user-data" / "exports")
    session = BattleSession(
        session_id="legacy-session",
        match_id="legacy-match",
        generation=1,
        state=BattleState.TURN_REVIEWED,
        battle_revision=1,
    )
    repository.insert_session(session)
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
        input_snapshot_id="turn-facts-1",
        request_payload_hash="0" * 64,
        human_authorized_at=datetime.now(UTC),
        status=WorkerJobStatus.QUEUED,
    )
    with pytest.raises(DomainError, match="JOB_NOT_RICH_STATE_CONTRACT"):
        application.build_rich_turn_advice_transport_request(job)


# --- 5. Hash coverage per field -----------------------------------------------


def _build_request_for_state(
    fixture: RichSessionFixture, state: ConfirmedTurnState, actions
) -> str:
    request = build_rich_state_turn_advice_request(
        confirmed_state=state,
        confirmed_legal_actions=actions,
        current_identity=fixture.identity(),
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=fixture.legal_switch_confirmation(
            based_on_confirmed_state_id=state.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
    )
    return request.request_hash


def test_hash_changes_with_previous_confirmed_state_id(tmp_path) -> None:
    fixture_a = RichSessionFixture(tmp_path / "a")
    dummy_a = ConfirmedTurnState(
        confirmed_state_id="state-0-a",
        identity=fixture_a.identity(battle_revision=0),
        previous_confirmed_state_id=None,
        self_side=_confirmed_side("Dondozo"),
        opponent_side=_confirmed_side("Garchomp"),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        confirmation=_confirmation(),
    )
    fixture_a.repository.append_confirmed_turn_state(dummy_a)
    fixture_a.repository.connection.commit()
    state_a = fixture_a.append_confirmed_state(previous_confirmed_state_id="state-0-a")
    actions_a = fixture_a.append_legal_actions()
    hash_a = _build_request_for_state(fixture_a, state_a, actions_a)

    fixture_b = RichSessionFixture(tmp_path / "b")
    state_b = fixture_b.append_confirmed_state(previous_confirmed_state_id=None)
    actions_b = fixture_b.append_legal_actions()
    hash_b = _build_request_for_state(fixture_b, state_b, actions_b)

    assert hash_a != hash_b


def test_hash_changes_with_evidence(tmp_path) -> None:
    fixture_a = RichSessionFixture(tmp_path / "a")
    fixture_a.repository.append_fixed_evidence_metadata(
        FixedEvidenceMetadata(
            evidence_id="ev-1",
            relative_path="evidence/ev-1.png",
            sha256="a" * 64,
            recorded_at_utc=CONFIRMED_AT,
        )
    )
    fixture_a.repository.connection.commit()
    state_a = fixture_a.append_confirmed_state(evidence_id="ev-1")
    actions_a = fixture_a.append_legal_actions()
    evidence_a = fixture_a.repository.get_fixed_evidence_metadata("ev-1")
    request_a = build_rich_state_turn_advice_request(
        confirmed_state=state_a,
        confirmed_legal_actions=actions_a,
        current_identity=fixture_a.identity(),
        latest_confirmed_state_id=state_a.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=fixture_a.legal_switch_confirmation(
            based_on_confirmed_state_id=state_a.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
        evidence=evidence_a,
    )

    fixture_b = RichSessionFixture(tmp_path / "b")
    state_b = fixture_b.append_confirmed_state(evidence_id=None)
    actions_b = fixture_b.append_legal_actions()
    request_b = build_rich_state_turn_advice_request(
        confirmed_state=state_b,
        confirmed_legal_actions=actions_b,
        current_identity=fixture_b.identity(),
        latest_confirmed_state_id=state_b.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=fixture_b.legal_switch_confirmation(
            based_on_confirmed_state_id=state_b.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
        evidence=None,
    )
    assert request_a.request_hash != request_b.request_hash


def test_hash_changes_with_selected_three(tmp_path) -> None:
    fixture = RichSessionFixture(tmp_path)
    state = fixture.append_confirmed_state()
    actions = fixture.append_legal_actions()
    request_a = build_rich_state_turn_advice_request(
        confirmed_state=state,
        confirmed_legal_actions=actions,
        current_identity=fixture.identity(),
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=fixture.legal_switch_confirmation(
            based_on_confirmed_state_id=state.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
    )
    request_b = build_rich_state_turn_advice_request(
        confirmed_state=state,
        confirmed_legal_actions=actions,
        current_identity=fixture.identity(),
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=fixture.legal_switch_confirmation(
            based_on_confirmed_state_id=state.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Hatterene"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Hatterene")
        ),
        rules_context=default_rules_context(),
    )
    assert request_a.request_hash != request_b.request_hash


def test_hash_changes_with_self_team_build_sha256(tmp_path) -> None:
    fixture = RichSessionFixture(tmp_path)
    state = fixture.append_confirmed_state()
    actions = fixture.append_legal_actions()
    request_a = build_rich_state_turn_advice_request(
        confirmed_state=state,
        confirmed_legal_actions=actions,
        current_identity=fixture.identity(),
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=fixture.legal_switch_confirmation(
            based_on_confirmed_state_id=state.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
        self_team_build_sha256="b" * 64,
    )
    request_b = build_rich_state_turn_advice_request(
        confirmed_state=state,
        confirmed_legal_actions=actions,
        current_identity=fixture.identity(),
        latest_confirmed_state_id=state.confirmed_state_id,
        latest_open_draft_turn_number=None,
        latest_open_draft_battle_revision=None,
        legal_switch_confirmation=fixture.legal_switch_confirmation(
            based_on_confirmed_state_id=state.confirmed_state_id
        ),
        selected_three=("Dondozo", "Gholdengo", "Urshifu"),
        self_active="Dondozo",
        bundle3_context=names_only_bundle3_context(
            selected_three=("Dondozo", "Gholdengo", "Urshifu")
        ),
        rules_context=default_rules_context(),
        self_team_build_sha256=None,
    )
    assert request_a.request_hash != request_b.request_hash


# --- 6. Repository-backed export selection -----------------------------------


def _end_and_export(fixture: RichSessionFixture):
    from maple_next.domain.enums import ActionOrder
    from maple_next.domain.models import TurnFactsSnapshot

    fixture.repository.append_turn_facts(
        fixture.session_id,
        TurnFactsSnapshot(
            turn_facts_id="facts-1",
            turn_id=fixture.turn_id,
            turn_number=fixture.turn_number,
            self_active="Dondozo",
            opponent_active="Garchomp",
            self_hp=HpBucket.FULL,
            opponent_hp=HpBucket.FULL,
            legal_moves=("Wave Crash",),
            legal_switches=("Gholdengo",),
        ),
    )
    fixture.repository.append_recorded_action(
        fixture.session_id,
        RecordedAction(
            action_id="action-1",
            turn_id=fixture.turn_id,
            turn_number=fixture.turn_number,
            action_type=ActionType.MOVE,
            action_name="Wave Crash",
            opponent_action_type=ActionType.MOVE,
            opponent_action_name="Earthquake",
            action_order=ActionOrder.SELF_FIRST,
        ),
    )
    fixture.repository.connection.commit()
    session = fixture.repository.load_active_session()
    session.state = BattleState.TURN_RECORDED
    fixture.repository.save_session(session)
    fixture.repository.connection.commit()
    fixture.application.end_match(MatchOutcome.WIN, human_confirmed=True)
    return fixture.application.export_match()


def test_export_selects_v3_for_rich_state_match(rich_fixture: RichSessionFixture) -> None:
    record = _end_and_export(rich_fixture)
    assert record.schema_version == "maple-match.v3"
    with open(record.export_path, encoding="utf-8") as handle:
        payload = json.loads(handle.read())
    parse_match_export_v3(json.dumps(payload).encode("utf-8"))
    assert payload["selection"]["selected_three"] == ["Dondozo", "Gholdengo", "Urshifu"]
    rich_turn = next(t for t in payload["turns"] if t["turn_number"] == 1)
    assert "rich_state" in rich_turn


# --- 7. Strict v3 parser rejections ------------------------------------------


_MINIMAL_SELECTION = {"self_team": [], "opponent_team": [], "selected_three": [], "lead": ""}


def _known_json(status: str = "CONFIRMED", value: object = "NONE") -> dict:
    if status == "UNKNOWN":
        return {"status": "UNKNOWN", "provenance_chain": ["UNKNOWN"]}
    return {"status": "CONFIRMED", "value": value, "provenance_chain": ["HUMAN_INPUT"]}


def _field_delta_json(observation: str = "UNCHANGED", after_value: object = None) -> dict:
    if observation == "CHANGED":
        return {
            "observation": "CHANGED",
            "after_value": after_value,
            "provenance_chain": ["HUMAN_INPUT"],
        }
    return {"observation": observation, "provenance_chain": ["HUMAN_INPUT"]}


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


def _valid_side_state_json() -> dict:
    values = {
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
    return {name: _known_json(value=values[name]) for name in _SIDE_STATE_FIELD_NAMES}


def _valid_side_delta_json() -> dict:
    return {name: _field_delta_json() for name in _SIDE_STATE_FIELD_NAMES}


def _valid_identity_json(**overrides) -> dict:
    identity = {
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "turn_id": "t1",
        "turn_number": 1,
        "battle_revision": 1,
    }
    identity.update(overrides)
    return identity


def _valid_confirmation_json() -> dict:
    return {
        "confirmed_by_human": True,
        "confirmed_at_utc": CONFIRMED_AT,
        "provenance": "HUMAN_CONFIRMED",
    }


def _valid_rich_state_block() -> dict:
    from maple_next.application.match_export_v3 import RICH_STATE_EXPORT_CONTRACT_VERSION

    return {
        "contract_version": RICH_STATE_EXPORT_CONTRACT_VERSION,
        "confirmed_turn_state": {
            "confirmed_state_id": "s1",
            "previous_confirmed_state_id": None,
            "identity": _valid_identity_json(),
            "self_side": _valid_side_state_json(),
            "opponent_side": _valid_side_state_json(),
            "weather": _known_json(),
            "terrain": _known_json(),
            "confirmation": _valid_confirmation_json(),
            "evidence_id": None,
        },
        "source_action_result_delta": None,
        "confirmed_legal_actions": [],
        "evidence": None,
    }


def test_parser_rejects_forbidden_provider_key() -> None:
    payload = {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 1,
        "selection": _MINIMAL_SELECTION,
        "action_history": [],
        "turns": [{"turn_number": 1, "api_key": "secret"}],
    }
    with pytest.raises(MatchExportV3Error, match="FORBIDDEN_KEY"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))


def test_parser_rejects_unknown_carrying_value() -> None:
    rich_state = _valid_rich_state_block()
    rich_state["confirmed_turn_state"]["weather"] = {"status": "UNKNOWN", "value": "NONE"}
    payload = {
        "schema_version": "maple-match.v3",
        "session_id": "s",
        "match_id": "m",
        "generation": 1,
        "outcome": "WIN",
        "ended_at_utc": CONFIRMED_AT,
        "final_battle_revision": 1,
        "selection": _MINIMAL_SELECTION,
        "action_history": [],
        "turns": [{"turn_number": 1, "rich_state": rich_state}],
    }
    with pytest.raises(MatchExportV3Error, match="UNKNOWN_CARRIES_VALUE"):
        parse_match_export_v3(json.dumps(payload).encode("utf-8"))
