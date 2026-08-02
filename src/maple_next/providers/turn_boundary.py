"""Pure Turn Advice prompt/request-body builders and human-trigger dispatch policy.

No network calls, no secrets, no filesystem/DB paths, no API keys/headers/
endpoints, no worker/UI/provider imports. Everything in this module is a
pure function of its arguments.

## Integration contract for the next lane

A future integrator (application/service.py, a worker, or the UI) must call
:func:`decide_turn_advice_dispatch` exactly once, immediately before it would
otherwise create a TURN_ADVICE job, and must treat its
:class:`DispatchDecision` as the single source of truth for whether that job
creation is allowed. Specifically the integrator must:

- Compute ``is_current_binding`` itself, from durable session state (does the
  reviewed_snapshot_id / battle_revision the human is looking at right now
  still match the session's current one?).
- Compute ``has_pending_job`` itself, from the job store (is there a
  QUEUED/IN_FLIGHT TURN_ADVICE job for this session?).
- Compute ``attempt_consumed`` itself, from a durable idempotency ledger keyed
  by the full binding tuple (session, match, generation, turn, battle_revision,
  reviewed_snapshot_id) — the sibling of
  ``SQLiteRepository.reserve_gemini_selection_attempt`` for Turn Advice.

This module intentionally has no persistence access and cannot compute any
of those three inputs itself. The integrator must **not** re-implement the
allow/deny decision logic elsewhere (e.g. inline in the UI or in the worker) —
:func:`decide_turn_advice_dispatch` is the only place that logic may live.
Likewise, prompt text and provider request-body shape must only ever be
produced by :func:`build_turn_advice_prompt` / :func:`build_turn_provider_request_body`
— no other module should hand-assemble a Turn Advice prompt string or
provider body.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from maple_next.providers.turn_request import LegalAction, TurnAdviceRequest

_PROMPT_HEADER: Final[str] = (
    "You are advising a human Pokemon Champions player for the current turn.\n"
    "Use only the confirmed canonical facts and legal actions contained in this request.\n"
    "HP values are confirmed buckets, not exact percentages.\n"
    "UNKNOWN means unknown. Do not replace it with an assumed value.\n"
    "Do not invent moves, items, abilities, speed relations, damage ranges, opponent team "
    "members, weather, terrain, status, stat stages, or field effects.\n"
    "Compare every legal action before choosing.\n"
    "Choose exactly one recommended action from legal_actions. The action_id, action_type, "
    "and action_name must exactly match one legal action.\n"
    "A MOVE must belong to the current self active Pokemon. A SWITCH must target a listed "
    "legal switch target.\n"
    "Give short visible reasons, not hidden chain-of-thought. List relevant warnings.\n"
    "Predict the opponent's most likely action category conservatively. Use UNKNOWN when the "
    "confirmed facts do not support a stronger prediction.\n"
    "Respond with strict JSON only. Do not include markdown, code fences, commentary, or "
    "additional fields."
)


def _format_legal_action(action: LegalAction) -> str:
    if action.action_type.value == "MOVE":
        return (
            f"- id={action.action_id} type=MOVE name={action.action_name} "
            f"owner_active={action.owner_active}"
        )
    return (
        f"- id={action.action_id} type=SWITCH name={action.action_name} "
        f"switch_target={action.switch_target}"
    )


def build_turn_advice_prompt(request: TurnAdviceRequest) -> str:
    """Deterministic natural-language prompt for one Turn Advice request.

    Filled entirely from canonical request fields: reviewed facts (HP
    buckets, stat stages, status, weather, terrain, side effects), the
    applied selected three, every legal action, and the exact expected
    output JSON shape. No randomness, no clock, no network.
    """

    snapshot = request.reviewed_snapshot
    facts = snapshot.to_canonical_dict()
    legal_actions_text = "\n".join(_format_legal_action(a) for a in request.legal_actions)

    return (
        f"{_PROMPT_HEADER}\n\n"
        "Canonical reviewed facts:\n"
        f"self_active={facts['self_active']}\n"
        f"opponent_active={facts['opponent_active']}\n"
        f"self_hp={facts['self_hp']}\n"
        f"opponent_hp={facts['opponent_hp']}\n"
        f"self_status={facts['self_status']}\n"
        f"opponent_status={facts['opponent_status']}\n"
        f"self_stages={facts['self_stages']}\n"
        f"opponent_stages={facts['opponent_stages']}\n"
        f"weather={facts['weather']}\n"
        f"terrain={facts['terrain']}\n"
        f"self_side_effects={facts['self_side_effects']}\n"
        f"opponent_side_effects={facts['opponent_side_effects']}\n\n"
        f"Applied selected_three: {list(request.selected_three)}\n\n"
        "Legal actions:\n"
        f"{legal_actions_text}\n\n"
        "Respond with strict JSON only, matching exactly this shape, with no markdown, "
        "no code fence, and no additional commentary:\n"
        '{"recommended_action": {"action_id": "<id>", "action_type": "MOVE|SWITCH", '
        '"action_name": "<name>"}, "reasons": ["<reason>", "..."], '
        '"warnings": ["<warning>", "..."], "opponent_prediction": '
        '{"category": "MOVE|SWITCH|STATUS_OR_SETUP|UNKNOWN", '
        '"predicted_action": "<name-or-null>", "summary": "<summary>", '
        '"confidence": 0.0}}'
    )


def build_turn_provider_request_body(request: TurnAdviceRequest) -> dict[str, Any]:
    """Deterministic provider request body. No model name, endpoint, or secrets here."""

    return {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": build_turn_advice_prompt(request)}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": request.requested_output_schema,
        },
    }


class DispatchTrigger(StrEnum):
    """Every UI/system event that could conceivably precede a job creation."""

    TRUSTED_HUMAN_ACTIVATION = "TRUSTED_HUMAN_ACTIVATION"
    WINDOW_OPEN = "WINDOW_OPEN"
    RENDER = "RENDER"
    REFRESH = "REFRESH"
    POLL = "POLL"
    TIMER = "TIMER"
    STATE_CHANGE = "STATE_CHANGE"
    PROGRAMMATIC = "PROGRAMMATIC"
    RETRY = "RETRY"
    RESEND = "RESEND"
    FALLBACK = "FALLBACK"


#: The only allow reason this policy ever returns.
ALLOW_CREATE_ONE_JOB: Final[str] = "ALLOW_CREATE_ONE_JOB"

#: Stable deny reason codes.
DENY_TRIGGER_NOT_TRUSTED_HUMAN_ACTIVATION: Final[str] = (
    "DENY_TRIGGER_NOT_TRUSTED_HUMAN_ACTIVATION"
)
DENY_BINDING_NOT_CURRENT: Final[str] = "DENY_BINDING_NOT_CURRENT"
DENY_PENDING_JOB_EXISTS: Final[str] = "DENY_PENDING_JOB_EXISTS"
DENY_ATTEMPT_ALREADY_CONSUMED: Final[str] = "DENY_ATTEMPT_ALREADY_CONSUMED"


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """The outcome of :func:`decide_turn_advice_dispatch`. Immutable, pure data."""

    allowed: bool
    reason_code: str


def decide_turn_advice_dispatch(
    *,
    trigger: DispatchTrigger,
    is_current_binding: bool,
    has_pending_job: bool,
    attempt_consumed: bool,
) -> DispatchDecision:
    """Human-triggered-only Turn Advice dispatch policy. Pure decision function.

    Makes zero transport/worker/DB/UI/provider/game-action calls. Allows job
    creation only when every one of the following holds:

    - ``trigger`` is exactly :attr:`DispatchTrigger.TRUSTED_HUMAN_ACTIVATION`
      (every other trigger — including RETRY, RESEND, and FALLBACK — is
      denied unconditionally, regardless of the other three arguments);
    - ``is_current_binding`` is ``True`` (the reviewed binding the human
      acted on is still the session's current one);
    - ``has_pending_job`` is ``False`` (no QUEUED/IN_FLIGHT job already
      exists for that binding); and
    - ``attempt_consumed`` is ``False`` (this binding's one attempt has not
      already been used).
    """

    if trigger is not DispatchTrigger.TRUSTED_HUMAN_ACTIVATION:
        return DispatchDecision(
            allowed=False, reason_code=DENY_TRIGGER_NOT_TRUSTED_HUMAN_ACTIVATION
        )
    if not is_current_binding:
        return DispatchDecision(allowed=False, reason_code=DENY_BINDING_NOT_CURRENT)
    if has_pending_job:
        return DispatchDecision(allowed=False, reason_code=DENY_PENDING_JOB_EXISTS)
    if attempt_consumed:
        return DispatchDecision(allowed=False, reason_code=DENY_ATTEMPT_ALREADY_CONSUMED)
    return DispatchDecision(allowed=True, reason_code=ALLOW_CREATE_ONE_JOB)
