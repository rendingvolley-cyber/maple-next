"""Gemini V2 Bundle 2 R1: real Battle Record UI/controller legal-switch lifecycle.

Exercises the actual production path -- ``BattleRecordUiWindow`` /
``TurnStateFlowController`` -- rather than domain-only calls, per the R1
requirement that the UI lifecycle itself (candidate display, unresolved vs.
confirmed-none visual state, TurnIdentity/revision invalidation, restart
hydration) be proven through the real handlers. Reuses the existing
``test_issue31_turn_state_ui_bundle_c`` fixtures/team constants -- no new
team/session setup is invented here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_issue31_action_result_lifecycle_r2 import (
    _submit_turn_advice as _lifecycle_submit_turn_advice,
)
from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    SELECTED_THREE,
    SyncDispatch,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_production_compatible_window,
    build_window,
    qt_application,
)

from maple_next.application.match_service import MatchApplication
from maple_next.domain.enums import HpBucket
from maple_next.domain.legal_switches import LegalSwitchStatus
from maple_next.domain.turn_state import (
    ConfirmationMeta,
    Known,
    PokemonLocalMemory,
    ProvenanceStep,
    SideState,
    TurnIdentity,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.turn_transport import load_authorized_turn_provider_config_from_env
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController

_CONFIRMED_AT = "2026-08-15T00:00:00+00:00"
_HUMAN = (ProvenanceStep.HUMAN_INPUT,)


def _r3_confirmation() -> ConfirmationMeta:
    return ConfirmationMeta(
        confirmed_by_human=True, confirmed_at_utc=_CONFIRMED_AT, provenance="HUMAN_INPUT"
    )


def _full_side_state(*, active: Known[str]) -> SideState:
    """A fully-specified SideState, with only ``active`` varying by caller.

    Lets a test drive :meth:`TurnStateFlowController.confirm_turn_facts`
    directly with a genuinely-unknown active Pokemon (impossible via the
    real widgets, which always couple the legacy and rich ``active`` fields
    to the same combo box) while every other field stays validly confirmed.
    """

    return SideState(
        active=active,
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


def test_1_confirm_facts_auto_resolves_legal_switches_when_deterministic(
    tmp_path: Path,
) -> None:
    """R3R1: CONFIRM TURN FACTS persists exactly the visible workbench
    selection (here: every prefilled candidate, since the operator never
    touched it) as CONFIRMED_NONEMPTY, the same human confirmation action,
    no second click required. The prefill itself (selected_three - active -
    confirmed_fainted) is only ever an editable candidate list, never a
    silent auto-confirmation on its own -- see test_1f/1g/1h below for the
    operator editing it before confirming."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)

    window._on_confirm_turn_facts()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.confirmed_state is not None
    assert summary.legal_switch_candidates == ("Gholdengo", "Dragonite")
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    assert summary.legal_switch_confirmation.legal_switches == ("Gholdengo", "Dragonite")
    assert "LEGAL_SWITCHES_UNRESOLVED" not in summary.provider_ready_denial_reasons
    assert summary.provider_ready is True
    assert (
        window.legal_switch_status_label.text()
        == "確定 (CONFIRMED_NONEMPTY): Gholdengo、Dragonite"
    )
    assert {
        window.legal_switch_list.item(i).text() for i in range(window.legal_switch_list.count())
    } == {"Gholdengo", "Dragonite"}
    assert all(
        window.legal_switch_list.item(i).isSelected()
        for i in range(window.legal_switch_list.count())
    )
    # Confirming facts never itself dispatches -- only an explicit send does.
    assert transport.call_count == 0
    repository.close()


