"""Adversarial proof tests for source-list discovery completeness."""

from __future__ import annotations

import pytest

from maple_next.opponent_intel_db.discovery import DiscoveryStatus, discover_all_species
from maple_next.opponent_intel_db.downloader import SnapshotDownloader

ROBOTS_TXT_ALLOW_ALL = "User-agent: *\nAllow: /\n"
LIST_URL = "https://pokechamdb.com/?view=pokemon&format=single&season=M-5"


def _page(
    *anchors: str,
    well_formed: bool = True,
    advertised_total: int | None = None,
    declared_last_page: int | None = None,
) -> str:
    metadata = ""
    if advertised_total is not None:
        metadata += f'<div data-total-count="{advertised_total}"></div>'
    if declared_last_page is not None:
        metadata += f'<div data-last-page="{declared_last_page}"></div>'
    body = metadata + "\n".join(anchors)
    ending = "</body></html>" if well_formed else ""
    return f"<!DOCTYPE html><html><body>{body}{ending}"


def _anchor(slug: str, site_rank: int, name: str) -> str:
    href = f"/pokemon/{slug}?season=M-5&format=single"
    return f'<a href="{href}"><span>{site_rank}</span><span>{name}</span></a>'


def _downloader(pages: dict[str, str]) -> SnapshotDownloader:
    def fetch(url: str) -> str:
        if url.endswith("/robots.txt"):
            return ROBOTS_TXT_ALLOW_ALL
        if url in pages:
            return pages[url]
        raise ConnectionError(f"unexpected URL in test fixture: {url}")

    return SnapshotDownloader(fetch=fetch, sleep=lambda _seconds: None)


def test_full_source_advertised_discovery_is_complete() -> None:
    page = _page(
        _anchor("garchomp", 1, "Garchomp"),
        _anchor("primarina", 2, "Primarina"),
        _anchor("gyarados", 3, "Gyarados"),
        advertised_total=3,
    )

    outcome = discover_all_species(_downloader({LIST_URL: page}), LIST_URL)

    assert outcome.status is DiscoveryStatus.COMPLETE
    assert len(outcome.entries) == 3


