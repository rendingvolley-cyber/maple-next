"""Discovery-completeness proof for the pokechamdb ranked species list.

A single successfully-parsed HTML page is *not*, by itself, proof that
discovery captured every ranked species the source advertises -- it only
proves that whatever anchors were present in that one response parsed
cleanly. ``succeeded == attempted`` (the previous safeguard) has the same
blind spot: it proves internal consistency of the discovered subset, never
that the subset is the *whole* set. A response silently truncated
mid-transfer, or a source that paginates and only ever fetching page one,
would both pass that check while missing real species.

This module builds an explicit, source-native completeness proof instead:

* the response body must be well-formed (:func:`parser_pokechamdb.
  page_is_well_formed`) -- a transfer cut off mid-stream is detected here,
  not silently accepted as "the whole list".
* pagination is followed with duplicate-page/loop detection, but an absent
  "next" link is never itself terminal proof.
* if any page advertises an explicit total species count, the number of
  *unique* species actually discovered across every page must match it
  exactly.
* alternatively, explicit last-page metadata must declare the terminal page
  and every page from 1 through that page must be fetched exactly once.
* the source's own displayed rank numbers (``SpeciesListEntry.site_rank``,
  not this module's internal enumeration order) must form the exact
  contiguous set ``{1, .., N}`` with no gaps or duplicates -- self-
  consistency evidence for whatever pages were traversed.

Well-formed HTML and contiguous ranks are integrity checks only.  Neither
can upgrade an otherwise unproven result to ``COMPLETE``.  Without one of
the two positive terminal proofs above this returns ``UNPROVABLE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlparse

from maple_next.opponent_intel_db.downloader import DownloadError, SnapshotDownloader
from maple_next.opponent_intel_db.parser_pokechamdb import (
    ParseError,
    SpeciesListEntry,
    extract_advertised_total,
    extract_declared_last_page,
    extract_next_page_url,
    page_is_well_formed,
    parse_species_list,
)

#: Hard ceiling on pages followed in one discovery traversal -- bounds a
#: pathological/malicious pagination chain rather than looping forever.
MAX_PAGES = 50


class DiscoveryStatus(Enum):
    COMPLETE = "DISCOVERY_COMPLETE"
    INCOMPLETE = "DISCOVERY_INCOMPLETE"
    UNPROVABLE = "DISCOVERY_UNPROVABLE"


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    status: DiscoveryStatus
    entries: tuple[SpeciesListEntry, ...]
    pages_visited: int
    reason: str | None = None


def _dedup_preserving_order(
    entries: list[SpeciesListEntry],
) -> tuple[list[SpeciesListEntry], bool]:
    """Drop duplicate species ids across pages; report whether any existed."""

    seen: set[str] = set()
    result: list[SpeciesListEntry] = []
    had_duplicate = False
    for entry in entries:
        if entry.species_id in seen:
            had_duplicate = True
            continue
        seen.add(entry.species_id)
        result.append(entry)
    return result, had_duplicate


def _check_rank_sequence(entries: list[SpeciesListEntry]) -> str | None:
    """``None`` if site-declared ranks form the exact contiguous {1..N} set.

    Returns a reason string describing the mismatch otherwise. If no entry
    carries a ``site_rank`` at all, returns a distinct "no rank data"
    sentinel reason so the caller can treat that as unprovable rather than
    as a proven mismatch.
    """

    site_ranks = [entry.site_rank for entry in entries if entry.site_rank is not None]
    if not site_ranks:
        return "NO_SITE_RANK_DATA"
    if len(site_ranks) != len(entries):
        return "SOME_ENTRIES_MISSING_SITE_RANK"
    expected = set(range(1, len(entries) + 1))
    actual = set(site_ranks)
    if actual != expected or len(site_ranks) != len(set(site_ranks)):
        return (
            f"RANK_SEQUENCE_NOT_CONTIGUOUS:expected_size={len(expected)}:"
            f"actual_size={len(actual)}"
        )
    return None


def _page_number(url: str) -> int | None:
    """Return the source page number (the first page omits ``page=1``)."""

    values = parse_qs(urlparse(url).query).get("page")
    if values is None:
        return 1
    if len(values) != 1:
        return None
    try:
        page = int(values[0])
    except ValueError:
        return None
    return page if page >= 1 else None


def discover_all_species(
    downloader: SnapshotDownloader, list_url: str
) -> DiscoveryOutcome:
    """Fetch + traverse the ranked species list with an explicit completeness proof."""

    visited_urls: list[str] = []
    all_entries: list[SpeciesListEntry] = []
    advertised_total: int | None = None
    declared_last_page: int | None = None

    current_url: str | None = list_url
    while current_url is not None:
        if len(visited_urls) >= MAX_PAGES:
            return DiscoveryOutcome(
                status=DiscoveryStatus.INCOMPLETE,
                entries=tuple(all_entries),
                pages_visited=len(visited_urls),
                reason=f"PAGINATION_EXCEEDED_MAX_PAGES:{MAX_PAGES}",
            )
        if current_url in visited_urls:
            # A "next page" link pointing back at an already-visited page is
            # a broken/looping pagination chain, not a legitimate traversal.
            return DiscoveryOutcome(
                status=DiscoveryStatus.INCOMPLETE,
                entries=tuple(all_entries),
                pages_visited=len(visited_urls),
                reason=f"DUPLICATE_PAGINATION_PAGE:{current_url}",
            )

        try:
            html = downloader.get(current_url)
        except DownloadError as exc:
            # A next-page link was known to exist and following it failed --
            # this is positive evidence of missing data, not mere
            # uncertainty, so it's INCOMPLETE rather than UNPROVABLE.
            return DiscoveryOutcome(
                status=DiscoveryStatus.INCOMPLETE,
                entries=tuple(all_entries),
                pages_visited=len(visited_urls),
                reason=f"REQUIRED_PAGE_FETCH_FAILED:{current_url}:{exc}",
            )
        visited_urls.append(current_url)

        if not page_is_well_formed(html):
            return DiscoveryOutcome(
                status=DiscoveryStatus.INCOMPLETE,
                entries=tuple(all_entries),
                pages_visited=len(visited_urls),
                reason=f"RESPONSE_NOT_WELL_FORMED_LIKELY_TRUNCATED:{current_url}",
            )

        try:
            page_entries = parse_species_list(html)
        except ParseError as exc:
            return DiscoveryOutcome(
                status=DiscoveryStatus.INCOMPLETE,
                entries=tuple(all_entries),
                pages_visited=len(visited_urls),
                reason=f"PAGE_UNPARSABLE:{current_url}:{exc}",
            )
        all_entries.extend(page_entries)

        page_total = extract_advertised_total(html)
        if page_total is not None:
            if advertised_total is not None and advertised_total != page_total:
                return DiscoveryOutcome(
                    status=DiscoveryStatus.INCOMPLETE,
                    entries=tuple(all_entries),
                    pages_visited=len(visited_urls),
                    reason=(
                        f"CONFLICTING_ADVERTISED_TOTAL:{advertised_total}!={page_total}"
                    ),
                )
            advertised_total = page_total

        page_last = extract_declared_last_page(html)
        if page_last is not None:
            if page_last < 1:
                return DiscoveryOutcome(
                    status=DiscoveryStatus.INCOMPLETE,
                    entries=tuple(all_entries),
                    pages_visited=len(visited_urls),
                    reason=f"INVALID_DECLARED_LAST_PAGE:{page_last}",
                )
            if declared_last_page is not None and declared_last_page != page_last:
                return DiscoveryOutcome(
                    status=DiscoveryStatus.INCOMPLETE,
                    entries=tuple(all_entries),
                    pages_visited=len(visited_urls),
                    reason=(
                        f"CONFLICTING_DECLARED_LAST_PAGE:{declared_last_page}!={page_last}"
                    ),
                )
            declared_last_page = page_last

        next_url = extract_next_page_url(html, base_url=current_url)
        if next_url is None and declared_last_page is not None:
            current_page = _page_number(current_url)
            if current_page is None or current_page < declared_last_page:
                return DiscoveryOutcome(
                    status=DiscoveryStatus.INCOMPLETE,
                    entries=tuple(all_entries),
                    pages_visited=len(visited_urls),
                    reason=(
                        "PAGINATION_TERMINATED_BEFORE_DECLARED_LAST_PAGE:"
                        f"current={current_page}:last={declared_last_page}"
                    ),
                )
        current_url = next_url

    deduped_entries, had_duplicate_species = _dedup_preserving_order(all_entries)
    if had_duplicate_species:
        return DiscoveryOutcome(
            status=DiscoveryStatus.INCOMPLETE,
            entries=tuple(deduped_entries),
            pages_visited=len(visited_urls),
            reason="DUPLICATE_SPECIES_ACROSS_PAGES",
        )

    if advertised_total is not None and advertised_total != len(deduped_entries):
        return DiscoveryOutcome(
            status=DiscoveryStatus.INCOMPLETE,
            entries=tuple(deduped_entries),
            pages_visited=len(visited_urls),
            reason=(
                f"ADVERTISED_TOTAL_MISMATCH:advertised={advertised_total} "
                f"discovered={len(deduped_entries)}"
            ),
        )

    rank_reason = _check_rank_sequence(deduped_entries)
    if rank_reason not in (None, "NO_SITE_RANK_DATA"):
        return DiscoveryOutcome(
            status=DiscoveryStatus.INCOMPLETE,
            entries=tuple(deduped_entries),
            pages_visited=len(visited_urls),
            reason=rank_reason,
        )

    if declared_last_page is not None:
        page_numbers = [_page_number(url) for url in visited_urls]
        expected_pages = list(range(1, declared_last_page + 1))
        if page_numbers != expected_pages:
            return DiscoveryOutcome(
                status=DiscoveryStatus.INCOMPLETE,
                entries=tuple(deduped_entries),
                pages_visited=len(visited_urls),
                reason=(
                    "DECLARED_PAGE_COVERAGE_MISMATCH:"
                    f"expected={expected_pages}:actual={page_numbers}"
                ),
            )

    has_total_proof = advertised_total is not None
    has_last_page_proof = declared_last_page is not None
    if not has_total_proof and not has_last_page_proof:
        return DiscoveryOutcome(
            status=DiscoveryStatus.UNPROVABLE,
            entries=tuple(deduped_entries),
            pages_visited=len(visited_urls),
            reason="NO_POSITIVE_TERMINAL_PROOF",
        )

    return DiscoveryOutcome(
        status=DiscoveryStatus.COMPLETE,
        entries=tuple(deduped_entries),
        pages_visited=len(visited_urls),
    )