def test_1b_confirm_facts_with_active_unknown_stays_unresolved(tmp_path: Path) -> None:
    """R3: genuinely incomplete facts (active Pokemon not yet confirmed) must
    never be guessed into a legal-switch confirmation -- the binding stays
    NOT_CAPTURED_OR_UNRESOLVED, exactly as before this feature existed."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    view = controller.confirm_turn_facts(
        self_active=SELECTED_THREE[0],
        opponent_active=OPPONENT_TEAM[0],
        self_hp="100",
        opponent_hp="100",
        legal_moves=["Flower Trick", "Knock Off"],
        legal_switches=[],
        human_note="",
        human_confirmed=True,
        self_side=_full_side_state(active=Known.unknown()),
        opponent_side=_full_side_state(
            active=Known.confirmed(OPPONENT_TEAM[0], provenance_chain=_HUMAN)
        ),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
    )
    assert view.error_message is None

    summary = controller.turn_state_summary()
    assert summary.confirmed_state is not None
    assert summary.legal_switch_confirmation is None
    assert summary.legal_switch_candidates == ()
    assert "LEGAL_SWITCHES_UNRESOLVED" in summary.provider_ready_denial_reasons
    window.render_view()
    assert window.legal_switch_status_label.text() == "未確認 (UNRESOLVED)"
    assert transport.call_count == 0
    repository.close()


def test_1c_confirm_facts_excludes_confirmed_fainted_backline_member(
    tmp_path: Path,
) -> None:
    """Faint exclusion: a selected member with match-local confirmed HP=0
    must never appear in the auto-confirmed legal-switch set, even though it
    is neither the current active Pokemon nor otherwise disqualified."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    session = repository.load_active_session()
    assert session is not None
    with repository.transaction():
        repository.upsert_pokemon_local_state(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            side="SELF",
            memory=PokemonLocalMemory(
                pokemon_name="Dragonite",
                hp_bucket=Known.confirmed(HpBucket.ZERO, provenance_chain=_HUMAN),
                status=Known.confirmed("NONE", provenance_chain=_HUMAN),
            ),
        )
    _fill_minimal_current_state(window)

    window._on_confirm_turn_facts()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_candidates == ("Gholdengo",)
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    assert summary.legal_switch_confirmation.legal_switches == ("Gholdengo",)
    assert transport.call_count == 0
    repository.close()


def test_1d_confirm_facts_all_backline_fainted_is_confirmed_none(tmp_path: Path) -> None:
    """Every non-active selected member confirmed-fainted -> CONFIRMED_NONE,
    the explicit "definitely zero legal switches" status, never merely an
    empty tuple with no status at all."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    session = repository.load_active_session()
    assert session is not None
    with repository.transaction():
        for name in ("Gholdengo", "Dragonite"):
            repository.upsert_pokemon_local_state(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                side="SELF",
                memory=PokemonLocalMemory(
                    pokemon_name=name,
                    hp_bucket=Known.confirmed(HpBucket.ZERO, provenance_chain=_HUMAN),
                    status=Known.confirmed("NONE", provenance_chain=_HUMAN),
                ),
            )
    _fill_minimal_current_state(window)

    window._on_confirm_turn_facts()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_candidates == ()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert summary.legal_switch_confirmation.legal_switches == ()
    assert "LEGAL_SWITCHES_UNRESOLVED" not in summary.provider_ready_denial_reasons
    assert window.legal_switch_status_label.text() == "確定: 交代先なし (CONFIRMED_NONE)"
    assert transport.call_count == 0
    repository.close()


def test_1e_provider_authorization_off_still_fails_closed_after_auto_resolve(
    tmp_path: Path,
) -> None:
    """R3: auto-resolving legal switches must never weaken the independent,
    downstream MAPLE_NEXT_GEMINI_TURN_AUTHORIZED gate -- reaching
    provider_ready faster/automatically changes nothing about that."""

    qt_application()
    repository = SQLiteRepository(tmp_path / "auth-gate.db")
    export_dir = tmp_path / "auth-gate-export"
    export_dir.mkdir()
    application = MatchApplication(repository, export_dir)

    class _NeverCalledTransport:
        def __init__(self) -> None:
            self.call_count = 0

        def send(self, request: object, config: object) -> None:
            self.call_count += 1
            raise AssertionError("transport must not be called when unauthorized")

    transport = _NeverCalledTransport()
    rich_adapter = GeminiRichTurnAdviceAdapter(
        transport,  # type: ignore[arg-type]
        load_authorized_turn_provider_config_from_env,
        dispatch_factory=SyncDispatch,
    )
    controller = TurnStateFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        None,
        None,
        rich_adapter,
    )
    ocr_dir = tmp_path / "auth-gate-ocr"
    ocr_dir.mkdir()
    window = BattleRecordUiWindow(
        controller, ocr_data_directory=ocr_dir, auto_start_capture=False
    )
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.provider_ready is True

    view = controller.send_rich_turn_advice_to_gemini(
        action_type="MOVE",
        action_name="Flower Trick",
        opponent_prediction="",
        rationale="",
        warnings=(),
        on_result=lambda _view: None,
    )

    assert transport.call_count == 0
    assert view.error_message is not None
    repository.close()


def test_1f_candidates_are_visible_and_unconfirmed_before_the_human_click(
    tmp_path: Path,
) -> None:
    """R3R1 mandatory case 1: unknown bench availability still populates the
    operator-facing candidate UI, but persistence remains
    NOT_CAPTURED_OR_UNRESOLVED until the human actually presses CONFIRM
    TURN FACTS. A displayed prefill is never itself sufficient
    confirmation."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    # Setting active alone (no HP/moves/notes yet) is enough to trigger the
    # prefill refresh -- CONFIRM TURN FACTS has not been pressed.
    window.self_active_box.setCurrentText(SELECTED_THREE[0])

    assert {
        window.legal_switch_list.item(i).text() for i in range(window.legal_switch_list.count())
    } == {"Gholdengo", "Dragonite"}
    assert all(
        window.legal_switch_list.item(i).isSelected()
        for i in range(window.legal_switch_list.count())
    )
    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is None
    assert "未確認" in window.legal_switch_status_label.text()
    assert transport.call_count == 0
    repository.close()


