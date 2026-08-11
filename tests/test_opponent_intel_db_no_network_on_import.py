"""Asserts the opponent-intel-db package never touches the network unless the
explicit ``update-opponent-intel`` CLI command is invoked.

Import time (and constructing objects, without calling ``cli.main()``) must
never perform an HTTP request -- this is the structural guarantee that keeps
network access out of the battle UI/runtime path.
"""

from __future__ import annotations

import pytest


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def blow_up(*args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected network call during import/construction")

    monkeypatch.setattr(requests, "get", blow_up)
    monkeypatch.setattr(requests.Session, "get", blow_up)
    monkeypatch.setattr(requests.Session, "request", blow_up)


def test_importing_cli_module_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)

    import importlib

    import maple_next.opponent_intel_db.cli as cli_module

    importlib.reload(cli_module)  # re-executes module body under the network guard


def test_constructing_core_objects_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)

    from maple_next.opponent_intel_db.downloader import SnapshotDownloader
    from maple_next.opponent_intel_db.robots import RobotsGate

    downloader = SnapshotDownloader()
    assert downloader is not None

    gate = RobotsGate(fetch=lambda url: "")
    assert gate is not None


def test_importing_all_submodules_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)

    import maple_next.opponent_intel_db  # noqa: F401
    import maple_next.opponent_intel_db.downloader  # noqa: F401
    import maple_next.opponent_intel_db.move_catalog_builder  # noqa: F401
    import maple_next.opponent_intel_db.normalize  # noqa: F401
    import maple_next.opponent_intel_db.parser_champs_pokedb  # noqa: F401
    import maple_next.opponent_intel_db.parser_pokechamdb  # noqa: F401
    import maple_next.opponent_intel_db.robots  # noqa: F401
    import maple_next.opponent_intel_db.runtime_paths  # noqa: F401
    import maple_next.opponent_intel_db.snapshot_store  # noqa: F401
