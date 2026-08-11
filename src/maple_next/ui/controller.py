"""GUI-independent operator controller for Selection and manual Turn flows."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from maple_next.application.projection import DomainProjection
from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import ActionOrder, ActionType, HpBucket, ResultDisposition
from maple_next.domain.models import AppliedSelectionSnapshot, SelfTeamPreset
from maple_next.domain.team_build import ChampionsTeamBuild
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter, describe_gemini_failure


class OperatorInputError(ValueError):
    """Raised when human-entered UI values are incomplete or illegal."""


@dataclass(frozen=True, slots=True)
class AdviceView:
    selected_three: tuple[str, str, str]
    lead: str
    source_type: str = "MOCK"
    is_mock: bool = True


@dataclass(frozen=True, slots=True)
class AppliedSelectionView:
    selected_three: tuple[str, str, str]
    lead: str
    backline: tuple[str, str]


@dataclass(frozen=True, slots=True)
class TurnFactsView:
    snapshot_id: str
    turn_number: int
    self_active: str
    opponent_active: str
    self_hp: str
    opponent_hp: str
    legal_moves: tuple[str, ...]
    legal_switches: tuple[str, ...]
    human_note: str


@dataclass(frozen=True, slots=True)
class TurnAdviceView:
    action_type: str
    action_name: str
    opponent_prediction: str
    rationale: str
    is_mock: bool = True
    source_type: str = "MOCK"
    model: str = "mock-dev"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordedActionView:
    turn_number: int
    action_type: str
    action_name: str
    opponent_action_type: str | None = None
    opponent_action_name: str = ""
    action_order: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class OperatorView:
    projection: DomainProjection
    error_message: str | None
    self_team: tuple[str, ...]
    opponent_team: tuple[str, ...]
    advice: AdviceView | None
    applied_selection: AppliedSelectionView | None
    turn_facts: TurnFactsView | None = None
    turn_advice: TurnAdviceView | None = None
    action_history: tuple[RecordedActionView, ...] = ()
    self_team_build: ChampionsTeamBuild | None = None
    self_team_build_sha256: str | None = None
    self_team_build_status: str = "NAMES_ONLY"
    persistence_reads_allowed: bool = True
    """Explicit renderer contract: False means this view was built without a
    durable DB read and must not trigger any further DB-backed read while
    rendering (status gates, repository lookups, controller refresh)."""

    @property
    def application_mode(self) -> str:
        return self.projection.application_mode

    @property
    def session_state(self) -> str | None:
        return self.projection.session_state

    @property
    def primary_cta(self) -> str:
        return self.projection.primary_cta

    @property
    def provider_status(self) -> str:
        return self.projection.provider_status

    @property
    def battle_revision(self) -> int | None:
        return self.projection.battle_revision

    @property
    def turn_number(self) -> int | None:
        return self.projection.turn_number


_ERROR_MESSAGES = {
    "ACTIVE_MATCH_EXISTS": "進行中の対戦があるため、NEW MATCHは作成できません。",
    "HUMAN_APPLY_REQUIRED": "APPLY前に確認チェックを入れてください。",
    "SELECTED_THREE_MUST_HAVE_EXACTLY_THREE": "実際に選んだポケモンを3体ちょうど選択してください。",
    "DUPLICATE_SELECTION": "実際の選出に同じポケモンを重複して指定できません。",
    "SELECTION_OUTSIDE_REVIEWED_TEAM": "実際の選出には、自分の確認済み6体だけを指定してください。",
    "LEAD_NOT_IN_SELECTED_THREE": "先発は、実際に選んだ3体の中から指定してください。",
    "CURRENT_SELECTION_ADVICE_REQUIRED": "APPLY前にMOCK Selection Adviceを受領してください。",
    "REVIEWED_SELECTION_UNAVAILABLE": (
        "確認済みSelection factsを読み込めません。もう一度確認してください。"
    ),
    "EXPECTED_SELECTION_OPEN": "現在の状態ではSelection factsを更新できません。",
    "EXPECTED_SELECTION_ADVICE_READY": "現在の状態ではAPPLYできません。",
    "EXPECTED_BATTLE_READY": "BATTLE_READYでのみTurnを開始できます。",
    "EXPECTED_TURN_CAPTURE_PENDING_OR_REVIEWED": (
        "現在の状態ではTurn factsを確認・修正できません。"
    ),
    "EXPECTED_TURN_REVIEWED": "Turn facts確認後にこの操作を行ってください。",
    "EXPECTED_TURN_RECORDED": "実際の行動を記録してからNEXT TURNを押してください。",
    "HUMAN_TURN_FACTS_CONFIRMATION_REQUIRED": "Turn factsの確認チェックを入れてください。",
    "HUMAN_ACTION_CONFIRMATION_REQUIRED": "実際の行動の確認チェックを入れてください。",
    "CURRENT_TURN_REQUIRED": "現在のTurnを読み込めません。",
    "CURRENT_TURN_ALREADY_EXISTS": "現在のTurnがあるため、重複開始できません。",
    "APPLIED_SELECTION_REQUIRED": "Turn開始前にSelection APPLYを完了してください。",
    "SELF_ACTIVE_OUTSIDE_APPLIED_SELECTION": "自分のactiveはAPPLY済み3体から選んでください。",
    "SWITCH_OUTSIDE_APPLIED_SELECTION": "交代候補はAPPLY済み3体から選んでください。",
    "SELF_ACTIVE_IN_SWITCH_CANDIDATES": "現在のactiveを交代候補に含めないでください。",
    "REVIEWED_TURN_FACTS_REQUIRED": "確認済みTurn factsが必要です。",
    "CURRENT_TURN_ADVICE_EXISTS": "現在のTurnには既にMOCK Adviceがあります。",
    "CURRENT_TURN_ADVICE_REQUIRED": "実際の行動を記録する前にMOCK Turn Adviceを受領してください。",
    "ACTION_ALREADY_RECORDED": "このTurnの実際の行動は既に記録済みです。",
    "ACTION_OUTSIDE_REVIEWED_LEGAL_ACTIONS": "確認済みの合法行動から選んでください。",
    "PROVIDER_REQUEST_PENDING": "MOCK Adviceの処理中です。",
    "PROVIDER_DELIVERY_UNKNOWN": "前回のprovider結果が不明なため、新しい送信はできません。",
    "GEMINI_SELECTION_ATTEMPT_CONSUMED": (
        "このSelectionではGemini送信を実行済みです。再送できません。"
    ),
}


def _domain_message(error: DomainError) -> str:
    code = str(error)
    if code.startswith("INVALID_TURN_FACTS:"):
        return "Turn factsの入力条件を確認してください。"
    return _ERROR_MESSAGES.get(code, f"操作を完了できませんでした: {code}")


def validate_team(entries: Sequence[str], *, label: str) -> tuple[str, ...]:
    """Validate exactly six explicit names without guessing or filling blanks."""

    if len(entries) != 6:
        raise OperatorInputError(f"{label}は6体ちょうど入力してください。")
    normalized = tuple(value.strip() for value in entries)
    empty_positions = [str(index + 1) for index, value in enumerate(normalized) if not value]
    if empty_positions:
        positions = "、".join(empty_positions)
        raise OperatorInputError(f"{label}の{positions}番目が空欄です。")
    duplicates = sorted({value for value in normalized if normalized.count(value) > 1})
    if duplicates:
        raise OperatorInputError(f"{label}に重複があります: {'、'.join(duplicates)}")
    return normalized


def validate_selected_three(
    selected_three: Sequence[str],
    *,
    lead: str,
    self_team: Sequence[str],
    label: str,
) -> tuple[str, str, str]:
    """Validate explicit three-and-lead input for mock advice or human APPLY."""

    if len(selected_three) != 3:
        raise OperatorInputError(f"{label}は3体ちょうど選択してください。")
    normalized = tuple(value.strip() for value in selected_three)
    if any(not value for value in normalized):
        raise OperatorInputError(f"{label}に未選択の欄があります。")
    if len(set(normalized)) != 3:
        raise OperatorInputError(f"{label}に同じポケモンを重複して指定できません。")
    if any(value not in self_team for value in normalized):
        raise OperatorInputError(f"{label}には、自分の確認済み6体だけを指定してください。")
    normalized_lead = lead.strip()
    if normalized_lead not in normalized:
        raise OperatorInputError(f"{label}の先発は、選択した3体の中から指定してください。")
    return (normalized[0], normalized[1], normalized[2])


def validate_hp_bucket(value: str, *, label: str) -> HpBucket:
    try:
        return HpBucket(value.strip())
    except ValueError as exc:
        raise OperatorInputError(f"{label}は一覧から選択してください。") from exc


def validate_legal_moves(entries: Sequence[str]) -> tuple[str, ...]:
    if not 1 <= len(entries) <= 4:
        raise OperatorInputError("合法moveは1〜4件入力してください。")
    normalized = tuple(value.strip() for value in entries)
    if any(not value for value in normalized):
        raise OperatorInputError("合法moveに空欄を含めないでください。")
    if len(set(normalized)) != len(normalized):
        raise OperatorInputError("合法moveに重複があります。")
    return normalized


def validate_switches(entries: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(value.strip() for value in entries)
    if any(not value for value in normalized):
        raise OperatorInputError("交代候補に空欄を含めないでください。")
    if len(set(normalized)) != len(normalized):
        raise OperatorInputError("交代候補に重複があります。")
    return normalized


def validate_action_type(value: str) -> ActionType:
    try:
        return ActionType(value.strip())
    except ValueError as exc:
        raise OperatorInputError("行動種別はMOVEまたはSWITCHを選択してください。") from exc


class SelectionFlowController:
    """Coordinates explicit UI commands while DomainProjection remains authoritative."""

    def __init__(
        self,
        application: BattleApplication,
        repository: SQLiteRepository,
        mock_adapter: MockSelectionAdviceAdapter,
        mock_turn_adapter: MockTurnAdviceAdapter | None = None,
        gemini_adapter: GeminiSelectionAdviceAdapter | None = None,
    ) -> None:
        self._application = application
        self._repository = repository
        self._mock_adapter = mock_adapter
        self._mock_turn_adapter = mock_turn_adapter or MockTurnAdviceAdapter()
        self._gemini_adapter = gemini_adapter
        self._error_message: str | None = None
        self._staged_self_team_build: ChampionsTeamBuild | None = None

    @property
    def network_call_count(self) -> int:
        gemini_count = self._gemini_adapter.network_call_count if self._gemini_adapter else 0
        return (
            self._mock_adapter.network_call_count
            + self._mock_turn_adapter.network_call_count
            + gemini_count
        )

    def list_self_team_presets(self) -> tuple[SelfTeamPreset, ...]:
        try:
            return self._repository.list_self_team_presets()
        except (ValueError, sqlite3.Error, RuntimeError):
            self._error_message = "team preset detailed data is invalid"
            return ()

    def last_used_self_team_preset(self) -> SelfTeamPreset | None:
        try:
            return self._repository.get_last_used_self_team_preset()
        except (ValueError, sqlite3.Error, RuntimeError):
            self._error_message = "team preset detailed data is invalid"
            return None

    def stage_self_team_build(self, team_build: ChampionsTeamBuild | None) -> None:
        """Set the in-memory draft used by the next human selection confirm."""

        self._staged_self_team_build = team_build

    def save_self_team_preset(
        self,
        name: str,
        self_entries: Sequence[str],
        team_build: ChampionsTeamBuild | None = None,
    ) -> OperatorView:
        try:
            display_name, normalized_name = self._validate_preset_name(name)
            self_team = self._validate_preset_team(self_entries)
            self._validate_preset_build(self_team, team_build)
            with self._repository.transaction():
                self._repository.insert_self_team_preset(
                    preset_id=str(uuid4()),
                    name=display_name,
                    normalized_name=normalized_name,
                    self_team=self_team,
                    team_build=team_build,
                )
        except (OperatorInputError, sqlite3.Error, ValueError) as error:
            self._error_message = self._preset_error_message(error)
        else:
            self._error_message = None
        return self.refresh()

    def update_self_team_preset(
        self,
        preset_id: str,
        name: str,
        self_entries: Sequence[str],
        team_build: ChampionsTeamBuild | None = None,
    ) -> OperatorView:
        try:
            display_name, normalized_name = self._validate_preset_name(name)
            self_team = self._validate_preset_team(self_entries)
            self._validate_preset_build(self_team, team_build)
            with self._repository.transaction():
                updated = self._repository.update_self_team_preset(
                    preset_id=preset_id,
                    name=display_name,
                    normalized_name=normalized_name,
                    self_team=self_team,
                    team_build=team_build,
                )
                if not updated:
                    raise OperatorInputError("選択した構築が見つかりません。")
        except (OperatorInputError, sqlite3.Error, ValueError) as error:
            self._error_message = self._preset_error_message(error)
        else:
            self._error_message = None
        return self.refresh()

    def delete_self_team_preset(self, preset_id: str) -> OperatorView:
        try:
            with self._repository.transaction():
                if not self._repository.delete_self_team_preset(preset_id):
                    raise OperatorInputError("選択した構築が見つかりません。")
        except (OperatorInputError, sqlite3.Error, ValueError, RuntimeError) as error:
            self._error_message = self._preset_error_message(error)
        else:
            self._error_message = None
        return self.refresh()

    def use_self_team_preset(self, preset_id: str) -> SelfTeamPreset | None:
        try:
            preset = self._repository.get_self_team_preset(preset_id)
        except (ValueError, sqlite3.Error, RuntimeError):
            self._error_message = "team preset detailed data is invalid"
            return None
        if preset is None:
            self._error_message = "選択した構築が見つかりません。"
            return None
        try:
            with self._repository.transaction():
                self._repository.set_last_used_self_team_preset(preset_id)
        except (sqlite3.Error, RuntimeError):
            self._error_message = "team preset could not be updated"
            return None
        self._staged_self_team_build = preset.team_build
        self._error_message = None
        return preset

    @staticmethod
    def _validate_preset_name(name: str) -> tuple[str, str]:
        display_name = name.strip()
        if not display_name:
            raise OperatorInputError("構築名を入力してください。")
        if len(display_name) > 80:
            raise OperatorInputError("構築名は80文字以内で入力してください。")
        return display_name, display_name.casefold()

    @staticmethod
    def _validate_preset_team(
        entries: Sequence[str],
    ) -> tuple[str, str, str, str, str, str]:
        team = validate_team(entries, label="自分の構築")
        return (team[0], team[1], team[2], team[3], team[4], team[5])

    @staticmethod
    def _validate_preset_build(
        self_team: tuple[str, str, str, str, str, str],
        team_build: ChampionsTeamBuild | None,
    ) -> None:
        if team_build is not None and team_build.pokemon_names != self_team:
            raise OperatorInputError("team build names must match the six team names")

    @staticmethod
    def _preset_error_message(error: Exception) -> str:
        if isinstance(error, sqlite3.IntegrityError):
            return "同じ名前の構築が既にあります。"
        if isinstance(error, sqlite3.Error):
            return "team preset could not be saved"
        if isinstance(error, ValueError) and not isinstance(error, OperatorInputError):
            return "team preset detailed data is invalid"
        return str(error)

    def refresh(self) -> OperatorView:
        projection = self._application.projection()
        self_team: tuple[str, ...] = ()
        opponent_team: tuple[str, ...] = ()
        advice: AdviceView | None = None
        applied_selection: AppliedSelectionView | None = None
        turn_facts: TurnFactsView | None = None
        turn_advice: TurnAdviceView | None = None
        action_history: tuple[RecordedActionView, ...] = ()
        self_team_build: ChampionsTeamBuild | None = self._staged_self_team_build
        self_team_build_sha256: str | None = (
            self_team_build.sha256() if self_team_build is not None else None
        )

        if projection.current_reviewed_selection_id is not None:
            facts = self._repository.get_selection_facts(projection.current_reviewed_selection_id)
            self_team = facts.self_team
            opponent_team = facts.opponent_team
            self_team_build = facts.self_team_build
            self_team_build_sha256 = facts.self_team_build_sha256
        if projection.current_selection_advice_id is not None:
            stored_advice = self._repository.get_selection_advice(
                projection.current_selection_advice_id
            )
            source_type = cast(str, stored_advice.get("source_type", "MOCK"))
            advice = AdviceView(
                selected_three=cast(tuple[str, str, str], stored_advice["selected_three"]),
                lead=cast(str, stored_advice["lead"]),
                source_type=source_type,
                is_mock=source_type != "GEMINI",
            )
        if projection.current_applied_selection_id is not None:
            stored_selection = self._repository.get_applied_selection(
                projection.current_applied_selection_id
            )
            applied_selection = self._to_applied_view(stored_selection)
        if projection.current_reviewed_board_id is not None:
            stored_turn_facts = self._repository.get_turn_facts(
                projection.current_reviewed_board_id
            )
            turn_facts = TurnFactsView(
                snapshot_id=stored_turn_facts.turn_facts_id,
                turn_number=stored_turn_facts.turn_number,
                self_active=stored_turn_facts.self_active,
                opponent_active=stored_turn_facts.opponent_active,
                self_hp=stored_turn_facts.self_hp.value,
                opponent_hp=stored_turn_facts.opponent_hp.value,
                legal_moves=stored_turn_facts.legal_moves,
                legal_switches=stored_turn_facts.legal_switches,
                human_note=stored_turn_facts.human_note,
            )
        if projection.current_turn_advice_id is not None:
            stored_turn_advice = self._repository.get_turn_advice(projection.current_turn_advice_id)
            turn_advice = TurnAdviceView(
                action_type=stored_turn_advice.action_type.value,
                action_name=stored_turn_advice.action_name,
                opponent_prediction=stored_turn_advice.opponent_prediction,
                rationale=stored_turn_advice.rationale,
                is_mock=stored_turn_advice.is_mock,
                source_type=stored_turn_advice.source_type,
                model=stored_turn_advice.model,
                warnings=stored_turn_advice.warnings,
            )
        if projection.session_id is not None:
            action_history = tuple(
                RecordedActionView(
                    turn_number=action.turn_number,
                    action_type=action.action_type.value,
                    action_name=action.action_name,
                    opponent_action_type=(
                        action.opponent_action_type.value
                        if action.opponent_action_type is not None
                        else None
                    ),
                    opponent_action_name=action.opponent_action_name,
                    action_order=action.action_order.value,
                )
                for action in self._repository.list_recorded_actions(projection.session_id)
            )

        return OperatorView(
            projection=projection,
            error_message=self._error_message,
            self_team=self_team,
            opponent_team=opponent_team,
            advice=advice,
            applied_selection=applied_selection,
            turn_facts=turn_facts,
            turn_advice=turn_advice,
            action_history=action_history,
            self_team_build=self_team_build,
            self_team_build_sha256=self_team_build_sha256,
            self_team_build_status=("DETAILED" if self_team_build is not None else "NAMES_ONLY"),
        )

    def new_match(self) -> OperatorView:
        return self._run_domain_command(
            self._application.new_match,
            "対戦の作成に失敗しました。新しいsessionは作成されていません。",
        )

    def confirm_selection_facts(
        self,
        self_entries: Sequence[str],
        opponent_entries: Sequence[str],
        self_team_build: ChampionsTeamBuild | None = None,
    ) -> OperatorView:
        try:
            self_team = validate_team(self_entries, label="自分のチーム")
            opponent_team = validate_team(opponent_entries, label="相手のチーム")
            selected_build = self_team_build or self._staged_self_team_build
            if (
                self_team_build is None
                and selected_build is not None
                and selected_build.pokemon_names != tuple(self_team)
            ):
                selected_build = None
            self._validate_preset_build(
                (
                    self_team[0],
                    self_team[1],
                    self_team[2],
                    self_team[3],
                    self_team[4],
                    self_team[5],
                ),
                selected_build,
            )
            self._application.confirm_selection_facts(
                self_team,
                opponent_team,
                self_team_build=selected_build,
            )
            self._staged_self_team_build = selected_build
        except OperatorInputError as error:
            self._error_message = str(error)
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = (
                "Selection factsの保存に失敗しました。入力内容は反映されていません。"
            )
        else:
            self._error_message = None
        return self.refresh()

    def submit_mock_advice(
        self,
        selected_three: Sequence[str],
        lead: str,
    ) -> OperatorView:
        current = self.refresh()
        try:
            typed_three = validate_selected_three(
                selected_three,
                lead=lead,
                self_team=current.self_team,
                label="MOCK Selection Advice",
            )
            result = self._mock_adapter.submit(
                self._application,
                selected_three=typed_three,
                lead=lead.strip(),
            )
            if result.disposition is not ResultDisposition.APPLIED:
                raise OperatorInputError("MOCK Selection Adviceを適用できませんでした。")
        except OperatorInputError as error:
            self._error_message = str(error)
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = "MOCK Selection Adviceの保存に失敗しました。"
        else:
            self._error_message = None
        return self.refresh()

    @property
    def gemini_send_available(self) -> bool:
        return self._gemini_adapter is not None

    def gemini_selection_attempt_consumed(self) -> bool:
        """Durable, restart-safe check: has this Selection identity already
        consumed its one production Gemini attempt?

        Recomputed from the database on every call. Used as a fail-closed
        guard even for direct/synthetic calls that bypass the UI's own
        disabled-button gate.
        """

        return self._application.gemini_selection_attempt_consumed()

    def send_selection_advice_to_gemini(
        self,
        *,
        on_result: Callable[[OperatorView], None],
    ) -> OperatorView:
        """Human-only explicit send. Must only be invoked from a trusted gate.

        Creates exactly one immutable job and dispatches its bounded provider policy
        call off the UI thread, and never auto-applies or auto-retries. The
        provider result — success or failure — arrives later via
        ``on_result`` once the strict binding contract has been applied (or
        rejected) on the UI thread.
        """

        current = self.refresh()
        if self._gemini_adapter is None:
            self._error_message = "Gemini adapterが設定されていません。"
            return self.refresh()
        if current.projection.session_state != "SELECTION_OPEN":
            self._error_message = "Selection factsが確認済みのSELECTION_OPENでのみ送信できます。"
            return self.refresh()
        if not current.projection.provider_send_enabled:
            self._error_message = "現在はGeminiへ送信できません。"
            return self.refresh()
        if self.gemini_selection_attempt_consumed():
            self._error_message = _domain_message(DomainError("GEMINI_SELECTION_ATTEMPT_CONSUMED"))
            return self.refresh()

        callback_already_ran = False

        def handle_applied(disposition: ResultDisposition) -> None:
            nonlocal callback_already_ran
            callback_already_ran = True
            if disposition is not ResultDisposition.APPLIED:
                self._error_message = "Gemini Selection Adviceを適用できませんでした。"
            else:
                self._error_message = None
            on_result(self.refresh())

        def handle_failed(reason: str) -> None:
            nonlocal callback_already_ran
            callback_already_ran = True
            self._error_message = describe_gemini_failure(reason)
            on_result(self.refresh())

        try:
            self._gemini_adapter.send(
                self._application,
                on_applied=handle_applied,
                on_failed=handle_failed,
            )
        except DomainError as error:
            self._error_message = _domain_message(error)
        else:
            if not callback_already_ran:
                self._error_message = None
        return self.refresh()

    def apply_selection(
        self,
        selected_three: Sequence[str],
        lead: str,
        *,
        human_confirmed: bool,
    ) -> OperatorView:
        current = self.refresh()
        try:
            typed_three = validate_selected_three(
                selected_three,
                lead=lead,
                self_team=current.self_team,
                label="実際の選出",
            )
            self._application.apply_selection(
                selected_three=typed_three,
                lead=lead.strip(),
                human_confirmed=human_confirmed,
            )
        except OperatorInputError as error:
            self._error_message = str(error)
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = "APPLYの保存に失敗しました。実際の選出は反映されていません。"
        else:
            self._error_message = None
        return self.refresh()

    def start_turn_capture(self) -> OperatorView:
        return self._run_domain_command(
            self._application.start_turn_capture,
            "Turn開始の保存に失敗しました。Turnは開始されていません。",
        )

    def confirm_turn_facts(
        self,
        *,
        self_active: str,
        opponent_active: str,
        self_hp: str,
        opponent_hp: str,
        legal_moves: Sequence[str],
        legal_switches: Sequence[str],
        human_note: str,
        human_confirmed: bool,
    ) -> OperatorView:
        try:
            normalized_self_active = self_active.strip()
            normalized_opponent_active = opponent_active.strip()
            if not normalized_self_active:
                raise OperatorInputError("自分のactiveを選択してください。")
            if not normalized_opponent_active:
                raise OperatorInputError("相手のactiveを入力してください。")
            typed_self_hp = validate_hp_bucket(self_hp, label="自分のHP")
            typed_opponent_hp = validate_hp_bucket(opponent_hp, label="相手のHP")
            typed_moves = validate_legal_moves(legal_moves)
            typed_switches = validate_switches(legal_switches)
            self._application.confirm_turn_facts(
                self_active=normalized_self_active,
                opponent_active=normalized_opponent_active,
                self_hp=typed_self_hp,
                opponent_hp=typed_opponent_hp,
                legal_moves=typed_moves,
                legal_switches=typed_switches,
                human_note=human_note,
                human_confirmed=human_confirmed,
            )
        except OperatorInputError as error:
            self._error_message = str(error)
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = (
                "Turn factsの保存に失敗しました。canonical stateは変更されていません。"
            )
        else:
            self._error_message = None
        return self.refresh()

    def submit_mock_turn_advice(
        self,
        *,
        action_type: str,
        action_name: str,
        opponent_prediction: str,
        rationale: str,
        warnings: tuple[str, ...] = (),
    ) -> OperatorView:
        current = self.refresh()
        try:
            typed_action = validate_action_type(action_type)
            normalized_name = action_name.strip()
            normalized_prediction = opponent_prediction.strip()
            normalized_rationale = rationale.strip()
            if current.turn_facts is None:
                raise OperatorInputError("確認済みTurn factsが必要です。")
            legal_actions = (
                current.turn_facts.legal_moves
                if typed_action is ActionType.MOVE
                else current.turn_facts.legal_switches
            )
            if normalized_name not in legal_actions:
                raise OperatorInputError("MOCK推奨actionは確認済み合法行動から選んでください。")
            if not normalized_prediction:
                raise OperatorInputError("相手の次行動予測を入力してください。")
            if not normalized_rationale:
                raise OperatorInputError("MOCK助言の理由を入力してください。")
            result = self._mock_turn_adapter.submit(
                self._application,
                action_type=typed_action,
                action_name=normalized_name,
                opponent_prediction=normalized_prediction,
                rationale=normalized_rationale,
                warnings=tuple(item.strip() for item in warnings if item.strip()),
            )
            if result.disposition is not ResultDisposition.APPLIED:
                raise OperatorInputError("MOCK Turn Adviceを適用できませんでした。")
        except OperatorInputError as error:
            self._error_message = str(error)
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = "MOCK Turn Adviceの保存に失敗しました。"
        else:
            self._error_message = None
        return self.refresh()

    def record_actual_action(
        self,
        *,
        action_type: str,
        action_name: str,
        human_confirmed: bool,
        opponent_action_type: str = "",
        opponent_action_name: str = "",
        action_order: str = ActionOrder.UNKNOWN.value,
    ) -> OperatorView:
        try:
            typed_action = validate_action_type(action_type)
            normalized_name = action_name.strip()
            if not normalized_name:
                raise OperatorInputError("実際に選んだ行動を選択してください。")
            normalized_opponent_type = opponent_action_type.strip()
            typed_opponent_type: ActionType | None = None
            if normalized_opponent_type:
                typed_opponent_type = validate_action_type(normalized_opponent_type)
            normalized_opponent_name = (
                opponent_action_name.strip() if typed_opponent_type is not None else ""
            )
            if typed_opponent_type is not None and not normalized_opponent_name:
                raise OperatorInputError("相手の実際の行動名を入力してください。")
            try:
                typed_order = ActionOrder(action_order.strip() or ActionOrder.UNKNOWN.value)
            except ValueError as exc:
                raise OperatorInputError("行動順序は一覧から選択してください。") from exc
            self._application.record_actual_action(
                action_type=typed_action,
                action_name=normalized_name,
                human_confirmed=human_confirmed,
                opponent_action_type=typed_opponent_type,
                opponent_action_name=normalized_opponent_name,
                action_order=typed_order,
            )
        except OperatorInputError as error:
            self._error_message = str(error)
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = "実際の行動の保存に失敗しました。履歴は変更されていません。"
        else:
            self._error_message = None
        return self.refresh()

    def next_turn(self) -> OperatorView:
        return self._run_domain_command(
            self._application.next_turn,
            "NEXT TURNの保存に失敗しました。Turn番号は変更されていません。",
        )

    def clear_error(self) -> OperatorView:
        self._error_message = None
        return self.refresh()

    def _run_domain_command(
        self,
        command: Callable[[], object],
        failure_message: str,
    ) -> OperatorView:
        try:
            command()
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = failure_message
        else:
            self._error_message = None
        return self.refresh()

    @staticmethod
    def _to_applied_view(snapshot: AppliedSelectionSnapshot) -> AppliedSelectionView:
        return AppliedSelectionView(
            selected_three=snapshot.selected_three,
            lead=snapshot.lead,
            backline=snapshot.backline,
        )