def test_1g_operator_unchecks_one_candidate_before_confirm(tmp_path: Path) -> None:
    """R3R1 mandatory case 3: selected A/B/C, active A, candidates B/C
    prefilled -> operator unchecks B -> CONFIRM TURN FACTS ->
    CONFIRMED_NONEMPTY [C] only. The removed candidate must never reappear
    in the persisted set."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    assert {
        window.legal_switch_list.item(i).text() for i in range(window.legal_switch_list.count())
    } == {"Gholdengo", "Dragonite"}

    for index in range(window.legal_switch_list.count()):
        item = window.legal_switch_list.item(index)
        if item.text() == "Gholdengo":
            item.setSelected(False)

    window._on_confirm_turn_facts()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    assert summary.legal_switch_confirmation.legal_switches == ("Dragonite",)
    assert transport.call_count == 0
    repository.close()


def test_1h_operator_unchecks_all_candidates_before_confirm(tmp_path: Path) -> None:
    """R3R1 mandatory case 4: operator unchecks every prefilled candidate ->
    CONFIRM TURN FACTS -> CONFIRMED_NONE [] -- an explicit, human-reviewed
    zero, not a guess."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)

    for index in range(window.legal_switch_list.count()):
        window.legal_switch_list.item(index).setSelected(False)

    window._on_confirm_turn_facts()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert summary.legal_switch_confirmation.legal_switches == ()
    assert "LEGAL_SWITCHES_UNRESOLVED" not in summary.provider_ready_denial_reasons
    assert transport.call_count == 0
    repository.close()


