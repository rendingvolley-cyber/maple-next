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
from urllib.parse import urljoin

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
# Each ranked-list anchor nests a rank-number span *before* the name span,
# e.g. ``<a href="/pokemon/abomasnow?..."><span>118</span><img .../>
# <span>アボマスノキ</span></a>`` -- so the anchor's whole inner HTML is
# captured (non-greedy, up to the closing tag) and every text node inside it
# is inspected below, skipping the leading purely-numeric rank token rather
# than naively taking the first text run.
_SPECIES_ANCHOR_RE = re.compile(
    r'href="(/pokemon/([a-z0-9\-]+)[^"]*)"[^>]*>(.*?)</a>', re.DOTALL
)
_TEXT_NODE_RE = re.compile(r">([^<>]+)<")


def _display_name_from_anchor_html(inner_html: str) -> str:
    """First non-blank, non-purely-numeric text node inside a species anchor.

    Skips the rank-number span (and any other blank/numeric-only nodes, e.g.
    a screen-reader-only counter) and returns the first remaining text node,
    which is the actual species display name.
    """

    for raw_text in _TEXT_NODE_RE.findall(f">{inner_html}<"):
        text: str = str(raw_text).strip()
        if not text or text.isdigit():
            continue
        return text
    return ""


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
    #: This module's own enumeration order within one page (1-based). This
    #: is *not* discovery-completeness evidence on its own -- a truncated
    #: subset of entries still enumerates as a contiguous 1..N sequence.
    #: See ``site_rank`` for the source's own displayed rank number instead.
    rank: int
    #: The rank number the source page itself displays next to this entry
    #: (e.g. the "118" in pokechamdb's ranked list), when one could be
    #: extracted. ``None`` when the page has no such per-entry rank marker.
    site_rank: int | None = None


def _site_rank_from_anchor_html(inner_html: str) -> int | None:
    """The first purely-numeric text node inside a species anchor, if any.

    This is the inverse of :func:`_display_name_from_anchor_html`: that
    function skips the leading numeric rank span to find the name; this one
    extracts that same rank span as the source's own declared rank number,
    used later as discovery-completeness evidence (rank sequence
    consistency) rather than being discarded.
    """

    for raw_text in _TEXT_NODE_RE.findall(f">{inner_html}<"):
        text = str(raw_text).strip()
        if text.isdigit():
            return int(text)
    return None


def parse_species_list(html: str) -> list[SpeciesListEntry]:
    """Parse one ranking/list page into an ordered list of species stubs.

    Raises :class:`ParseError` only if literally zero species links can be
    found -- that indicates the page structure changed too much to trust any
    data from it, as opposed to one bad row. This parses exactly one page's
    worth of anchors; it makes no claim about whether that page is the only
    page or the last page of a paginated listing -- see ``discovery.py`` for
    the completeness proof that answers that question.
    """

    entries: list[SpeciesListEntry] = []
    seen_slugs: set[str] = set()
    for match in _SPECIES_ANCHOR_RE.finditer(html):
        detail_path, slug, anchor_inner_html = match.groups()
        display_name = _display_name_from_anchor_html(anchor_inner_html)
        if not slug or not display_name or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        entries.append(
            SpeciesListEntry(
                species_id=slug,
                display_name=display_name,
                detail_path=detail_path,
                rank=len(entries) + 1,
                site_rank=_site_rank_from_anchor_html(anchor_inner_html),
            )
        )

    if not entries:
        raise ParseError("NO_SPECIES_LINKS_FOUND_ON_LIST_PAGE")
    return entries


#: A response body is well-formed only if it ends with a proper closing
#: ``</html>`` tag -- a response truncated mid-transfer (network/proxy
#: cutoff) would not, and must not be silently treated as "the complete
#: page".
_WELL_FORMED_TERMINATION_RE = re.compile(r"</html\s*>\s*\Z", re.IGNORECASE)

#: A "next page" link: either an explicit ``rel="next"`` anchor (attribute
#: order-independent) or an anchor whose visible text is a recognizable
#: "next page" label. Deliberately conservative/heuristic, matching this
#: module's existing structural-parsing philosophy -- a source that doesn't
#: paginate (pokechamdb.com today) simply never matches this, in which case
#: ``extract_next_page_url`` returns ``None``.
_NEXT_LINK_REL_RE = re.compile(
    r'<a\b(?=[^>]*\brel=["\']next["\'])(?=[^>]*\bhref=["\']([^"\']+)["\'])[^>]*>',
    re.IGNORECASE,
)
_NEXT_LINK_TEXT_RE = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>\s*(?:次へ|次のページ|Next|»)\s*</a>',
    re.IGNORECASE,
)

#: An explicit source-advertised total species count, if the page carries
#: one (pokechamdb.com today does not -- verified live -- but the check
#: stays generic/forward-compatible per the "use source-native evidence
#: where available" requirement).
_ADVERTISED_TOTAL_RE = re.compile(
    r'data-total-count=["\'](\d+)["\']|全\s*(\d+)\s*(?:匹|件)|total[^0-9]{0,10}(\d+)\s*species',
    re.IGNORECASE,
)


def page_is_well_formed(html: str) -> bool:
    """Whether the response body ends with a proper closing ``</html>`` tag.

    A response cut off mid-transfer would not have this -- this is the
    signal used to reject a silently truncated fetch rather than treating a
    partial document as if it were the whole page.
    """

    return bool(_WELL_FORMED_TERMINATION_RE.search(html))


def extract_next_page_url(html: str, *, base_url: str) -> str | None:
    """The absolute URL of an explicit "next page" link, if the page has one."""

    match = _NEXT_LINK_REL_RE.search(html) or _NEXT_LINK_TEXT_RE.search(html)
    if match is None:
        return None
    href: str = match.group(1)
    return urljoin(base_url, href)


def extract_advertised_total(html: str) -> int | None:
    """An explicit source-advertised total species count, if the page has one."""

    match = _ADVERTISED_TOTAL_RE.search(html)
    if match is None:
        return None
    for group in match.groups():
        if group is not None:
            return int(group)
    return None  # pragma: no cover - regex always has exactly one live group


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
