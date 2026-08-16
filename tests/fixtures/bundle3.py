"""Shared Bundle 3 (Gemini V2) test helpers.

Two small pieces reused by every rich-state Turn Advice test:

- :func:`names_only_bundle3_context` -- the minimal, legitimate
  ``Bundle3TurnContext`` for a Turn 1 request whose reviewed
  ``SelectionFacts`` are names-only (three ``UNKNOWN`` build entries, empty
  battle memory). This is the *non-blocking* shape the spec requires to stay
  provider-ready, so pre-Bundle-3 regressions can keep asserting exactly
  what they asserted before.
- :func:`seed_selection_advice_binding` -- writes the durable
  ``async_jobs`` (SELECTION_ADVICE) + ``selection_advices`` rows the real
  apply-selection flow always produces, so the Bundle 3
  applied -> advice -> job -> reviewed-selection chain validates.

Nothing here sends anything or contacts a provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from maple_next.domain.battle_memory import (
    BattleMemory,
    Bundle3TurnContext,
    SelectedBuildMember,
    SelectedBuildStatus,
)
from maple_next.domain.champions_rules import build_rules_context, load_bundled_snapshot
from maple_next.domain.enums import BattleState, JobStatus, JobType
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.workers.contracts.models import JobEnvelope


def default_rules_context() -> dict[str, Any]:
    """The real, checked-in M-B Single ``rules_context`` for request-assembly tests.

    Bundle 4 (Gemini V2). Every rich-request-assembly test needs *some*
    valid ``rules_context`` dict; this resolves the actual bundled snapshot
    (the same one production code resolves) rather than a hand-rolled
    fixture, so these tests exercise the real contract shape.
    """

    return build_rules_context(load_bundled_snapshot())


def names_only_selected_builds(
    selected_three: tuple[str, str, str],
) -> tuple[SelectedBuildMember, ...]:
    return tuple(
        SelectedBuildMember(
            pokemon_name=name, build_status=SelectedBuildStatus.UNKNOWN, build=None
        )
        for name in selected_three
    )


def names_only_bundle3_context(
    *,
    selected_three: tuple[str, str, str],
    applied_selection_id: str = "applied-1",
    reviewed_selection_id: str = "selection-1",
    battle_memory: BattleMemory | None = None,
) -> Bundle3TurnContext:
    return Bundle3TurnContext(
        applied_selection_id=applied_selection_id,
        reviewed_selection_id=reviewed_selection_id,
        selected_three_builds=names_only_selected_builds(selected_three),
        battle_memory=battle_memory if battle_memory is not None else BattleMemory(),
    )


def seed_selection_advice_binding(
    repository: SQLiteRepository,
    *,
    session_id: str,
    match_id: str,
    generation: int,
    reviewed_selection_id: str,
    advice_id: str,
    selected_three: tuple[str, str, str],
    lead: str,
    job_id: str | None = None,
    base_battle_revision: int = 1,
) -> None:
    """Write the Selection Advice job + advice rows the real flow produces."""

    resolved_job_id = job_id or f"job-{advice_id}"
    repository.insert_job(
        JobEnvelope(
            contract_version="maple-worker.v1",
            job_id=resolved_job_id,
            command_id=f"command-{advice_id}",
            job_type=JobType.SELECTION_ADVICE,
            session_id=session_id,
            match_id=match_id,
            generation=generation,
            turn_number=None,
            base_battle_revision=base_battle_revision,
            expected_state=BattleState.SELECTION_OPEN,
            input_snapshot_id=reviewed_selection_id,
            request_payload_hash="0" * 64,
            human_authorized_at=datetime.now(UTC),
            status=JobStatus.SUCCEEDED,
        )
    )
    backline = tuple(name for name in selected_three if name != lead)
    repository.append_selection_advice(
        advice_id,
        session_id,
        resolved_job_id,
        selected_three,
        lead,
        (backline[0], backline[1]),
    )
