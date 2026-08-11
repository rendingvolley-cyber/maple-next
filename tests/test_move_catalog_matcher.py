"""Pure-Python ranking tests for maple_next.domain.move_catalog."""

from __future__ import annotations

from maple_next.domain.move_catalog import MoveCandidate, MoveMatcher, normalize_move_query

_MOVES = [
    "じしん",
    "じならし",
    "げきりん",
    "だいちのちから",
    "10まんばりき",
    "Earthquake",
    "Protect",
]


def test_empty_query_returns_no_candidates() -> None:
    matcher = MoveMatcher(_MOVES)
    assert matcher.rank("") == []
    assert matcher.rank("   ") == []


def test_exact_match_ranks_before_prefix_substring_and_fuzzy() -> None:
    matcher = MoveMatcher(["じしん", "じしんつよし", "つよいじしん", "じわれ"])
    results = matcher.rank("じしん")
    names = [candidate.canonical_name for candidate in results]
    assert names[0] == "じしん"
    assert names.index("じしんつよし") < names.index("つよいじしん")


def test_prefix_beats_substring() -> None:
    matcher = MoveMatcher(["あいうえお", "かきあいうえお"])
    results = matcher.rank("あい")
    names = [candidate.canonical_name for candidate in results]
    assert names == ["あいうえお", "かきあいうえお"]


def test_fuzzy_match_only_appears_above_threshold() -> None:
    matcher = MoveMatcher(["Earthquake", "Protect", "Toxic"])
    results = matcher.rank("Earthquack")
    names = [candidate.canonical_name for candidate in results]
    assert "Earthquake" in names
    # "Toxic" shares almost nothing with the query and must not fuzzy-match.
    assert "Toxic" not in names


def test_katakana_query_matches_hiragana_catalog_entry() -> None:
    matcher = MoveMatcher(["じしん"])
    results = matcher.rank("ジシン")
    assert [candidate.canonical_name for candidate in results] == ["じしん"]


def test_full_and_half_width_normalization() -> None:
    matcher = MoveMatcher(["10まんばりき"])
    results = matcher.rank("１０まんばりき")
    assert [candidate.canonical_name for candidate in results] == ["10まんばりき"]


def test_boost_reorders_within_tier_but_never_crosses_tiers() -> None:
    # Both "じしん" and "じしんつよし" match query "じしん" -- one exact
    # (tier 1), one prefix (tier 2). A huge boost on the prefix match must
    # never let it outrank the exact match.
    matcher = MoveMatcher(["じしん", "じしんつよし"])
    results = matcher.rank("じしん", boosts={"じしんつよし": 1000.0})
    assert results[0].canonical_name == "じしん"

    # Within the same tier (two prefix matches), boost does reorder.
    matcher2 = MoveMatcher(["じしんA", "じしんB"])
    results2 = matcher2.rank("じしん", boosts={"じしんB": 50.0})
    assert results2[0].canonical_name == "じしんB"


def test_boost_never_makes_a_non_matching_name_appear() -> None:
    matcher = MoveMatcher(["Earthquake", "Toxic"])
    results = matcher.rank("Earthquake", boosts={"Toxic": 100000.0})
    names = [candidate.canonical_name for candidate in results]
    assert "Toxic" not in names


def test_normalize_move_query_folds_width_kana_and_case() -> None:
    assert normalize_move_query("  ジシン ") == normalize_move_query("じしん")
    assert normalize_move_query("ＥＡＲＴＨ") == normalize_move_query("earth")


def test_move_candidate_is_frozen_dataclass() -> None:
    candidate = MoveCandidate(canonical_name="じしん", normalized_name="じしん")
    assert candidate.boost_score == 0.0
