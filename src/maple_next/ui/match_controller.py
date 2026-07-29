"""GUI-independent controller for explicit match completion and export."""

from __future__ import annotations

from dataclasses import dataclass

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.domain.enums import MatchOutcome
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.controller import OperatorInputError, OperatorView
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.explicit_turn_number import ExplicitTurnNumberController
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter


@dataclass(frozen=True, slots=True)
class MatchOperatorView(OperatorView):
    outcome: str | None = None
    ended_at_utc: str | None = None
    final_battle_revision: int | None = None
    turn_count: int = 0
    action_count: int = 0
    export_path: str | None = None
    export_sha256: str | None = None
    export_schema_version: str | None = None


_MATCH_ERROR_MESSAGES = {
    "HUMAN_MATCH_OUTCOME_CONFIRMATION_REQUIRED": "WIN / LOSE確定前に確認チェックを入れてください。",
    "MATCH_OUTCOME_ALREADY_SET": "この対戦の勝敗は既に確定しており変更できません。",
    "MATCH_END_NOT_ALLOWED_IN_CURRENT_STATE": (
        "現在の状態では対戦を終了できません。BATTLE_READYまたはTURN_RECORDEDで操作してください。"
    ),
    "EXPECTED_MATCH_ENDED": "勝敗を確定してからMATCH JSONを保存してください。",
    "MATCH_OUTCOME_REQUIRED": "確定済みのWIN / LOSEを読み込めません。",
    "MATCH_EXPORT_RECORD_MISSING": "保存済みJSONの記録を読み込めません。",
    "EXPORT_FILE_CONTENT_MISMATCH": (
        "既存JSONの内容がcanonical recordと一致しません。対象fileを確認して再試行してください。"
    ),
    "EXPORT_HASH_MISMATCH": (
        "保存済みJSONのSHA-256が一致しません。対象fileを確認して再試行してください。"
    ),
    "EXPORT_PATH_MISMATCH": "保存先が現在のruntime export directoryと一致しません。",
    "EXPORT_DIRECTORY_INSIDE_REPOSITORY": (
        "保存先がrepository内であるためMATCH JSONを保存できません。"
        "repository外のruntime/user-dataフォルダーを指定してください。"
    ),
    "EXPORT_FILE_UNREADABLE": "保存済みJSONを読み込めません。権限とfile状態を確認してください。",
    "EXPORT_WRITE_FAILED": (
        "MATCH JSONの書き込みに失敗しました。保存先を確認して再試行してください。"
    ),
    "MATCH_CHANGED_DURING_EXPORT": "保存中にactive matchが変わったため処理を中止しました。",
    "CANONICAL_TURN_FACTS_MISSING": "canonical Turn factsが不足しているため保存できません。",
    "CANONICAL_ACTUAL_ACTION_MISSING": "canonical actual actionが不足しているため保存できません。",
    "EXPECTED_MATCH_EXPORTED": "MATCH JSONの保存成功後にNEW MATCHを実行してください。",
}


def _match_message(error: DomainError) -> str:
    code = str(error)
    return _MATCH_ERROR_MESSAGES.get(code, f"操作を完了できませんでした: {code}")


class MatchFlowController(ExplicitTurnNumberController):
    """Coordinates explicit outcome, export, and next-match UI commands."""

    def __init__(
        self,
        application: MatchApplication,
        repository: SQLiteRepository,
        mock_adapter: MockSelectionAdviceAdapter,
        mock_turn_adapter: MockTurnAdviceAdapter | None = None,
        gemini_adapter: GeminiSelectionAdviceAdapter | None = None,
    ) -> None:
        super().__init__(
            application, repository, mock_adapter, mock_turn_adapter, gemini_adapter
        )
        self._match_application = application

    def refresh(self) -> MatchOperatorView:
        base = super().refresh()
        outcome_value: str | None = None
        ended_at_utc: str | None = None
        final_revision: int | None = None
        export_path: str | None = None
        export_sha256: str | None = None
        export_schema_version: str | None = None
        turn_count = 0
        action_count = 0
        session_id = base.projection.session_id
        if session_id is not None:
            outcome = self._repository.get_match_outcome(session_id)
            export = self._repository.get_match_export(session_id)
            turn_count = self._repository.count_turns(session_id)
            action_count = self._repository.count_actions(session_id)
            if outcome is not None:
                outcome_value = outcome.outcome.value
                ended_at_utc = outcome.ended_at_utc
                final_revision = outcome.final_battle_revision
            if export is not None:
                export_path = export.export_path
                export_sha256 = export.sha256
                export_schema_version = export.schema_version
        return MatchOperatorView(
            projection=base.projection,
            error_message=base.error_message,
            self_team=base.self_team,
            opponent_team=base.opponent_team,
            advice=base.advice,
            applied_selection=base.applied_selection,
            turn_facts=base.turn_facts,
            turn_advice=base.turn_advice,
            action_history=base.action_history,
            outcome=outcome_value,
            ended_at_utc=ended_at_utc,
            final_battle_revision=final_revision,
            turn_count=turn_count,
            action_count=action_count,
            export_path=export_path,
            export_sha256=export_sha256,
            export_schema_version=export_schema_version,
        )

    def end_match(self, outcome: str, *, human_confirmed: bool) -> MatchOperatorView:
        try:
            try:
                typed_outcome = MatchOutcome(outcome.strip())
            except ValueError as exc:
                raise OperatorInputError("WINまたはLOSEを選択してください。") from exc
            self._match_application.end_match(
                typed_outcome,
                human_confirmed=human_confirmed,
            )
        except OperatorInputError as error:
            self._error_message = str(error)
        except DomainError as error:
            self._error_message = _match_message(error)
        except RuntimeError:
            self._error_message = "勝敗の保存に失敗しました。canonical stateは変更されていません。"
        else:
            self._error_message = None
        return self.refresh()

    def save_match_json(self) -> MatchOperatorView:
        try:
            self._match_application.export_match()
        except DomainError as error:
            self._error_message = _match_message(error)
        except RuntimeError:
            self._error_message = "MATCH JSONの保存に失敗しました。MATCH_ENDEDを維持しています。"
        else:
            self._error_message = None
        return self.refresh()

    def new_match_after_export(self) -> MatchOperatorView:
        try:
            self._match_application.new_match_after_export()
        except DomainError as error:
            self._error_message = _match_message(error)
        except RuntimeError:
            self._error_message = "NEW MATCHの作成に失敗しました。前試合は変更されていません。"
        else:
            self._error_message = None
        return self.refresh()
