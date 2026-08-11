"""Partial-import atomicity: a primary-source import that isn't 100% complete
must never promote and must leave any existing valid snapshot (and move
catalog) byte-identical. Regression coverage for the fail-closed promotion
gate in ``cli.run_update_opponent_intel`` / ``cli.fetch_primary_species``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.opponent_intel_db import cli
from maple_next.opponent_intel_db.downloader import SnapshotDownloader
from maple_next.opponent_intel_db.normalize import SpeciesStatsRecord
from maple_next.opponent_intel_db.runtime_paths import ensure_intel_db_directory
from maple_next.opponent_intel_db.snapshot_store import read_snapshot, write_snapshot_atomic

ROBOTS_TXT_ALLOW_ALL = "User-agent: *\nAllow: /\n"

LIST_HTML = """<!DOCTYPE html><html><body>
<a href="/pokemon/garchomp?season=M-5&format=single"><span>1</span><span>ガブリアス</span></a>
<a href="/pokemon/primarina?season=M-5&format=single"><span>2</span><span>アシレーヌ</span></a>
<a href="/pokemon/gyarados?season=M-5&format=single"><span>3</span><span>ギャラドス</span></a>
</body></html>"""


def _detail_html(move: str, percentage: float) -> str:
    return f"<div>技</div><div>{move}: {percentage}%</div>"


def _seed_existing_snapshot(runtime_root: Path) -> tuple[Path, bytes]:
    intel_directory = ensure_intel_db_directory(runtime_root)
    snapshot_path = intel_directory / cli.SNAPSHOT_FILENAME
    write_snapshot_atomic(
        snapshot_path,
        [
            SpeciesStatsRecord(
                species_id="garchomp",
                display_name="Garchomp (existing good snapshot)",
                season="M-5",
                format="single",
                source="pokechamdb",
                source_url="https://pokechamdb.com/pokemon/garchomp",
                source_updated_at=None,
                fetched_at="2026-08-01T00:00:00+00:00",
                ranking=1.0,
            ),
        ],
        source="pokechamdb",
        season="M-5",
        format="single",
        fetched_at="2026-08-01T00:00:00+00:00",
    )
    return snapshot_path, snapshot_path.read_bytes()


def _run_with_fetch(runtime_root: Path, fetch, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(
        cli,
        "SnapshotDownloader",
        lambda **kwargs: SnapshotDownloader(fetch=fetch, sleep=lambda _seconds: None),
    )
    return cli.main(
        [
            "update-opponent-intel",
            "--runtime-root",
            str(runtime_root),
            "--skip-secondary",
        ]
    )


def test_partial_primary_import_never_promotes_and_preserves_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_root = tmp_path / "runtime"
    snapshot_path, original_bytes = _seed_existing_snapshot(runtime_root)
    catalog_path = snapshot_path.parent / cli.MOVE_CATALOG_FILENAME
    assert not catalog_path.exists()

    def fetch(url: str) -> str:
        if url.endswith("/robots.txt"):
            return ROBOTS_TXT_ALLOW_ALL
        if "?view=pokemon" in url:
            return LIST_HTML
        if "/pokemon/garchomp" in url:
            return _detail_html("じしん", 99.0)
        if "/pokemon/primarina" in url:
            return _detail_html("ハイドロポンプ", 80.0)
        if "/pokemon/gyarados" in url:
            # 1: some species succeed, 2: this later required fetch fails.
            raise ConnectionError("network is down for gyarados")
        raise AssertionError(f"unexpected URL: {url}")

    exit_code = _run_with_fetch(runtime_root, fetch, monkeypatch)

    # 3: command exit code reflects incomplete/failure.
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "UPDATE_INCOMPLETE" in stderr
    assert "FETCH_PARTIAL" in stderr
    assert "gyarados" in stderr

    # 4: old snapshot remains byte-identical.
    assert snapshot_path.read_bytes() == original_bytes
    document = read_snapshot(snapshot_path)
    assert document is not None
    assert document.species["garchomp"].display_name == "Garchomp (existing good snapshot)"

    # 5: no partial snapshot/catalog became production-current.
    assert not catalog_path.exists()


def test_total_primary_source_failure_never_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Discovery succeeds (a well-formed, rank-consistent list page), but
    every per-species detail fetch fails -- zero species end up importable.
    This is a degenerate ``FETCH_PARTIAL`` (succeeded=0, failed=attempted),
    not a distinct status -- it still never promotes."""

    runtime_root = tmp_path / "runtime"
    snapshot_path, original_bytes = _seed_existing_snapshot(runtime_root)

    def fetch(url: str) -> str:
        if url.endswith("/robots.txt"):
            return ROBOTS_TXT_ALLOW_ALL
        if "?view=pokemon" in url:
            return LIST_HTML
        raise ConnectionError("every detail page is unreachable")

    exit_code = _run_with_fetch(runtime_root, fetch, monkeypatch)

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "UPDATE_INCOMPLETE" in stderr
    assert "FETCH_PARTIAL" in stderr
    assert "succeeded=0" in stderr
    assert snapshot_path.read_bytes() == original_bytes


def test_list_page_totally_unreachable_reports_discovery_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the list page itself can't be fetched at all, that's a discovery
    failure (never even started), reported as DISCOVERY_INCOMPLETE."""

    runtime_root = tmp_path / "runtime"
    snapshot_path, original_bytes = _seed_existing_snapshot(runtime_root)

    def fetch(url: str) -> str:
        raise ConnectionError("network is down")

    exit_code = _run_with_fetch(runtime_root, fetch, monkeypatch)

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "UPDATE_INCOMPLETE" in stderr
    assert "DISCOVERY_INCOMPLETE" in stderr
    assert snapshot_path.read_bytes() == original_bytes


def test_complete_import_promotes_snapshot_and_move_catalog_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime_root = tmp_path / "runtime"
    intel_directory = ensure_intel_db_directory(runtime_root)
    snapshot_path = intel_directory / cli.SNAPSHOT_FILENAME
    catalog_path = intel_directory / cli.MOVE_CATALOG_FILENAME
    assert not snapshot_path.exists()
    assert not catalog_path.exists()

    def fetch(url: str) -> str:
        if url.endswith("/robots.txt"):
            return ROBOTS_TXT_ALLOW_ALL
        if "?view=pokemon" in url:
            return LIST_HTML
        if "/pokemon/garchomp" in url:
            return _detail_html("じしん", 99.0)
        if "/pokemon/primarina" in url:
            return _detail_html("ハイドロポンプ", 80.0)
        if "/pokemon/gyarados" in url:
            return _detail_html("たきのぼり", 70.0)
        raise AssertionError(f"unexpected URL: {url}")

    exit_code = _run_with_fetch(runtime_root, fetch, monkeypatch)

    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert "species_imported=3" in stdout

    assert snapshot_path.exists()
    assert catalog_path.exists()
    document = read_snapshot(snapshot_path)
    assert document is not None
    assert set(document.species) == {"garchomp", "primarina", "gyarados"}

    # The coherent-generation commit also switched, and both files resolve
    # to the exact same generation id through the pointer.
    from maple_next.opponent_intel_db.generation_store import read_current_generation

    active = read_current_generation(intel_directory)
    assert active is not None
    assert active.snapshot_path.parent == active.catalog_path.parent
    assert active.snapshot_path.read_bytes() == snapshot_path.read_bytes()
    assert active.catalog_path.read_bytes() == catalog_path.read_bytes()
