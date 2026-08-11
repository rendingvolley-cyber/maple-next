from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.opponent_intel_db.downloader import DownloadError, SnapshotDownloader

ROBOTS_TXT_DISALLOW_PATH = "\n".join(
    [
        "User-agent: *",
        "Disallow: /forbidden",
    ]
)

ROBOTS_TXT_ALLOW_ALL = "\n".join(
    [
        "User-agent: *",
        "Allow: /",
    ]
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


def make_fetch(pages: dict[str, str], calls: list[str]) -> object:
    def fetch(url: str) -> str:
        calls.append(url)
        if url not in pages:
            raise RuntimeError(f"unexpected fetch: {url}")
        return pages[url]

    return fetch


def test_rate_limiting_sleeps_between_requests() -> None:
    clock = FakeClock()
    calls: list[str] = []
    pages = {
        "https://example.test/robots.txt": ROBOTS_TXT_ALLOW_ALL,
        "https://example.test/a": "page-a",
        "https://example.test/b": "page-b",
    }
    downloader = SnapshotDownloader(
        fetch=make_fetch(pages, calls),
        sleep=clock.sleep,
        now=clock.monotonic,
        min_interval_seconds=1.5,
    )

    downloader.get("https://example.test/a")
    downloader.get("https://example.test/b")

    assert clock.sleep_calls == [1.5]


def test_no_sleep_before_first_request() -> None:
    clock = FakeClock()
    calls: list[str] = []
    pages = {
        "https://example.test/robots.txt": ROBOTS_TXT_ALLOW_ALL,
        "https://example.test/a": "page-a",
    }
    downloader = SnapshotDownloader(
        fetch=make_fetch(pages, calls),
        sleep=clock.sleep,
        now=clock.monotonic,
        min_interval_seconds=1.5,
    )
    downloader.get("https://example.test/a")
    assert clock.sleep_calls == []


def test_robots_disallow_skips_the_url() -> None:
    clock = FakeClock()
    calls: list[str] = []
    pages = {
        "https://example.test/robots.txt": ROBOTS_TXT_DISALLOW_PATH,
    }
    downloader = SnapshotDownloader(
        fetch=make_fetch(pages, calls),
        sleep=clock.sleep,
        now=clock.monotonic,
    )

    with pytest.raises(DownloadError, match="ROBOTS_DISALLOWED"):
        downloader.get("https://example.test/forbidden/page")

    # robots.txt itself was fetched, but the disallowed page never was.
    assert "https://example.test/forbidden/page" not in calls


def test_robots_allow_permits_other_paths() -> None:
    clock = FakeClock()
    calls: list[str] = []
    pages = {
        "https://example.test/robots.txt": ROBOTS_TXT_DISALLOW_PATH,
        "https://example.test/allowed": "page content",
    }
    downloader = SnapshotDownloader(
        fetch=make_fetch(pages, calls),
        sleep=clock.sleep,
        now=clock.monotonic,
    )
    result = downloader.get("https://example.test/allowed")
    assert result == "page content"


def test_same_url_never_fetched_twice() -> None:
    clock = FakeClock()
    calls: list[str] = []
    pages = {
        "https://example.test/robots.txt": ROBOTS_TXT_ALLOW_ALL,
        "https://example.test/a": "page-a",
    }
    downloader = SnapshotDownloader(
        fetch=make_fetch(pages, calls),
        sleep=clock.sleep,
        now=clock.monotonic,
    )
    downloader.get("https://example.test/a")
    downloader.get("https://example.test/a")
    assert calls.count("https://example.test/a") == 1


def test_retries_once_on_transient_error_then_succeeds() -> None:
    clock = FakeClock()
    attempts: list[str] = []

    def flaky_fetch(url: str) -> str:
        attempts.append(url)
        if url == "https://example.test/robots.txt":
            return ROBOTS_TXT_ALLOW_ALL
        if attempts.count(url) == 1:
            raise ConnectionError("transient")
        return "recovered content"

    downloader = SnapshotDownloader(fetch=flaky_fetch, sleep=clock.sleep, now=clock.monotonic)
    result = downloader.get("https://example.test/flaky")
    assert result == "recovered content"


def test_gives_up_after_one_retry() -> None:
    clock = FakeClock()

    def always_fails(url: str) -> str:
        if url == "https://example.test/robots.txt":
            return ROBOTS_TXT_ALLOW_ALL
        raise ConnectionError("permanently broken")

    downloader = SnapshotDownloader(fetch=always_fails, sleep=clock.sleep, now=clock.monotonic)
    with pytest.raises(DownloadError, match="FETCH_FAILED"):
        downloader.get("https://example.test/broken")


def test_download_failure_leaves_existing_snapshot_untouched(tmp_path: Path) -> None:
    from maple_next.opponent_intel_db.normalize import SpeciesStatsRecord
    from maple_next.opponent_intel_db.snapshot_store import read_snapshot, write_snapshot_atomic

    snapshot_path = tmp_path / "species_stats_snapshot.json"
    original_record = SpeciesStatsRecord(
        species_id="garchomp",
        display_name="Garchomp",
        season="M-5",
        format="single",
        source="pokechamdb",
        source_url="https://pokechamdb.com/pokemon/garchomp",
        source_updated_at=None,
        fetched_at="2026-08-11T00:00:00+00:00",
        ranking=1.0,
    )
    write_snapshot_atomic(
        snapshot_path,
        [original_record],
        source="pokechamdb",
        season="M-5",
        format="single",
        fetched_at="2026-08-11T00:00:00+00:00",
    )
    original_bytes = snapshot_path.read_bytes()

    clock = FakeClock()

    def always_fails(url: str) -> str:
        raise ConnectionError("network is down")

    downloader = SnapshotDownloader(fetch=always_fails, sleep=clock.sleep, now=clock.monotonic)

    # Simulate an updater run that fails before ever calling write_snapshot_atomic
    # again -- the CLI's fetch_primary_species raises CliError in this situation
    # and run_update_opponent_intel returns before touching the snapshot file.
    with pytest.raises(DownloadError):
        downloader.get("https://pokechamdb.com/?view=pokemon&format=single&season=M-5")

    assert snapshot_path.read_bytes() == original_bytes

    document = read_snapshot(snapshot_path)
    assert document is not None
    assert document.species["garchomp"].display_name == "Garchomp"


def test_cli_update_leaves_existing_snapshot_untouched_on_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full-CLI version of the failure-preserves-snapshot guarantee."""

    from maple_next.opponent_intel_db import cli
    from maple_next.opponent_intel_db.normalize import SpeciesStatsRecord
    from maple_next.opponent_intel_db.runtime_paths import ensure_intel_db_directory
    from maple_next.opponent_intel_db.snapshot_store import write_snapshot_atomic

    runtime_root = tmp_path / "runtime"
    intel_directory = ensure_intel_db_directory(runtime_root)
    snapshot_path = intel_directory / cli.SNAPSHOT_FILENAME
    write_snapshot_atomic(
        snapshot_path,
        [
            SpeciesStatsRecord(
                species_id="garchomp",
                display_name="Garchomp",
                season="M-5",
                format="single",
                source="pokechamdb",
                source_url="https://pokechamdb.com/pokemon/garchomp",
                source_updated_at=None,
                fetched_at="2026-08-11T00:00:00+00:00",
                ranking=1.0,
            )
        ],
        source="pokechamdb",
        season="M-5",
        format="single",
        fetched_at="2026-08-11T00:00:00+00:00",
    )
    original_bytes = snapshot_path.read_bytes()

    def always_raises(url: str) -> str:
        raise ConnectionError("network is down")

    monkeypatch.setattr(
        cli,
        "SnapshotDownloader",
        lambda **kwargs: SnapshotDownloader(fetch=always_raises, sleep=lambda _seconds: None),
    )

    exit_code = cli.main(
        [
            "update-opponent-intel",
            "--runtime-root",
            str(runtime_root),
        ]
    )

    assert exit_code == 1
    assert snapshot_path.read_bytes() == original_bytes
