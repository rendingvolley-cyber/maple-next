"""Discovery-completeness proof regression tests.

``succeeded == attempted`` alone only proves internal consistency of
whatever subset of species was discovered -- never that discovery itself
was exhaustive. These tests exercise ``discover_all_species`` directly
against synthetic multi-page/truncated/inconsistent fixtures to prove the
completeness gate actually distinguishes proven-complete discovery from
every way it can silently fail.
"""

from __future__ import annotations

import pytest

from maple_next.opponent_intel_db.discovery import DiscoveryStatus, discover_all_species
from maple_next.opponent_intel_db.downloader import SnapshotDownloader

ROBOTS_TXT_ALLOW_ALL = "User-agent: *\nAllow: /\n"
LIST_URL = "https://pokechamdb.com/?view=pokemon&format=single&season=M-5"


def _page(*anchors: str, well_formed: bool = True) -> str:
    body = "\n".join(anchors)
    if well_formed:
        return f"<!DOCTYPE html><html><body>{body}</body></html>"
    # Deliberately never closes </body></html> -- simulates a response cut
    # off mid-transfer.
    return f"<!DOCTYPE html><html><body>{body}"


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


def test_1_full_valid_discovery_is_complete() -> None:
    page = _page(
        _anchor("garchomp", 1, "ガブリアス"),
        _anchor("primarina", 2, "アシレーヌ"),
        _anchor("gyarados", 3, "ギャラドス"),
    )
    downloader = _downloader({LIST_URL: page})

    outcome = discover_all_species(downloader, LIST_URL)

    assert outcome.status is DiscoveryStatus.COMPLETE
    assert {entry.species_id for entry in outcome.entries} == {
        "garchomp",
        "primarina",
        "gyarados",
    }
    assert outcome.pages_visited == 1


def test_2_silently_truncated_response_is_not_complete() -> None:
    # Missing trailing entries because the transfer itself was cut off --
    # the body never reaches a closing </html>.
    truncated_page = _page(
        _anchor("garchomp", 1, "ガブリアス"),
        _anchor("primarina", 2, "アシレーヌ"),
        well_formed=False,
    )
    downloader = _downloader({LIST_URL: truncated_page})

    outcome = discover_all_species(downloader, LIST_URL)

    assert outcome.status is not DiscoveryStatus.COMPLETE
    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "NOT_WELL_FORMED" in outcome.reason


def test_3_intermediate_pagination_page_missing_is_not_complete() -> None:
    page1_url = LIST_URL
    page2_url = "https://pokechamdb.com/?view=pokemon&format=single&season=M-5&page=2"
    page1 = (
        '<!DOCTYPE html><html><body>'
        + _anchor("garchomp", 1, "ガブリアス")
        + f'<a rel="next" href="{page2_url}">Next</a>'
        + "</body></html>"
    )

    def fetch(url: str) -> str:
        if url.endswith("/robots.txt"):
            return ROBOTS_TXT_ALLOW_ALL
        if url == page1_url:
            return page1
        if url == page2_url:
            raise ConnectionError("page 2 is unreachable")
        raise AssertionError(f"unexpected URL: {url}")

    downloader = SnapshotDownloader(fetch=fetch, sleep=lambda _seconds: None)

    outcome = discover_all_species(downloader, page1_url)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "REQUIRED_PAGE_FETCH_FAILED" in outcome.reason


def test_4_duplicated_pagination_page_is_not_complete() -> None:
    page1_url = LIST_URL
    page2_url = "https://pokechamdb.com/?view=pokemon&format=single&season=M-5&page=2"
    page1 = (
        '<!DOCTYPE html><html><body>'
        + _anchor("garchomp", 1, "ガブリアス")
        + f'<a rel="next" href="{page2_url}">Next</a>'
        + "</body></html>"
    )
    # page2's "next" link loops back to page1 -- a broken pagination chain.
    page2 = (
        '<!DOCTYPE html><html><body>'
        + _anchor("primarina", 2, "アシレーヌ")
        + f'<a rel="next" href="{page1_url}">Next</a>'
        + "</body></html>"
    )

    downloader = _downloader({page1_url: page1, page2_url: page2})

    outcome = discover_all_species(downloader, page1_url)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "DUPLICATE_PAGINATION_PAGE" in outcome.reason


