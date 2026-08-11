"""Parser for pokechamdb.com usage-stats pages.

pokechamdb.com is a Japanese-language fan site with:

  * a ranking/list page at ``/?view=pokemon&format=single&season=<season>``
    listing every ranked species as a link to ``/pokemon/<slug>?season=...``
  * a per-species detail page at ``/pokemon/<slug>?season=...&format=...``
    with sections for moves (技), held items (持ち物), abilities
    (特性), natures (性格), teammates (同じチーム), and an EV-spread table
    (配分), each listing entries with a usage percentage.

The exact markup (class names, table layout) can change without notice and
was not something this module could fetch live and pin down byte-for-byte,
so parsing here is deliberately structural/heuristic: tags are stripped to
block-separated text, then named-entry-plus-percentage pairs are pulled out
of the text between one known section header and the next. This trades exact
markup coupling for resilience to minor site changes -- a missing or
malformed field just yields an empty tuple, and a genuinely unparsable
species is skipped by the caller, not the whole import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

SOURCE_NAME = "pokechamdb"

_BLOCK_TAGS = {
    "div",
    "li",
    "tr",
    "td",
    "th",
    "section",
    "table",
    "ul",
    "ol",
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "br",
}

_MOVES_HEADERS = ("技", "わざ", "Moves", "充分な技")
_ITEMS_HEADERS = ("持ち物", "もちもの", "Items")
_ABILITIES_HEADERS = ("特性", "とくせい", "Abilities")
_NATURES_HEADERS = ("性格", "せいかく", "Natures")
_PARTNERS_HEADERS = ("同じチーム", "パートナー", "Partners", "Teammates")
_SPREAD_HEADERS = ("配分", "努力値", "EV")

_ALL_HEADER_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("moves", _MOVES_HEADERS),
    ("items", _ITEMS_HEADERS),
    ("abilities", _ABILITIES_HEADERS),
    ("natures", _NATURES_HEADERS),
    ("partners", _PARTNERS_HEADERS),
    ("spreads", _SPREAD_HEADERS),
)

_PERCENT_ENTRY_RE = re.compile(r"([^\d\n%]{1,40}?)\s*[:：]?\s*(\d{1,3}(?:\.\d+)?)\s*%")
_SPECIES_LINK_RE = re.compile(
    r'href="(/pokemon/([a-z0-9\-]+)[^"]*)"[^>]*>\s*(?:<[^>]*>\s*)*([^<>\n]{1,60})'
)


class ParseError(Exception):
    """Raised when a page cannot be parsed well enough to produce any usable data."""


class _BlockTextExtractor(HTMLParser):
    """Strip tags to plain text, inserting newlines at block-element boundaries."""

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


@dataclass(frozen=True, slots=True)
class SpeciesListEntry:
    species_id: str
    display_name: str
    detail_path: str
    rank: int


def parse_species_list(html: str) -> list[SpeciesListEntry]:
    """Parse the ranking/list page into an ordered list of species stubs.

    Raises :class:`ParseError` only if literally zero species links can be
    found -- that indicates the page structure changed too much to trust any
    data from it, as opposed to one bad row.
    """

    entries: list[SpeciesListEntry] = []
    seen_slugs: set[str] = set()
    for match in _SPECIES_LINK_RE.finditer(html):
        detail_path, slug, display_name = match.groups()
        display_name = display_name.strip()
        if not slug or not display_name or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        entries.append(
            SpeciesListEntry(
                species_id=slug,
                display_name=display_name,
                detail_path=detail_path,
                rank=len(entries) + 1,
            )
        )

    if not entries:
        raise ParseError("NO_SPECIES_LINKS_FOUND_ON_LIST_PAGE")
    return entries


def _section_text(full_text: str, header_positions: list[tuple[int, str]], group_name: str) -> str:
    for index, (position, name) in enumerate(header_positions):
        if name != group_name:
            continue
        start = position
        has_next = index + 1 < len(header_positions)
        end = header_positions[index + 1][0] if has_next else len(full_text)
        return full_text[start:end]
    return ""


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


def parse_species_detail(
    html: str,
    *,
    species_id: str,
    display_name: str,
    season: str,
    format: str,
    source_url: str,
    fetched_at: str,
    ranking: float | None,
) -> dict[str, Any]:
    """Parse one species detail page into a plain dict matching the
    ``SpeciesStatsRecord`` JSON shape (see ``normalize.py``).

    Raises :class:`ParseError` if the page yields zero usable sections at
    all (moves/items/abilities/natures/partners all empty), since that
    means nothing worth recording was found. A page that yields *some*
    sections but not others is returned with the missing sections as empty
    lists -- callers should not treat that as fatal.
    """

    text = _extract_text(html)

    header_positions: list[tuple[int, str]] = []
    for group_name, headers in _ALL_HEADER_GROUPS:
        best_position: int | None = None
        for header in headers:
            position = text.find(header)
            if position != -1 and (best_position is None or position < best_position):
                best_position = position
        if best_position is not None:
            header_positions.append((best_position, group_name))
    header_positions.sort(key=lambda item: item[0])

    moves = _ranked_entries(_section_text(text, header_positions, "moves"))
    items = _ranked_entries(_section_text(text, header_positions, "items"))
    abilities = _ranked_entries(_section_text(text, header_positions, "abilities"))
    natures = _ranked_entries(_section_text(text, header_positions, "natures"))
    partners = _ranked_entries(_section_text(text, header_positions, "partners"), limit=15)

    if not any((moves, items, abilities, natures, partners)):
        raise ParseError(f"NO_USABLE_SECTIONS_FOUND_FOR_SPECIES:{species_id}")

    return {
        "species_id": species_id,
        "display_name": display_name,
        "season": season,
        "format": format,
        "source": SOURCE_NAME,
        "source_url": source_url,
        "source_updated_at": None,
        "fetched_at": fetched_at,
        "ranking": ranking,
        "moves": moves,
        "items": items,
        "abilities": abilities,
        "natures": natures,
        "partners": partners,
        "spreads": [],
    }
