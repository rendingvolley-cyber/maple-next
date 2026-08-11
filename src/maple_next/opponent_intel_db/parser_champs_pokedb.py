"""Best-effort parser for champs.pokedb.tokyo (secondary usage-stats source).

This site returned HTTP 403 Forbidden when probed during development, which
is itself evidence this source is unreliable to depend on -- exactly why the
spec treats it as best-effort. Every function in this module is expected to
be wrapped by the caller (``cli.py``) in a broad try/except that logs a
warning and continues with primary-source-only data; nothing here should be
treated as fatal to the overall import.

The parsing approach mirrors ``parser_pokechamdb.py``'s generic
header-plus-percentage heuristic rather than assuming any specific markup,
since this site's actual HTML could not be inspected.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

SOURCE_NAME = "champs_pokedb_tokyo"

_BLOCK_TAGS = {"div", "li", "tr", "td", "th", "section", "table", "ul", "ol", "p", "br"}
_PERCENT_ENTRY_RE = re.compile(r"([^\d\n%]{1,40}?)\s*[:：]?\s*(\d{1,3}(?:\.\d+)?)\s*%")

_NATURES_HEADERS = ("性格", "せいかく", "Nature", "Natures")
_PARTNERS_HEADERS = ("同じチーム", "パートナー", "Partner", "Partners", "Teammates")


class ParseError(Exception):
    """Raised when a page cannot be parsed well enough to produce any usable data."""


class _BlockTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _extract_text(html: str) -> str:
    extractor = _BlockTextExtractor()
    extractor.feed(html)
    return extractor.text()


def _find_section(text: str, headers: tuple[str, ...]) -> str:
    best_position: int | None = None
    for header in headers:
        position = text.find(header)
        if position != -1 and (best_position is None or position < best_position):
            best_position = position
    if best_position is None:
        return ""
    return text[best_position:]


def _ranked_entries(section_text: str, *, limit: int = 30) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for match in _PERCENT_ENTRY_RE.finditer(section_text):
        raw_name, raw_percentage = match.groups()
        name = raw_name.strip(" \t　-:：・")
        if not name or name in seen_names:
            continue
        try:
            percentage = float(raw_percentage)
        except ValueError:
            continue
        seen_names.add(name)
        results.append({"name": name, "percentage": percentage})
        if len(results) >= limit:
            break
    return results


def parse_species_supplement(html: str, *, species_id: str) -> dict[str, Any]:
    """Best-effort extraction of natures/partners data to fill primary-source gaps.

    This secondary source is only ever used to backfill fields the primary
    source lacked for a species -- see ``cli.py``'s merge step. Raises
    :class:`ParseError` if nothing usable is found; callers must catch this
    (it is not a fatal error for the overall run).
    """

    text = _extract_text(html)
    natures = _ranked_entries(_find_section(text, _NATURES_HEADERS))
    partners = _ranked_entries(_find_section(text, _PARTNERS_HEADERS), limit=15)

    if not natures and not partners:
        raise ParseError(f"NO_USABLE_SUPPLEMENT_DATA_FOR_SPECIES:{species_id}")

    return {"natures": natures, "partners": partners}