def test_5_advertised_total_mismatch_is_not_complete() -> None:
    # Advertises 5 species but only 3 are actually present on the page.
    page = (
        '<!DOCTYPE html><html><body><div data-total-count="5"></div>'
        + _anchor("garchomp", 1, "ガブリアス")
        + _anchor("primarina", 2, "アシレーヌ")
        + _anchor("gyarados", 3, "ギャラドス")
        + "</body></html>"
    )
    downloader = _downloader({LIST_URL: page})

    outcome = discover_all_species(downloader, LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "ADVERTISED_TOTAL_MISMATCH" in outcome.reason


def test_6_no_completeness_signal_is_unprovable_not_complete() -> None:
    # Well-formed, single page, no pagination, no advertised total, and no
    # per-entry rank number at all -- nothing here has been proven
    # incomplete, but nothing has been proven complete either.
    page = (
        "<!DOCTYPE html><html><body>"
        '<a href="/pokemon/garchomp?season=M-5&format=single">ガブリアス</a>'
        '<a href="/pokemon/primarina?season=M-5&format=single">アシレーヌ</a>'
        "</body></html>"
    )
    downloader = _downloader({LIST_URL: page})

    outcome = discover_all_species(downloader, LIST_URL)

    assert outcome.status is DiscoveryStatus.UNPROVABLE
    assert outcome.status is not DiscoveryStatus.COMPLETE
    assert outcome.reason == "NO_COMPLETENESS_SIGNAL_AVAILABLE"


def test_rank_sequence_gap_is_not_complete() -> None:
    # Site-declared ranks 1 and 3 present, 2 missing -- a genuine internal
    # inconsistency in what was discovered.
    page = _page(
        _anchor("garchomp", 1, "ガブリアス"),
        _anchor("gyarados", 3, "ギャラドス"),
    )
    downloader = _downloader({LIST_URL: page})

    outcome = discover_all_species(downloader, LIST_URL)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "RANK_SEQUENCE_NOT_CONTIGUOUS" in outcome.reason


def test_multi_page_pagination_terminates_and_is_complete() -> None:
    page1_url = LIST_URL
    page2_url = "https://pokechamdb.com/?view=pokemon&format=single&season=M-5&page=2"
    page1 = (
        '<!DOCTYPE html><html><body>'
        + _anchor("garchomp", 1, "ガブリアス")
        + f'<a rel="next" href="{page2_url}">Next</a>'
        + "</body></html>"
    )
    # Terminal page: no further "next" link.
    page2 = _page(_anchor("primarina", 2, "アシレーヌ"))

    downloader = _downloader({page1_url: page1, page2_url: page2})

    outcome = discover_all_species(downloader, page1_url)

    assert outcome.status is DiscoveryStatus.COMPLETE
    assert outcome.pages_visited == 2
    assert {entry.species_id for entry in outcome.entries} == {"garchomp", "primarina"}


@pytest.mark.parametrize("duplicate_slug", ["garchomp"])
def test_duplicate_species_across_pages_is_not_complete(duplicate_slug: str) -> None:
    page1_url = LIST_URL
    page2_url = "https://pokechamdb.com/?view=pokemon&format=single&season=M-5&page=2"
    page1 = (
        '<!DOCTYPE html><html><body>'
        + _anchor("garchomp", 1, "ガブリアス")
        + f'<a rel="next" href="{page2_url}">Next</a>'
        + "</body></html>"
    )
    # Same species repeated on page 2 (rank renumbered as "1" again there).
    page2 = _page(_anchor(duplicate_slug, 1, "ガブリアス"))

    downloader = _downloader({page1_url: page1, page2_url: page2})

    outcome = discover_all_species(downloader, page1_url)

    assert outcome.status is DiscoveryStatus.INCOMPLETE
    assert outcome.reason is not None
    assert "DUPLICATE_SPECIES_ACROSS_PAGES" in outcome.reason
