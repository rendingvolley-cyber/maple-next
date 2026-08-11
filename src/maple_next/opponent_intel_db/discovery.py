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
* if a page advertises pagination (a "next page" link), that link is
  followed until a terminal page (no further "next" link) is reached, with
  duplicate-page/loop detection -- a link that points back to an
  already-visited URL is treated as a broken/incomplete traversal, not
  silently ignored.
* if any page advertises an explicit total species count, the number of
  *unique* species actually discovered across every page must match it
  exactly.
* the source's own displayed rank numbers (``SpeciesListEntry.site_rank``,
  not this module's internal enumeration order) must form the exact
  contiguous set ``{1, .., N}`` with no gaps or duplicates -- self-
  consistency evidence for whatever pages were traversed.

If none of the above can be established at all (the very first response
isn't well-formed, or the source displays no rank numbers to check and
offers no pagination/total-count signal either), this returns
``UNPROVABLE`` rather than assuming completeness -- discovery is not
proven complete just because nothing was proven *in*complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from maple_next.opponent_intel_db.downloader import DownloadError, SnapshotDownloader
from maple_next.opponent_intel_db.parser_pokechamdb import (
    ParseError,
    SpeciesListEntry,
    extract_advertised_total,
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


def discover_all_species(
    downloader: SnapshotDownloader, list_url: str
) -> DiscoveryOutcome:
    """Fetch + traverse the ranked species list with an explicit completeness proof."""

    visited_urls: list[str] = []
    all_entries: list[SpeciesListEntry] = []
    advertised_total: int | None = None

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

        current_url = extract_next_page_url(html, base_url=current_url)

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
    if rank_reason == "NO_SITE_RANK_DATA":
        # No independent signal at all (no pagination was followed beyond
        # page one, no advertised total, and the page displays no rank
        # numbers to cross-check) -- nothing here has been proven
        # incomplete, but nothing has been proven complete either.
        if advertised_total is None and len(visited_urls) == 1:
            return DiscoveryOutcome(
                status=DiscoveryStatus.UNPROVABLE,
                entries=tuple(deduped_entries),
                pages_visited=len(visited_urls),
                reason="NO_COMPLETENESS_SIGNAL_AVAILABLE",
            )
    elif rank_reason is not None:
        return DiscoveryOutcome(
            status=DiscoveryStatus.INCOMPLETE,
            entries=tuple(deduped_entries),
            pages_visited=len(visited_urls),
            reason=rank_reason,
        )

    return DiscoveryOutcome(
        status=DiscoveryStatus.COMPLETE,
        entries=tuple(deduped_entries),
        pages_visited=len(visited_urls),
    )
