"""GUI-independent operator controller for the manual Selection APPLY flow."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from maple_next.application.projection import DomainProjection
from maple_next.application.service import BattleApplication, DomainError
from maple_next.domain.enums import ResultDisposition
from maple_next.domain.models import AppliedSelectionSnapshot
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter


class OperatorInputError(ValueError):
    """Raised when human-entered UI values are incomplete or illegal."""


@dataclass(frozen=True, slots=True)
class AdviceView:
    selected_three: tuple[str, str, str]
    lead: str
    is_mock: bool = True


@dataclass(frozen=True, slots=True)
class AppliedSelectionView:
    selected_three: tuple[str, str, str]
    lead: str
    backline: tuple[str, str]


@dataclass(frozen=True, slots=True)
class OperatorView:
    projection: DomainProjection
    error_message: str | None
    self_team: tuple[str, ...]
    opponent_team: tuple[str, ...]
    advice: AdviceView | None
    applied_selection: AppliedSelectionView | None

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


_ERROR_MESSAGES = {
    "ACTIVE_MATCH_EXISTS": "進行中の対戦があるため、NEW MATCHは作成できません。",
    "HUMAN_APPLY_REQUIRED": "APPLY前に確認チェックを入れてください。",
    "SELECTED_THREE_MUST_HAVE_EXACTLY_THREE": "実際に選んだポケモンを3体ちょうど選択してください。",
    "DUPLICATE_SELECTION": "実際の選出に同じポケモンを重複して指定できません。",
    "SELECTION_OUTSIDE_REVIEWED_TEAM": "実際の選出には、自分の確認済み6体だけを指定してください。",
    "LEAD_NOT_IN_SELECTED_THREE": "先発は、実際に選んだ3体の中から指定してください。",
    "CURRENT_SELECTION_ADVICE_REQUIRED": "APPLY前にMOCK Selection Adviceを受領してください。",
    "REVIEWED_SELECTION_UNAVAILABLE": "確認済みSelection factsを読み込めません。もう一度確認してください。",
    "EXPECTED_SELECTION_OPEN": "現在の状態ではSelection factsを更新できません。",
    "EXPECTED_SELECTION_ADVICE_READY": "現在の状態ではAPPLYできません。",
    "PROVIDER_REQUEST_PENDING": "MOCK Selection Adviceの処理中です。",
    "PROVIDER_DELIVERY_UNKNOWN": "前回のprovider結果が不明なため、新しい送信はできません。",
}


def _domain_message(error: DomainError) -> str:
    code = str(error)
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


class SelectionFlowController:
    """Coordinates explicit UI commands while DomainProjection remains the display authority."""

    def __init__(
        self,
        application: BattleApplication,
        repository: SQLiteRepository,
        mock_adapter: MockSelectionAdviceAdapter,
    ) -> None:
        self._application = application
        self._repository = repository
        self._mock_adapter = mock_adapter
        self._error_message: str | None = None

    @property
    def network_call_count(self) -> int:
        return self._mock_adapter.network_call_count

    def refresh(self) -> OperatorView:
        projection = self._application.projection()
        self_team: tuple[str, ...] = ()
        opponent_team: tuple[str, ...] = ()
        advice: AdviceView | None = None
        applied_selection: AppliedSelectionView | None = None

        if projection.current_reviewed_selection_id is not None:
            facts = self._repository.get_selection_facts(projection.current_reviewed_selection_id)
            self_team = facts.self_team
            opponent_team = facts.opponent_team
        if projection.current_selection_advice_id is not None:
            stored_advice = self._repository.get_selection_advice(
                projection.current_selection_advice_id
            )
            advice = AdviceView(
                selected_three=cast(tuple[str, str, str], stored_advice["selected_three"]),
                lead=cast(str, stored_advice["lead"]),
            )
        if projection.current_applied_selection_id is not None:
            stored_selection = self._repository.get_applied_selection(
                projection.current_applied_selection_id
            )
            applied_selection = self._to_applied_view(stored_selection)

        return OperatorView(
            projection=projection,
            error_message=self._error_message,
            self_team=self_team,
            opponent_team=opponent_team,
            advice=advice,
            applied_selection=applied_selection,
        )

    def new_match(self) -> OperatorView:
        return self._run_domain_command(self._application.new_match)

    def confirm_selection_facts(
        self,
        self_entries: Sequence[str],
        opponent_entries: Sequence[str],
    ) -> OperatorView:
        try:
            self_team = validate_team(self_entries, label="自分のチーム")
            opponent_team = validate_team(opponent_entries, label="相手のチーム")
            self._application.confirm_selection_facts(self_team, opponent_team)
        except OperatorInputError as error:
            self._error_message = str(error)
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = "Selection factsの保存に失敗しました。入力内容は反映されていません。"
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

    def clear_error(self) -> OperatorView:
        self._error_message = None
        return self.refresh()

    def _run_domain_command(self, command: Callable[[], object]) -> OperatorView:
        try:
            command()
        except DomainError as error:
            self._error_message = _domain_message(error)
        except RuntimeError:
            self._error_message = "対戦の作成に失敗しました。新しいsessionは作成されていません。"
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
