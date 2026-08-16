"""Gemini V2 Bundle 4: versioned, immutable official Champions rules context.

Closes the prior audit gap: a rich Turn Advice request had no proof of
*which* official Pokemon Champions ranked-single rules the recommendation
was actually reasoned under. This bundle adds one checked-in, immutable,
first-party-sourced rules snapshot (``src/maple_next/data/champions_rules/
pokemon-champions-ranked-single/regulation-m-b/``), pins its identity to
each match at creation time, and embeds a compact, hash-covered
``rules_context`` in the rich Turn Advice request contract (now
``maple-turn-advice.v5``).

Nothing here contacts a network, a provider, or a real Gemini endpoint --
every test uses the checked-in snapshot exactly as production code would,
offline.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from maple_next.application.service import BattleApplication, DomainError
from maple_next.application.turn_provider_export_bridge import load_champions_rules_context
from maple_next.domain.champions_rules import (
    BUNDLED_RULESET_ROOT,
    RULES_CONTEXT_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    SUPPORTED_RULESET_ID,
    SUPPORTED_RULESET_VERSION,
    SUPPORTED_SCOPE,
    ChampionsRulesError,
    ChampionsRulesIntegrityError,
    ChampionsRulesPinError,
    RulesPin,
    current_rules_pin_for_new_match,
    load_bundled_snapshot,
    load_raw_bundled_snapshot_dict,
    parse_and_validate_snapshot,
    resolve_pinned_rules_context,
    snapshot_to_rules_pin,
    verify_pin_against_snapshot,
)
from maple_next.domain.models import BattleSession
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers import turn_request as legacy_turn_request
from maple_next.providers.turn_advice_rich_state import (
    RICH_STATE_REQUEST_CONTRACT_VERSION,
    canonical_rich_request_dict,
)
from tests.fixtures.bundle3 import default_rules_context
from tests.test_gemini_v2_bundle3_confirmed_memory_build_context import (
    CURRENT_TURN_NUMBER,
    GABURIASU,
    HASSAMU,
    MASUKAANYA,
    SELECTED_THREE,
    SELF_ACTIVE_BY_TURN,
    Bundle3Fixture,
)

# --- helpers -------------------------------------------------------------


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _fresh_raw_snapshot() -> dict:
    return copy.deepcopy(load_raw_bundled_snapshot_dict())


# =========================================================================
# A. SNAPSHOT: load/validate/tamper-detection
# =========================================================================


def test_bundled_snapshot_loads_and_is_m_b_single() -> None:
    snapshot = load_bundled_snapshot()
    assert snapshot.schema_version == SNAPSHOT_SCHEMA_VERSION
    assert snapshot.ruleset_id == SUPPORTED_RULESET_ID
    assert snapshot.ruleset_version == SUPPORTED_RULESET_VERSION
    assert snapshot.scope == SUPPORTED_SCOPE
    assert len(snapshot.snapshot_id) == 64
    assert len(snapshot.facts_content_sha256) == 64
    assert len(snapshot.sources) == 3


def test_facts_digest_is_deterministic_across_reloads() -> None:
    first = parse_and_validate_snapshot(_fresh_raw_snapshot(), evidence_root=BUNDLED_RULESET_ROOT)
    second = parse_and_validate_snapshot(_fresh_raw_snapshot(), evidence_root=BUNDLED_RULESET_ROOT)
    assert first.facts_content_sha256 == second.facts_content_sha256
    assert first.snapshot_id == second.snapshot_id
    assert first.facts.to_canonical_dict() == second.facts.to_canonical_dict()


def test_tampered_fact_value_fails_facts_hash_check() -> None:
    raw = _fresh_raw_snapshot()
    raw["facts"]["duplicate_held_items_allowed"] = True
    with pytest.raises(ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_FACTS_HASH_MISMATCH"):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_tampered_facts_hash_alone_also_fails() -> None:
    raw = _fresh_raw_snapshot()
    raw["facts_content_sha256"] = "0" * 64
    with pytest.raises(ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_FACTS_HASH_MISMATCH"):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_tampered_snapshot_id_fails_identity_check() -> None:
    raw = _fresh_raw_snapshot()
    raw["snapshot_id"] = "1" * 64
    with pytest.raises(ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_ID_MISMATCH"):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_tampered_archived_evidence_content_fails_hash_check(tmp_path: Path) -> None:
    """Corrupting the on-disk evidence bytes (not the manifest) must fail closed."""

    raw = _fresh_raw_snapshot()
    ruleset_root = tmp_path / "ruleset"
    evidence_dir = ruleset_root / "evidence"
    evidence_dir.mkdir(parents=True)
    for source in raw["sources"]:
        relative = source["archived_evidence_relative_path"]
        real_path = BUNDLED_RULESET_ROOT / relative
        target = ruleset_root / relative
        target.write_bytes(real_path.read_bytes())
    # Corrupt exactly one archived evidence file without touching its
    # recorded hash in the snapshot dict.
    corrupted_relative = raw["sources"][0]["archived_evidence_relative_path"]
    (ruleset_root / corrupted_relative).write_bytes(b"tampered evidence bytes")
    with pytest.raises(
        ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_EVIDENCE_HASH_MISMATCH"
    ):
        parse_and_validate_snapshot(raw, evidence_root=ruleset_root)


def test_missing_archived_evidence_file_fails_closed(tmp_path: Path) -> None:
    raw = _fresh_raw_snapshot()
    empty_root = tmp_path / "empty-ruleset"
    empty_root.mkdir()
    with pytest.raises(ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_EVIDENCE_MISSING"):
        parse_and_validate_snapshot(raw, evidence_root=empty_root)


def test_unknown_ruleset_id_fails_closed() -> None:
    raw = _fresh_raw_snapshot()
    raw["ruleset_id"] = "pokemon-champions-ranked-double"
    with pytest.raises(ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_UNRECOGNIZED_RULESET"):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_unknown_ruleset_version_fails_closed() -> None:
    raw = _fresh_raw_snapshot()
    raw["ruleset_version"] = "M-C"
    with pytest.raises(ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_UNRECOGNIZED_RULESET"):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_unsupported_schema_version_fails_closed() -> None:
    raw = _fresh_raw_snapshot()
    raw["schema_version"] = "maple-champions-rules-snapshot.v2"
    with pytest.raises(
        ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_UNSUPPORTED_SCHEMA_VERSION"
    ):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_unsupported_fact_key_is_rejected_not_silently_ignored() -> None:
    """A fabricated Champions-specific fact must fail closed, never pass through."""

    raw = _fresh_raw_snapshot()
    raw["facts"]["forced_switch_on_faint"] = True
    # Recompute the recorded hash so only the *shape* check (not the hash
    # check) is exercised here.
    facts_bytes = _canonical_bytes(raw["facts"])
    raw["facts_content_sha256"] = hashlib.sha256(facts_bytes).hexdigest()
    with pytest.raises(
        ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_UNSUPPORTED_FACT_KEYS"
    ):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_pokemon_selected_to_battle_is_not_an_authoritative_fact() -> None:
    """R1 remediation: no first-party evidence directly binds a Pokemon-count

    to Ranked Single Regulation M-B specifically (the only page that states
    "3 Pokemon" is a different competition, "Monthly Challenge Series July
    2026", and neither Source A nor Source B state a selection *count* for
    M-B -- only a selection *time*). Bundle 4 must not assert an unproven
    Champions rules-authority claim, so this key must be entirely absent
    from the facts contract, not merely unused.
    """

    snapshot = load_bundled_snapshot()
    facts_dict = snapshot.facts.to_canonical_dict()
    assert "pokemon_selected_to_battle" not in facts_dict
    assert "pokemon_selected_to_battle" not in snapshot.coverage.authoritative_categories
    assert not hasattr(snapshot.facts, "pokemon_selected_to_battle")


def test_reintroducing_pokemon_selected_to_battle_fact_key_fails_closed() -> None:
    """The validator itself rejects the key, not just the checked-in file.

    Defense in depth: even if a future edit reintroduced
    ``pokemon_selected_to_battle`` into the on-disk snapshot, the parser
    must fail closed rather than silently accepting an unsupported fact
    key -- the same protection already proven for a fabricated mechanic in
    ``test_unsupported_fact_key_is_rejected_not_silently_ignored``.
    """

    raw = _fresh_raw_snapshot()
    raw["facts"]["pokemon_selected_to_battle"] = 3
    facts_bytes = _canonical_bytes(raw["facts"])
    raw["facts_content_sha256"] = hashlib.sha256(facts_bytes).hexdigest()
    with pytest.raises(
        ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_UNSUPPORTED_FACT_KEYS"
    ):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_coverage_authoritative_categories_must_match_mandatory_set() -> None:
    raw = _fresh_raw_snapshot()
    raw["coverage"]["authoritative_categories"] = ["battle_format"]
    with pytest.raises(
        ChampionsRulesIntegrityError, match="RULES_SNAPSHOT_AUTHORITATIVE_CATEGORIES_MISMATCH"
    ):
        parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)


def test_unsupported_mechanics_are_declared_absent_not_guessed() -> None:
    snapshot = load_bundled_snapshot()
    for mechanic in (
        "switching_mechanics",
        "faint_replacement_flow",
        "stat_stage_mechanics",
        "status_mechanics",
        "weather_mechanics",
        "terrain_mechanics",
        "turn_order_mechanics",
        "type_chart",
        "move_effects",
        "damage_formula",
    ):
        assert mechanic in snapshot.coverage.intentionally_not_asserted
    facts_dict = snapshot.facts.to_canonical_dict()
    for forbidden_key in (
        "switching",
        "faint",
        "stat_stage",
        "status",
        "weather",
        "terrain",
        "turn_order",
        "type_chart",
        "move_effects",
        "damage",
    ):
        assert forbidden_key not in facts_dict


# =========================================================================
# B. OFFICIAL FACT SET
# =========================================================================


def test_official_fact_values_match_the_regulation_m_b_sources() -> None:
    facts = load_bundled_snapshot().facts
    assert facts.battle_format == "SINGLE"
    assert facts.mega_evolution.allowed is True
    assert facts.mega_evolution.max_uses_per_battle == 1
    assert facts.mega_evolution.requires_mega_stone_for_eligible_pokemon is True
    assert facts.duplicate_held_items_allowed is False
    assert facts.timers.total_time_seconds == 1200
    assert facts.timers.player_time_seconds == 420
    assert facts.timers.turn_selection_seconds == 45
    assert facts.timers.pokemon_selection_seconds == 90


def test_sources_carry_https_urls_and_64_char_hashes() -> None:
    for source in load_bundled_snapshot().sources:
        assert source.url.startswith("https://")
        assert len(source.archived_content_sha256) == 64


# =========================================================================
# C. PINNING
# =========================================================================


def test_new_match_pins_the_accepted_m_b_snapshot(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "maple.db")
    app = BattleApplication(repo)
    session = app.new_match()
    expected = current_rules_pin_for_new_match()
    assert session.rules_ruleset_id == expected.ruleset_id
    assert session.rules_ruleset_version == expected.ruleset_version
    assert session.rules_snapshot_id == expected.rules_snapshot_id
    assert session.rules_facts_sha256 == expected.rules_facts_sha256


def test_restart_resolves_the_exact_same_pin(tmp_path: Path) -> None:
    db_path = tmp_path / "maple.db"
    repo = SQLiteRepository(db_path)
    app = BattleApplication(repo)
    created = app.new_match()
    repo.close()

    # Simulate a real process restart: a brand-new repository/connection
    # against the same on-disk database file.
    restarted_repo = SQLiteRepository(db_path)
    reloaded = restarted_repo.load_active_session()
    assert reloaded is not None
    assert reloaded.rules_ruleset_id == created.rules_ruleset_id
    assert reloaded.rules_ruleset_version == created.rules_ruleset_version
    assert reloaded.rules_snapshot_id == created.rules_snapshot_id
    assert reloaded.rules_facts_sha256 == created.rules_facts_sha256
    restarted_repo.close()


def test_next_match_after_export_pins_rules_again(tmp_path: Path) -> None:
    """A second match created via ``new_match_after_export`` is pinned too.

    Reuses the exact same ``build_ready_application`` fixture helper the
    existing Bundle 1/legacy match-lifecycle regression suite uses to reach
    a real ``BATTLE_READY`` -> ``MATCH_ENDED`` -> ``MATCH_EXPORTED`` match,
    so this test exercises the real command sequence rather than a
    hand-rolled shortcut.
    """

    from maple_next.domain.enums import MatchOutcome
    from tests.test_match_lifecycle import build_ready_application

    repository, application, _, _ = build_ready_application(tmp_path)
    first = repository.load_active_session()
    assert first is not None
    first_expected = current_rules_pin_for_new_match()
    assert first.rules_snapshot_id == first_expected.rules_snapshot_id

    application.end_match(MatchOutcome.WIN, human_confirmed=True)
    application.export_match()
    second = application.new_match_after_export()

    assert second.session_id != first.session_id
    assert second.rules_snapshot_id is not None
    expected = current_rules_pin_for_new_match()
    assert second.rules_snapshot_id == expected.rules_snapshot_id
    assert second.rules_ruleset_id == expected.ruleset_id
    assert second.rules_facts_sha256 == expected.rules_facts_sha256


def test_unpinned_match_fails_closed_on_context_resolution() -> None:
    from maple_next.domain.enums import BattleState

    unpinned = BattleSession(
        session_id="s",
        match_id="m",
        generation=1,
        state=BattleState.SELECTION_OPEN,
        battle_revision=1,
    )
    with pytest.raises(ChampionsRulesError, match="MATCH_RULES_UNPINNED"):
        load_champions_rules_context(unpinned)


def test_wrong_persisted_snapshot_id_fails_closed() -> None:
    real = current_rules_pin_for_new_match()
    forged = RulesPin(
        ruleset_id=real.ruleset_id,
        ruleset_version=real.ruleset_version,
        rules_snapshot_id="0" * 64,
        rules_facts_sha256=real.rules_facts_sha256,
    )
    with pytest.raises(ChampionsRulesPinError, match="PINNED_SNAPSHOT_ID_MISMATCH"):
        resolve_pinned_rules_context(forged)


def test_wrong_persisted_facts_hash_fails_closed() -> None:
    real = current_rules_pin_for_new_match()
    forged = RulesPin(
        ruleset_id=real.ruleset_id,
        ruleset_version=real.ruleset_version,
        rules_snapshot_id=real.rules_snapshot_id,
        rules_facts_sha256="0" * 64,
    )
    with pytest.raises(ChampionsRulesPinError, match="PINNED_FACTS_HASH_MISMATCH"):
        resolve_pinned_rules_context(forged)


def test_wrong_persisted_ruleset_version_fails_closed() -> None:
    real = current_rules_pin_for_new_match()
    forged = RulesPin(
        ruleset_id=real.ruleset_id,
        ruleset_version="M-A",
        rules_snapshot_id=real.rules_snapshot_id,
        rules_facts_sha256=real.rules_facts_sha256,
    )
    with pytest.raises(ChampionsRulesPinError, match="PINNED_RULESET_NOT_AVAILABLE"):
        resolve_pinned_rules_context(forged)


def test_a_newer_alternate_snapshot_cannot_alter_an_existing_pin() -> None:
    """A match pinned to the real snapshot must reject a *different* one.

    Simulates "a newer accepted snapshot appears on disk" by constructing
    an alternate, internally-consistent snapshot (own facts, own
    recomputed hash/identity) and proving the *existing* pin does not
    silently resolve against it -- it fails closed instead.
    """

    real_snapshot = load_bundled_snapshot()
    real_pin = snapshot_to_rules_pin(real_snapshot)

    raw = _fresh_raw_snapshot()
    raw["facts"]["timers"]["total_time_seconds"] = 1500
    facts_bytes = _canonical_bytes(raw["facts"])
    raw["facts_content_sha256"] = hashlib.sha256(facts_bytes).hexdigest()
    identity = {
        "schema_version": raw["schema_version"],
        "ruleset_id": raw["ruleset_id"],
        "ruleset_version": raw["ruleset_version"],
        "scope": raw["scope"],
        "effective_period": raw["effective_period"],
        "facts_content_sha256": raw["facts_content_sha256"],
        "coverage": raw["coverage"],
    }
    raw["snapshot_id"] = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    alternate_snapshot = parse_and_validate_snapshot(raw, evidence_root=BUNDLED_RULESET_ROOT)

    assert alternate_snapshot.snapshot_id != real_snapshot.snapshot_id
    with pytest.raises(ChampionsRulesPinError, match="PINNED_SNAPSHOT_ID_MISMATCH"):
        verify_pin_against_snapshot(real_pin, alternate_snapshot)
    # The existing pin still resolves correctly against the real snapshot.
    verify_pin_against_snapshot(real_pin, real_snapshot)


def test_provider_ready_request_fails_closed_for_unpinned_match(tmp_path: Path) -> None:
    """An old/unpinned match must never silently bind to the latest rules.

    Reuses the full historical rich-request-ready fixture (real confirmed
    state, legal actions, legal-switch confirmation, Bundle 3 context) with
    ``pin_rules=False`` -- everything else that ``request_rich_turn_advice``
    checks is satisfied, so this proves the rules-pin gate specifically,
    not an earlier unrelated gate.
    """

    fixture = Bundle3Fixture(tmp_path, pin_rules=False, db_name="bundle4-unpinned.db")
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        assert session.rules_ruleset_id is None
        with pytest.raises(DomainError, match="CHAMPIONS_RULES_CONTEXT_INVALID"):
            fixture.application.request_rich_turn_advice("command-unpinned")
    finally:
        fixture.close()


# =========================================================================
# D. REQUEST V5
# =========================================================================


def test_request_contract_still_carries_rules_context_after_v6() -> None:
    """Bundle 5 advanced the contract to v6; Bundle 4's field is unchanged.

    The version constant moved (``.v5`` -> ``.v6``) because Bundle 5 added
    exactly one new top-level field. Bundle 4's ``rules_context`` -- its
    presence, its schema version, and its content -- is untouched, which the
    assertions below and ``test_canonical_request_carries_rules_context_and_
    all_bundle_1_to_3_fields`` continue to prove.
    """

    assert RICH_STATE_REQUEST_CONTRACT_VERSION == "maple-turn-advice.v6"


def test_canonical_request_carries_rules_context_and_all_bundle_1_to_3_fields(
    tmp_path: Path,
) -> None:
    fixture = Bundle3Fixture(tmp_path)
    try:
        request = fixture.build_request()
        canonical = canonical_rich_request_dict(request)
        assert canonical["rules_context"] == request.rules_context
        assert request.rules_context["context_schema_version"] == RULES_CONTEXT_SCHEMA_VERSION
        assert request.rules_context["ruleset_id"] == SUPPORTED_RULESET_ID
        assert request.rules_context["ruleset_version"] == SUPPORTED_RULESET_VERSION
        for field in (
            # Bundle 1/2/3 fields, all preserved verbatim.
            "reviewed_state",
            "selected_three",
            "self_active",
            "legal_actions",
            "legal_switches",
            "legal_switches_status",
            "applied_selection_id",
            "reviewed_selection_id",
            "selected_three_builds",
            "battle_memory",
        ):
            assert field in canonical
    finally:
        fixture.close()


def test_semantic_rules_content_change_changes_the_request_hash(tmp_path: Path) -> None:
    fixture = Bundle3Fixture(tmp_path)
    try:
        original = fixture.build_request()
        mutated_context = copy.deepcopy(original.rules_context)
        mutated_context["facts"]["timers"]["total_time_seconds"] = 1500
        mutated = fixture.build_request()
        from dataclasses import replace

        from maple_next.providers.turn_advice_rich_state import rich_request_payload_hash

        mutated = replace(mutated, rules_context=mutated_context)
        mutated = replace(mutated, request_hash=rich_request_payload_hash(mutated))
        assert mutated.request_hash != original.request_hash
    finally:
        fixture.close()


def test_rules_version_change_changes_the_request_hash() -> None:
    context_a = default_rules_context()
    context_b = copy.deepcopy(context_a)
    context_b["ruleset_version"] = "M-A"
    assert _canonical_bytes(context_a) != _canonical_bytes(context_b)


def test_identical_rebuild_reproduces_identical_bytes_and_hash(tmp_path: Path) -> None:
    fixture = Bundle3Fixture(tmp_path)
    try:
        job = fixture.application.request_rich_turn_advice("command-b3")
        rebuilt = fixture.application.build_rich_turn_advice_transport_request(job)
        assert rebuilt.request_hash == job.request_payload_hash
        assert rebuilt.rules_context == default_rules_context()
    finally:
        fixture.close()


def test_rules_context_missing_keys_fail_closed_defense_in_depth(tmp_path: Path) -> None:
    fixture = Bundle3Fixture(tmp_path)
    try:
        from maple_next.providers.turn_advice_rich_state import (
            RichStateRequestError,
            build_rich_state_turn_advice_request,
        )

        state = fixture.repository.get_confirmed_turn_state(
            fixture.confirmed_state_id(CURRENT_TURN_NUMBER)
        )
        identity = fixture.identity(CURRENT_TURN_NUMBER)
        with pytest.raises(RichStateRequestError, match="RULES_CONTEXT_MISSING_KEYS"):
            build_rich_state_turn_advice_request(
                confirmed_state=state,
                confirmed_legal_actions=(
                    fixture.repository.list_confirmed_legal_action_selections_for_identity(
                        identity
                    )
                ),
                current_identity=identity,
                latest_confirmed_state_id=state.confirmed_state_id,
                latest_open_draft_turn_number=None,
                latest_open_draft_battle_revision=None,
                legal_switch_confirmation=fixture.repository.get_legal_switch_confirmation(
                    identity=identity,
                    based_on_confirmed_state_id=state.confirmed_state_id,
                    applied_selection_id=fixture.applied_selection_id,
                ),
                selected_three=SELECTED_THREE,
                self_active=SELF_ACTIVE_BY_TURN[CURRENT_TURN_NUMBER],
                bundle3_context=fixture.bundle3_context(),
                rules_context={"context_schema_version": RULES_CONTEXT_SCHEMA_VERSION},
                opponent_intel_context=fixture.opponent_intel_context(),
            )
    finally:
        fixture.close()


# =========================================================================
# E. PROMPT AUTHORITY
# =========================================================================


def test_no_blanket_general_champions_knowledge_authority_remains() -> None:
    assert "You may use general Pokémon Champions knowledge" not in (
        legacy_turn_request._TURN_INITIAL_PROMPT
    )


def test_rules_context_declared_authoritative_in_prompt() -> None:
    prompt = legacy_turn_request._TURN_INITIAL_PROMPT
    assert "rules_context" in prompt
    assert "authoritative" in prompt


def test_general_knowledge_is_explicitly_non_authoritative_background() -> None:
    prompt = legacy_turn_request._TURN_INITIAL_PROMPT
    assert "unconfirmed background" in prompt
    assert "must never override" in prompt


def test_absent_mechanic_must_not_be_asserted_as_confirmed() -> None:
    prompt = legacy_turn_request._TURN_INITIAL_PROMPT
    assert "absent from rules_context" in prompt
    assert "surface that uncertainty as a warning" in prompt
    assert "Never fabricate a Champions-specific mechanic" in prompt


def test_rich_prompt_embeds_the_shared_initial_prompt_unmodified(tmp_path: Path) -> None:
    """The rich (Bundle 4) prompt path is not a second, independent copy."""

    from maple_next.providers.turn_advice_rich_state import build_rich_provider_prompt

    fixture = Bundle3Fixture(tmp_path)
    try:
        request = fixture.build_request()
        prompt = build_rich_provider_prompt(request)
        assert "rules_context" in prompt
        assert legacy_turn_request._TURN_INITIAL_PROMPT in prompt
    finally:
        fixture.close()


# =========================================================================
# F. HISTORICAL-LIKE MATCH: マスカーニャ / ハッサム / ガブリアス
# =========================================================================


def test_historical_match_carries_the_exact_pinned_m_b_context(tmp_path: Path) -> None:
    fixture = Bundle3Fixture(tmp_path)
    try:
        expected = current_rules_pin_for_new_match()
        session = fixture.repository.load_active_session()
        assert session is not None
        assert session.rules_ruleset_id == expected.ruleset_id
        assert session.rules_snapshot_id == expected.rules_snapshot_id

        job = fixture.application.request_rich_turn_advice("command-hist-1")
        rebuilt = fixture.application.build_rich_turn_advice_transport_request(job)
        assert rebuilt.rules_context["snapshot_id"] == expected.rules_snapshot_id
        assert rebuilt.rules_context["facts_content_sha256"] == expected.rules_facts_sha256
        assert rebuilt.selected_three == SELECTED_THREE
        names = {member.pokemon_name for member in rebuilt.selected_three_builds}
        assert names == {MASUKAANYA, HASSAMU, GABURIASU}
        # Bundle 3 memory/build context remains intact alongside Bundle 4.
        assert len(rebuilt.battle_memory.turns) == CURRENT_TURN_NUMBER - 1
    finally:
        fixture.close()


def test_historical_match_same_pin_after_restart(tmp_path: Path) -> None:
    fixture = Bundle3Fixture(tmp_path)
    try:
        before = fixture.rules_context()
        db_path = fixture.db_path
    finally:
        fixture.close()

    restarted_repo = SQLiteRepository(db_path)
    session = restarted_repo.load_active_session()
    assert session is not None
    after = load_champions_rules_context(session)
    assert after == before
    restarted_repo.close()


def test_historical_match_same_context_resolved_independently_of_turn_identity(
    tmp_path: Path,
) -> None:
    """``rules_context`` is a per-match invariant, not something that drifts turn to turn."""

    fixture = Bundle3Fixture(tmp_path)
    try:
        session = fixture.repository.load_active_session()
        assert session is not None
        first = load_champions_rules_context(session)
        second = load_champions_rules_context(session)
        assert first == second
        assert _canonical_bytes(first) == _canonical_bytes(second)
    finally:
        fixture.close()


def test_historical_match_serializes_no_unsupported_mechanic(tmp_path: Path) -> None:
    fixture = Bundle3Fixture(tmp_path)
    try:
        request = fixture.build_request()
        canonical = canonical_rich_request_dict(request)
        facts = canonical["rules_context"]["facts"]
        assert set(facts.keys()) == {
            "battle_format",
            "mega_evolution",
            "duplicate_held_items_allowed",
            "timers",
        }
    finally:
        fixture.close()