def test_1i_confirmed_fainted_member_cannot_be_confirmed_legal(tmp_path: Path) -> None:
    """R3R1 mandatory case 5: even if a confirmed-fainted member somehow
    reaches the confirm call (e.g. a stale/forced selection bypassing the
    UI's own prefill filtering), the write path fails closed rather than
    persisting it -- defense in depth, not merely a UI-level filter."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    session = repository.load_active_session()
    assert session is not None
    with repository.transaction():
        repository.upsert_pokemon_local_state(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            side="SELF",
            memory=PokemonLocalMemory(
                pokemon_name="Dragonite",
                hp_bucket=Known.confirmed(HpBucket.ZERO, provenance_chain=_HUMAN),
                status=Known.confirmed("NONE", provenance_chain=_HUMAN),
            ),
        )

    view = controller.confirm_turn_facts(
        self_active=SELECTED_THREE[0],
        opponent_active=OPPONENT_TEAM[0],
        self_hp="100",
        opponent_hp="100",
        legal_moves=["Flower Trick", "Knock Off"],
        legal_switches=[],
        human_note="",
        human_confirmed=True,
        self_side=_full_side_state(
            active=Known.confirmed(SELECTED_THREE[0], provenance_chain=_HUMAN)
        ),
        opponent_side=_full_side_state(
            active=Known.confirmed(OPPONENT_TEAM[0], provenance_chain=_HUMAN)
        ),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        # Forced/stale: includes the confirmed-fainted "Dragonite" as if the
        # operator had (incorrectly) left it checked.
        legal_switch_selection=("Gholdengo", "Dragonite"),
    )
    assert view.error_message is None  # Turn facts themselves still confirm.

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is None
    assert "LEGAL_SWITCHES_UNRESOLVED" in summary.provider_ready_denial_reasons
    assert transport.call_count == 0
    repository.close()


def test_1j_selection_naming_a_non_selected_pokemon_is_rejected(tmp_path: Path) -> None:
    """R3R1 mandatory case 6 (stale/foreign reference): a legal-switch name
    outside the applied selected_three (e.g. a stale name left over from a
    different binding) must never be persisted -- the write path always
    re-validates against the CURRENT applied selection, not whatever the
    caller happened to pass."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    view = controller.confirm_turn_facts(
        self_active=SELECTED_THREE[0],
        opponent_active=OPPONENT_TEAM[0],
        self_hp="100",
        opponent_hp="100",
        legal_moves=["Flower Trick", "Knock Off"],
        legal_switches=[],
        human_note="",
        human_confirmed=True,
        self_side=_full_side_state(
            active=Known.confirmed(SELECTED_THREE[0], provenance_chain=_HUMAN)
        ),
        opponent_side=_full_side_state(
            active=Known.confirmed(OPPONENT_TEAM[0], provenance_chain=_HUMAN)
        ),
        weather=Known.confirmed("NONE", provenance_chain=_HUMAN),
        terrain=Known.confirmed("NONE", provenance_chain=_HUMAN),
        legal_switch_selection=("Not-In-Selected-Three",),
    )
    # With one canonical explicit switch set, the legacy snapshot also
    # validates the selected name; a foreign workbench selection therefore
    # fails closed before either representation is persisted.
    assert view.error_message is not None

    summary = controller.turn_state_summary()
    assert summary.confirmed_state is None
    assert summary.legal_switch_confirmation is None
    assert summary.provider_ready_denial_reasons == ()
    assert transport.call_count == 0
    repository.close()