def test_silently_transport_truncated_response_is_incomplete() -> None:
    page = _page(_anchor("garchomp", 1, "Garchomp"), well_formed=False)

    outcome = discover_all_species(_downloader({LIST_URL: page}), LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "NOT_WELL_FORMED" in outcome.reason


@pytest.mark.parametrize("terminal_rank", [100, 235])
def test_contiguous_ranks_without_positive_terminal_proof_are_unprovable(
    terminal_rank: int,
) -> None:
    page = _page(
        *(
            _anchor(f"species-{rank}", rank, f"Species {rank}")
            for rank in range(1, terminal_rank + 1)
        )
    )

    outcome = discover_all_species(_downloader({LIST_URL: page}), LIST_URL)

    assert outcome.status is DiscoveryStatus.UNPROVABLE
    assert outcome.reason == "NO_POSITIVE_TERMINAL_PROOF"


def test_well_formed_html_and_no_next_link_are_not_terminal_proof() -> None:
    outcome = discover_all_species(
        _downloader({LIST_URL: _page(_anchor("one", 1, "One"))}), LIST_URL
    )

    assert outcome.status is DiscoveryStatus.UNPROVABLE


def test_advertised_total_235_with_only_100_unique_is_incomplete() -> None:
    page = _page(
        *(
            _anchor(f"species-{rank}", rank, f"Species {rank}")
            for rank in range(1, 101)
        ),
        advertised_total=235,
    )

    outcome = discover_all_species(_downloader({LIST_URL: page}), LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "ADVERTISED_TOTAL_MISMATCH" in outcome.reason


def test_advertised_total_235_with_consistent_full_coverage_is_complete() -> None:
    page = _page(
        *(
            _anchor(f"species-{rank}", rank, f"Species {rank}")
            for rank in range(1, 236)
        ),
        advertised_total=235,
    )

    outcome = discover_all_species(_downloader({LIST_URL: page}), LIST_URL)

    assert outcome.status is DiscoveryStatus.COMPLETE
    assert len(outcome.entries) == 235


def test_rank_gap_is_incomplete_even_when_total_matches() -> None:
    page = _page(
        _anchor("one", 1, "One"),
        _anchor("three", 3, "Three"),
        advertised_total=2,
    )

    outcome = discover_all_species(_downloader({LIST_URL: page}), LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "RANK_SEQUENCE_NOT_CONTIGUOUS" in outcome.reason


def test_missing_required_pagination_page_is_incomplete() -> None:
    page2 = LIST_URL + "&page=2"
    page1 = _page(
        _anchor("one", 1, "One"),
        f'<a rel="next" href="{page2}">Next</a>',
        declared_last_page=2,
    )

    outcome = discover_all_species(_downloader({LIST_URL: page1}), LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "REQUIRED_PAGE_FETCH_FAILED" in outcome.reason


def test_duplicate_page_or_pagination_loop_is_incomplete() -> None:
    page2 = LIST_URL + "&page=2"
    pages = {
        LIST_URL: _page(
            _anchor("one", 1, "One"),
            f'<a rel="next" href="{page2}">Next</a>',
            declared_last_page=2,
        ),
        page2: _page(
            _anchor("two", 2, "Two"),
            f'<a rel="next" href="{LIST_URL}">Next</a>',
            declared_last_page=2,
        ),
    }

    outcome = discover_all_species(_downloader(pages), LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "DUPLICATE_PAGINATION_PAGE" in outcome.reason


def test_duplicate_species_across_pages_is_incomplete() -> None:
    page2 = LIST_URL + "&page=2"
    pages = {
        LIST_URL: _page(
            _anchor("same", 1, "Same"),
            f'<a rel="next" href="{page2}">Next</a>',
            declared_last_page=2,
        ),
        page2: _page(_anchor("same", 2, "Same Again"), declared_last_page=2),
    }

    outcome = discover_all_species(_downloader(pages), LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason == "DUPLICATE_SPECIES_ACROSS_PAGES"


def test_declared_last_page_requires_pages_one_through_three_exactly_once() -> None:
    page2 = LIST_URL + "&page=2"
    page3 = LIST_URL + "&page=3"
    pages = {
        LIST_URL: _page(
            _anchor("one", 1, "One"),
            f'<a rel="next" href="{page2}">Next</a>',
            declared_last_page=3,
        ),
        page2: _page(
            _anchor("two", 2, "Two"),
            f'<a rel="next" href="{page3}">Next</a>',
            declared_last_page=3,
        ),
        page3: _page(_anchor("three", 3, "Three"), declared_last_page=3),
    }

    outcome = discover_all_species(_downloader(pages), LIST_URL)

    assert outcome.status is DiscoveryStatus.COMPLETE
    assert outcome.pages_visited == 3


def test_declared_last_page_with_page_two_skipped_is_incomplete() -> None:
    page3 = LIST_URL + "&page=3"
    pages = {
        LIST_URL: _page(
            _anchor("one", 1, "One"),
            f'<a rel="next" href="{page3}">Next</a>',
            declared_last_page=3,
        ),
        page3: _page(_anchor("three", 3, "Three"), declared_last_page=3),
    }

    outcome = discover_all_species(_downloader(pages), LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE


def test_terminal_marker_missing_without_other_proof_is_unprovable() -> None:
    page2 = LIST_URL + "&page=2"
    pages = {
        LIST_URL: _page(
            _anchor("one", 1, "One"), f'<a rel="next" href="{page2}">Next</a>'
        ),
        page2: _page(_anchor("two", 2, "Two")),
    }

    outcome = discover_all_species(_downloader(pages), LIST_URL)

    assert outcome.status is DiscoveryStatus.UNPROVABLE


def test_malformed_entry_cannot_shrink_advertised_total_and_complete() -> None:
    page = _page(
        _anchor("one", 1, "One"),
        '<a href="/pokemon/broken"><span>2</span><span> </span></a>',
        _anchor("three", 3, "Three"),
        advertised_total=3,
    )

    outcome = discover_all_species(_downloader({LIST_URL: page}), LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "ADVERTISED_TOTAL_MISMATCH" in outcome.reason
