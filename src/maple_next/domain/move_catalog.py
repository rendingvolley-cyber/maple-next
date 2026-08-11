"""Offline move-name fuzzy matching for the operator autocomplete UI.

Ranking never auto-commits a value; callers are responsible for explicit-
selection semantics. This module contains no Qt import and performs no I/O
of its own -- it only ranks a caller-supplied list of canonical move names
against a caller-supplied query string.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_KATAKANA_START = 0x30A1
_KATAKANA_END = 0x30F6
_HIRAGANA_START = 0x3041

_FUZZY_MATCH_THRESHOLD = 0.55


def _katakana_to_hiragana(text: str) -> str:
    result: list[str] = []
    for character in text:
        code_point = ord(character)
        if _KATAKANA_START <= code_point <= _KATAKANA_END:
            result.append(chr(code_point - _KATAKANA_START + _HIRAGANA_START))
        else:
            result.append(character)
    return "".join(result)


def normalize_move_query(text: str) -> str:
    """Canonical comparison form: NFKC width-fold, kana-fold, casefold, trim."""

    normalized = unicodedata.normalize("NFKC", text.strip())
    normalized = _katakana_to_hiragana(normalized)
    return normalized.casefold()


@dataclass(frozen=True, slots=True)
class MoveCandidate:
    canonical_name: str
    normalized_name: str
    boost_score: float = 0.0


class MoveMatcher:
    """Ranks canonical move names against a free-text human query.

    Construction accepts any iterable of strings (in production, the
    ``canonical_name`` values from ``move_catalog.json``; in tests, any
    plain list of move names).
    """

    def __init__(self, canonical_names: list[str]) -> None:
        seen: dict[str, str] = {}
        for name in canonical_names:
            cleaned = name.strip()
            if not cleaned:
                continue
            normalized = normalize_move_query(cleaned)
            if normalized not in seen:
                seen[normalized] = cleaned
        self._entries: tuple[tuple[str, str], ...] = tuple(seen.items())

    def rank(
        self,
        query: str,
        *,
        boosts: dict[str, float] | None = None,
        limit: int = 8,
    ) -> list[MoveCandidate]:
        normalized_query = normalize_move_query(query)
        if not normalized_query:
            return []
        boost_map = boosts or {}

        tier1: list[tuple[float, str, str]] = []
        tier2: list[tuple[float, str, str]] = []
        tier3: list[tuple[float, str, str]] = []
        tier4: list[tuple[float, str, str]] = []

        for normalized_name, canonical_name in self._entries:
            boost = boost_map.get(canonical_name, 0.0)
            if normalized_name == normalized_query:
                tier1.append((boost, normalized_name, canonical_name))
            elif normalized_name.startswith(normalized_query):
                tier2.append((boost, normalized_name, canonical_name))
            elif normalized_query in normalized_name:
                tier3.append((boost, normalized_name, canonical_name))
            else:
                ratio = SequenceMatcher(None, normalized_query, normalized_name).ratio()
                if ratio >= _FUZZY_MATCH_THRESHOLD:
                    tier4.append((boost + ratio, normalized_name, canonical_name))

        tier1.sort(key=lambda item: (-item[0], item[2]))
        tier2.sort(key=lambda item: (-item[0], item[2]))
        tier3.sort(key=lambda item: (-item[0], item[2]))
        tier4.sort(key=lambda item: (-item[0], item[2]))

        ordered = tier1 + tier2 + tier3 + tier4
        results = [
            MoveCandidate(
                canonical_name=canonical_name,
                normalized_name=normalized_name,
                boost_score=boost_map.get(canonical_name, 0.0),
            )
            for boost, normalized_name, canonical_name in ordered
        ]
        return results[:limit]