def test_2_confirm_two_candidates_selected_is_confirmed_nonempty(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    # Direct controller call (never the UI click cascade) proves confirming
    # switches is itself only a factual persistence operation.
    view = controller.confirm_legal_switches(
        legal_switches=("Gholdengo", "Dragonite"), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    assert view.error_message is None
    assert transport.call_count == 0

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    assert summary.legal_switch_confirmation.legal_switches == ("Gholdengo", "Dragonite")
    assert summary.provider_ready is True
    repository.close()


def test_2b_confirm_none_via_direct_controller_call_makes_no_dispatch(tmp_path: Path) -> None:
    """R2-E: the controller/application command for CONFIRMED_NONE, called
    directly (never through a UI click), makes zero dispatch attempts and
    zero transport calls -- symmetric with test_2's CONFIRMED_NONEMPTY case.
    """

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    view = controller.confirm_legal_switches(
        legal_switches=(), status=LegalSwitchStatus.CONFIRMED_NONE
    )
    assert view.error_message is None
    assert transport.call_count == 0

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert summary.legal_switch_confirmation.legal_switches == ()
    repository.close()


def test_3_explicit_confirm_none_is_visually_distinct_from_auto_confirmed(
    tmp_path: Path,
) -> None:
    """An explicit operator override (CONFIRMED_NONE) must render visibly
    differently from whatever CONFIRM TURN FACTS auto-resolved -- an
    operator correction is never silently indistinguishable from the
    deterministic default it replaces."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    auto_confirmed_label = window.legal_switch_status_label.text()
    assert "CONFIRMED_NONEMPTY" in auto_confirmed_label

    window._on_confirm_legal_switches_none()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert summary.legal_switch_confirmation.legal_switches == ()
    confirmed_none_label = window.legal_switch_status_label.text()
    assert confirmed_none_label != auto_confirmed_label
    assert "CONFIRMED_NONE" in confirmed_none_label
    assert transport.call_count == 0  # not yet provider-ready overall (no legal move confirmed)
    repository.close()


def test_3b_confirm_none_reaches_provider_ready_but_never_sends(tmp_path: Path) -> None:
    """R2-D: even when CONFIRMED_NONE is the last missing provider-ready
    prerequisite (every other requirement already satisfied, matching the
    v5 production-compatible fixture), confirming it must not dispatch --
    only the separate explicit send action may reach the transport."""

    repository, controller, window, transport, adapter = (
        build_production_compatible_window(tmp_path)
    )
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    session = repository.load_active_session()
    assert session is not None
    # CONFIRMED_NONE is provider-ready only when the selected backline is
    # actually confirmed fainted. Keep this positive-path fixture distinct
    # from test_3c, which intentionally exercises the contradictory case.
    with repository.transaction():
        for name in (SELECTED_THREE[1], SELECTED_THREE[2]):
            repository.upsert_pokemon_local_state(
                session_id=session.session_id,
                match_id=session.match_id,
                generation=session.generation,
                side="SELF",
                memory=PokemonLocalMemory(
                    pokemon_name=name,
                    hp_bucket=Known.confirmed(HpBucket.ZERO, provenance_chain=_HUMAN),
                    status=Known.confirmed("NONE", provenance_chain=_HUMAN),
                ),
            )
    _fill_minimal_current_state(window)
    window.confirm_turn_facts_button.click()
    assert transport.call_count == 0

    window._on_confirm_legal_switches_none()  # noqa: SLF001

    assert transport.call_count == 0
    assert adapter.dispatch_count == 0
    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert summary.provider_ready is True
    assert window._bundle_c_gemini_send_button.isEnabled()  # noqa: SLF001

    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001
    assert transport.call_count == 1
    assert adapter.dispatch_count == 1
    repository.close()


def test_3c_confirmed_none_contradicting_real_backline_blocks_ready(tmp_path: Path) -> None:
    """Tournament-week hotfix: an explicit CONFIRMED_NONE override while
    Gholdengo/Dragonite are still valid (non-fainted, non-active) backline
    candidates must never reach provider-ready -- the gate treats it as an
    unresolved contradiction, exactly like a never-confirmed binding, rather
    than letting an empty legal-switch set be sent to Gemini. Uses the plain
    (non-auto-sending) fixture so the contradiction itself -- not any
    unrelated same-click send behavior -- is what's under test."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    before = controller.turn_state_summary()
    assert before.legal_switch_confirmation is not None
    assert before.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY

    window._on_confirm_legal_switches_none()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONE
    assert summary.provider_ready is False
    assert "LEGAL_SWITCHES_CONFIRMED_NONE_CONTRADICTS_DERIVATION" in (
        summary.provider_ready_denial_reasons
    )
    assert transport.call_count == 0
    repository.close()


def test_4_new_turn_identity_invalidates_previous_ui_confirmation(tmp_path: Path) -> None:
    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    controller.confirm_legal_switches(
        legal_switches=("Gholdengo",), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    window.render_view()
    assert "CONFIRMED_NONEMPTY" in window.legal_switch_status_label.text()
    turn1_identity = controller.turn_state_summary().identity
    assert turn1_identity is not None

    # Advance to the next Turn -- a fresh TurnIdentity/binding -- via the
    # same real record-action/next-turn path Bundle 1's own lifecycle tests
    # use (satisfying the legacy MOCK Turn Advice + rich completion
    # prerequisites first).
    _lifecycle_submit_turn_advice(window)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.opponent_action_type_box.setCurrentText("選択してください")
    window._on_record_action()  # noqa: SLF001
    window._on_next_turn()  # noqa: SLF001
    window.render_view()

    turn2_identity = controller.turn_state_summary().identity
    assert turn2_identity is not None
    assert turn2_identity != turn1_identity

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is None
    # Turn 2 is a fresh, not-yet-confirmed binding back in
    # TURN_CAPTURE_PENDING -- the label reflects the pre-confirm-editable
    # prefill state, not the plain post-confirmation UNRESOLVED text.
    assert "未確認" in window.legal_switch_status_label.text()
    assert "CONFIRMED_NONEMPTY" not in window.legal_switch_status_label.text()
    assert transport.call_count == 0
    repository.close()


def test_5_same_binding_render_keeps_confirmed_selection_over_unsaved_edit(
    tmp_path: Path,
) -> None:
    """R3: since CONFIRM TURN FACTS now auto-confirms a binding's legal
    switches in the same click, a populated-but-unconfirmed candidate list no
    longer exists as a reachable state -- there is always either an
    unresolved binding with zero candidates (see test_1b) or an already-
    confirmed one. This proves the confirmed selection stays authoritative:
    a same-binding re-render (e.g. editing an unrelated field) restores it
    rather than trusting a stray, never-confirmed widget edit."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001

    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.legal_switches == ("Gholdengo", "Dragonite")

    # Operator deselects one item directly in the widget, without clicking
    # either confirm button -- an unsaved, never-persisted edit.
    for index in range(window.legal_switch_list.count()):
        item = window.legal_switch_list.item(index)
        if item.text() == "Dragonite":
            item.setSelected(False)

    # An unrelated same-binding re-render must not silently promote that
    # unsaved edit -- it must keep reflecting the actual confirmed record.
    window.render_view()

    selected_names = {
        window.legal_switch_list.item(i).text()
        for i in range(window.legal_switch_list.count())
        if window.legal_switch_list.item(i).isSelected()
    }
    assert selected_names == {"Gholdengo", "Dragonite"}
    repository.close()


def test_6_restart_same_binding_hydrates_exact_confirmed_state(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    controller.confirm_legal_switches(
        legal_switches=("Dragonite",), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    db_path = repository.database_path
    repository.close()

    restarted = SQLiteRepository(db_path)
    from maple_next.application.match_service import MatchApplication
    from maple_next.providers.turn_transport import FakeTurnAdviceTransport
    from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
    from maple_next.ui.turn_state_flow import GeminiRichTurnAdviceAdapter, TurnStateFlowController

    class _SyncDispatch:
        def __init__(self, transport, request, config, *, on_succeeded, on_failed):
            self._transport, self._request, self._config = transport, request, config
            self._on_succeeded, self._on_failed = on_succeeded, on_failed

        def start(self) -> None:
            self._on_succeeded(self._transport.send(self._request, self._config))

    restarted_application = MatchApplication(restarted, tmp_path / "restart-export")
    restarted_controller = TurnStateFlowController(
        restarted_application,
        restarted,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        None,
        None,
        GeminiRichTurnAdviceAdapter(FakeTurnAdviceTransport(), dispatch_factory=_SyncDispatch),
    )
    summary = restarted_controller.turn_state_summary()
    assert summary.legal_switch_confirmation is not None
    assert summary.legal_switch_confirmation.legal_switches == ("Dragonite",)
    assert summary.legal_switch_confirmation.status is LegalSwitchStatus.CONFIRMED_NONEMPTY
    restarted.close()


def test_7_restart_stale_binding_is_unresolved(tmp_path: Path) -> None:
    """A restart whose reopened session no longer matches the persisted
    confirmation's binding (here: a different session entirely, standing in
    for "the persisted confirmation's revision/state predates what's now
    current") must show unresolved -- never resurrect a stale confirmation."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    window._on_confirm_turn_facts()  # noqa: SLF001
    controller.confirm_legal_switches(
        legal_switches=("Gholdengo",), status=LegalSwitchStatus.CONFIRMED_NONEMPTY
    )
    db_path = repository.database_path
    repository.close()

    restarted = SQLiteRepository(db_path)
    stale_lookup = restarted.get_legal_switch_confirmation(
        identity=TurnIdentity(
            session_id="a-different-session",
            match_id="a-different-match",
            generation=1,
            turn_id="turn-x",
            turn_number=1,
            battle_revision=1,
        ),
        based_on_confirmed_state_id="whatever",
        applied_selection_id="whatever",
    )
    assert stale_lookup is None
    restarted.close()


def test_r3a_fact_confirm_label_stays_factual_across_render_states(tmp_path: Path) -> None:
    """R3-A: CONFIRM TURN FACTS is never overwritten to look like the send
    action, at any render state -- initial, unresolved, confirmed, or
    provider-ready. Only the distinct explicit-send control may say SEND
    TURN TO GEMINI."""

    repository, controller, window, _transport, _adapter = build_production_compatible_window(
        tmp_path
    )

    # 1. initial render (before even starting Turn capture).
    window.render_view()
    assert window.confirm_turn_facts_button.text() == "CONFIRM TURN FACTS"

    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)

    # 2. unresolved legal switches render.
    window.confirm_turn_facts_button.click()
    assert window.confirm_turn_facts_button.text() == "CONFIRM TURN FACTS"

    # 3. legal switches confirmed render.
    for index in range(window.legal_switch_list.count()):
        window.legal_switch_list.item(index).setSelected(True)
    window._on_confirm_legal_switches_selected()  # noqa: SLF001
    assert window.confirm_turn_facts_button.text() == "CONFIRM TURN FACTS"

    # 4. provider-ready render: fact button stays factual; explicit send
    # control is now the one labeled SEND TURN TO GEMINI.
    summary = controller.turn_state_summary()
    assert summary.provider_ready is True
    assert window.confirm_turn_facts_button.text() == "CONFIRM TURN FACTS"
    assert window._bundle_c_gemini_send_button.text() == "SEND TURN TO GEMINI"  # noqa: SLF001

    # 5. new TurnIdentity render (re-confirming facts bumps the binding).
    window.confirm_turn_facts_button.click()
    assert window.confirm_turn_facts_button.text() == "CONFIRM TURN FACTS"
    repository.close()


def test_r3b_active_outside_selected_three_fails_closed() -> None:
    """R3-B: a confirmed active that is not itself a member of the applied
    selected_three must never cause every selected member to be silently
    promoted as a switch candidate -- candidate derivation (and the
    confirmation builder built on it) fail closed instead."""

    from maple_next.domain.legal_switches import (
        LegalSwitchError,
        confirm_legal_switches,
        derive_legal_switch_candidates,
    )
    from maple_next.domain.models import AppliedSelectionSnapshot

    applied = AppliedSelectionSnapshot(
        applied_selection_id="applied-r3b",
        selected_three=("A", "B", "C"),
        lead="A",
        backline=("B", "C"),
        source_advice_id="advice-r3b",
    )
    with pytest.raises(LegalSwitchError, match="CURRENT_ACTIVE_OUTSIDE_SELECTED_THREE"):
        derive_legal_switch_candidates(
            applied=applied, current_active_name="X", local_memory_by_name={}
        )
    with pytest.raises(LegalSwitchError, match="CURRENT_ACTIVE_OUTSIDE_SELECTED_THREE"):
        confirm_legal_switches(
            confirmation_id="c-r3b",
            identity=TurnIdentity(
                session_id="s", match_id="m", generation=1, turn_id="t", turn_number=1,
                battle_revision=1,
            ),
            based_on_confirmed_state_id="state-1",
            applied=applied,
            current_active_name="X",
            local_memory_by_name={},
            legal_switches=("A", "B", "C"),
            status=LegalSwitchStatus.CONFIRMED_NONEMPTY,
            confirmation=_r3_confirmation(),
        )

    # Preserved: active=A -> candidates only B/C, subject to faint filtering.
    candidates = derive_legal_switch_candidates(
        applied=applied, current_active_name="A", local_memory_by_name={}
    )
    assert candidates == ("B", "C")


def test_8_operator_edited_prefill_next_turn_same_active_gets_fresh_candidates(
    tmp_path: Path,
) -> None:
    """R3R2 mandatory regression 1: the pre-confirm PREFILL must be scoped
    to the complete TurnIdentity, not merely the active-Pokemon text. Turn 1:
    prefill B/C, operator removes B -> [C], CONFIRM. Turn 2: same active A
    again -- despite the identical active text, this is a brand new
    TurnIdentity, so the pre-confirm prefill must come back fresh as B/C
    (all selected), never silently carrying forward Turn 1's [C] edit."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    for index in range(window.legal_switch_list.count()):
        item = window.legal_switch_list.item(index)
        if item.text() == "Gholdengo":
            item.setSelected(False)
    window._on_confirm_turn_facts()  # noqa: SLF001

    turn1_identity = controller.turn_state_summary().identity
    assert turn1_identity is not None

    _lifecycle_submit_turn_advice(window)
    window.actual_action_type_box.setCurrentText("MOVE")
    window.actual_action_name_box.setCurrentText("Flower Trick")
    window.actual_action_confirm_checkbox.setChecked(True)
    window.opponent_action_type_box.setCurrentText("選択してください")
    window._on_record_action()  # noqa: SLF001
    window._on_next_turn()  # noqa: SLF001
    window.render_view()

    turn2_identity = controller.turn_state_summary().identity
    assert turn2_identity is not None
    assert turn2_identity != turn1_identity

    # Same active as Turn 1, re-entered fresh for Turn 2.
    window.self_active_box.setCurrentText(SELECTED_THREE[0])

    assert {
        window.legal_switch_list.item(i).text() for i in range(window.legal_switch_list.count())
    } == {"Gholdengo", "Dragonite"}
    assert all(
        window.legal_switch_list.item(i).isSelected()
        for i in range(window.legal_switch_list.count())
    )
    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is None
    assert "未確認" in window.legal_switch_status_label.text()
    assert transport.call_count == 0
    repository.close()


def test_9_operator_edited_prefill_new_match_same_active_gets_fresh_candidates(
    tmp_path: Path,
) -> None:
    """R3R2 decisive regression 2: identical to test_8 but across a NEW
    MATCH (fresh session/match/generation, not merely a fresh turn). Match
    1: prefill B/C, operator removes B -> [C], CONFIRM. Match 2: new
    session/match/generation, same selected_three, same active A. The
    visible pre-confirm selection must come back fresh as B/C -- an
    immediate CONFIRM in Match 2 must never persist Match 1's stale [C]."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    _fill_minimal_current_state(window)
    for index in range(window.legal_switch_list.count()):
        item = window.legal_switch_list.item(index)
        if item.text() == "Gholdengo":
            item.setSelected(False)
    window._on_confirm_turn_facts()  # noqa: SLF001

    match1_identity = controller.turn_state_summary().identity
    assert match1_identity is not None
    assert controller.turn_state_summary().legal_switch_confirmation is not None

    # NEW MATCH: release the active session slot (mirrors the real
    # abort/new-match lifecycle -- ``new_match()`` refuses to create a
    # second session while one is still active) and start a genuinely new
    # session/match/generation binding, same selected_three/active as
    # before.
    controller.abort_match(human_confirmed=True)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    match2_identity = controller.turn_state_summary().identity
    assert match2_identity is not None
    assert match2_identity != match1_identity
    assert controller.turn_state_summary().legal_switch_confirmation is None

    window.self_active_box.setCurrentText(SELECTED_THREE[0])

    assert {
        window.legal_switch_list.item(i).text() for i in range(window.legal_switch_list.count())
    } == {"Gholdengo", "Dragonite"}
    assert all(
        window.legal_switch_list.item(i).isSelected()
        for i in range(window.legal_switch_list.count())
    )
    summary = controller.turn_state_summary()
    assert summary.legal_switch_confirmation is None
    assert "未確認" in window.legal_switch_status_label.text()
    assert transport.call_count == 0
    repository.close()


def test_10_prefill_edit_survives_unrelated_same_identity_rerender(tmp_path: Path) -> None:
    """R3R2 mandatory regression 3: same TurnIdentity, same active, operator
    edits the prefill, then an unrelated field changes and the workbench
    re-renders -- the operator's edit must be preserved, not reset. The UI
    must not become annoying by discarding edits on every render."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window.self_active_box.setCurrentText(SELECTED_THREE[0])
    assert {
        window.legal_switch_list.item(i).text() for i in range(window.legal_switch_list.count())
    } == {"Gholdengo", "Dragonite"}

    for index in range(window.legal_switch_list.count()):
        item = window.legal_switch_list.item(index)
        if item.text() == "Gholdengo":
            item.setSelected(False)

    # Unrelated field edit -> re-render via the same active-changed hook
    # (identity unchanged, active text unchanged).
    window.opponent_hp_box.setCurrentText("50")
    window._on_self_active_changed_for_legal_switches()  # noqa: SLF001

    selected_names = {
        window.legal_switch_list.item(i).text()
        for i in range(window.legal_switch_list.count())
        if window.legal_switch_list.item(i).isSelected()
    }
    assert selected_names == {"Dragonite"}
    assert transport.call_count == 0
    repository.close()


def test_11_active_change_same_identity_discards_old_selection(tmp_path: Path) -> None:
    """R3R2 mandatory regression 4: same TurnIdentity, active changes from A
    to B -- the previous active's edited selection must never leak into B's
    fresh candidate set."""

    repository, controller, window, transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window.self_active_box.setCurrentText(SELECTED_THREE[0])
    for index in range(window.legal_switch_list.count()):
        item = window.legal_switch_list.item(index)
        if item.text() == "Gholdengo":
            item.setSelected(False)
    assert {
        window.legal_switch_list.item(i).text()
        for i in range(window.legal_switch_list.count())
        if window.legal_switch_list.item(i).isSelected()
    } == {"Dragonite"}

    window.self_active_box.setCurrentText(SELECTED_THREE[1])

    assert {
        window.legal_switch_list.item(i).text() for i in range(window.legal_switch_list.count())
    } == {SELECTED_THREE[0], "Dragonite"}
    assert all(
        window.legal_switch_list.item(i).isSelected()
        for i in range(window.legal_switch_list.count())
    )
    assert transport.call_count == 0
    repository.close()
