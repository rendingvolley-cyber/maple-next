from __future__ import annotations

from maple_next.providers.selection_request import (
    REQUESTED_OUTPUT_SCHEMA,
    SELECTION_PROMPT_VERSION,
    build_provider_prompt,
    build_selection_advice_request,
    canonical_request_dict,
    encode_canonical_request,
    request_payload_hash,
)

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")


def _build(**overrides: object) -> object:
    kwargs = {
        "session_id": "session-1",
        "match_id": "match-1",
        "generation": 3,
        "battle_revision": 2,
        "reviewed_selection_id": "reviewed-1",
        "self_team": SELF_TEAM,
        "opponent_team": OPPONENT_TEAM,
    }
    kwargs.update(overrides)
    return build_selection_advice_request(**kwargs)  # type: ignore[arg-type]


def test_canonical_dict_contains_only_required_minimum_fields() -> None:
    request = _build()
    canonical = canonical_request_dict(request)
    assert canonical["job_type"] == "SELECTION_ADVICE"
    assert canonical["session_id"] == "session-1"
    assert canonical["match_id"] == "match-1"
    assert canonical["generation"] == 3
    assert canonical["battle_revision"] == 2
    assert canonical["reviewed_selection_id"] == "reviewed-1"
    assert canonical["self_team"] == list(SELF_TEAM)
    assert canonical["opponent_team"] == list(OPPONENT_TEAM)
    assert canonical["requested_output_schema"] == REQUESTED_OUTPUT_SCHEMA


def test_canonical_dict_never_contains_secrets_or_timing() -> None:
    canonical = canonical_request_dict(_build())
    forbidden_keys = {
        "api_key",
        "authorization",
        "model",
        "timeout",
        "timeout_seconds",
        "human_authorized_at",
        "created_at",
    }
    assert forbidden_keys.isdisjoint(canonical.keys())


def test_self_team_and_opponent_team_preserve_exact_order() -> None:
    reordered_self = tuple(reversed(SELF_TEAM))
    canonical_original = canonical_request_dict(_build())
    canonical_reordered = canonical_request_dict(_build(self_team=reordered_self))
    assert canonical_original["self_team"] != canonical_reordered["self_team"]
    assert canonical_reordered["self_team"] == list(reordered_self)


def test_requested_output_schema_is_fixed_and_deterministic() -> None:
    first = canonical_request_dict(_build())["requested_output_schema"]
    second = canonical_request_dict(_build(session_id="different-session"))[
        "requested_output_schema"
    ]
    assert first == second == REQUESTED_OUTPUT_SCHEMA


def test_encoding_is_deterministic_regardless_of_python_dict_insertion_order() -> None:
    request = _build()
    first = encode_canonical_request(request)
    second = encode_canonical_request(request)
    assert first == second


def test_request_payload_hash_matches_independent_recomputation() -> None:
    request = _build()
    expected = request_payload_hash(request)
    recomputed_from_scratch = request_payload_hash(_build())
    assert expected == recomputed_from_scratch
    assert len(expected) == 64
    int(expected, 16)  # hexadecimal


def test_request_payload_hash_changes_when_team_order_changes() -> None:
    baseline = request_payload_hash(_build())
    reordered = request_payload_hash(_build(self_team=tuple(reversed(SELF_TEAM))))
    assert baseline != reordered


def test_request_payload_hash_changes_when_any_canonical_field_changes() -> None:
    baseline = request_payload_hash(_build())
    assert baseline != request_payload_hash(_build(battle_revision=99))
    assert baseline != request_payload_hash(_build(generation=99))
    assert baseline != request_payload_hash(_build(reviewed_selection_id="other"))
    assert baseline != request_payload_hash(_build(session_id="other-session"))
    assert baseline != request_payload_hash(_build(match_id="other-match"))


def test_tournament_selection_prompt_is_v2_and_requires_full_matchup_comparison() -> None:
    prompt = build_provider_prompt(_build())

    assert SELECTION_PROMPT_VERSION == "maple-selection-prompt.v2"
    assert "Compare all 20 distinct three-Pokémon combinations" in prompt
    assert "worst reasonable lead matchup" in prompt
    assert "weather setter + weather beneficiary" in prompt
    assert "fixed preset package" in prompt
    assert "one exact opponent" in prompt


def test_tournament_selection_prompt_preserves_opponent_uncertainty_boundary() -> None:
    prompt = build_provider_prompt(_build())

    assert "opponent's team contains names only" in prompt
    assert "unconfirmed opponent details must remain uncertainty" in prompt
    assert "do not assume the player's moves" in prompt
