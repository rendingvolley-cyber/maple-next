"""Fail-closed robots.txt policy matrix for ``RobotsGate``.

Every scenario here must resolve to "blocked" except a genuinely fetched,
parseable, permissive/explicitly-allowing policy. No network is used --
``fetch`` is always a fake callable.
"""

from __future__ import annotations

import pytest

from maple_next.opponent_intel_db.robots import RobotsGate

URL = "https://example.test/pokemon/garchomp"

ROBOTS_TXT_ALLOW_ALL = "User-agent: *\nAllow: /\n"
ROBOTS_TXT_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def test_robots_allow_permits_the_url() -> None:
    gate = RobotsGate(lambda url: ROBOTS_TXT_ALLOW_ALL)
    assert gate.is_allowed(URL) is True


def test_robots_explicit_disallow_blocks_the_url() -> None:
    gate = RobotsGate(lambda url: ROBOTS_TXT_DISALLOW_ALL)
    assert gate.is_allowed(URL) is False


def test_robots_unreachable_http_error_blocks_the_url() -> None:
    def fetch(url: str) -> str:
        raise RuntimeError("HTTP 404 Not Found")

    gate = RobotsGate(fetch)
    assert gate.is_allowed(URL) is False


def test_robots_timeout_blocks_the_url() -> None:
    def fetch(url: str) -> str:
        raise TimeoutError("robots.txt fetch timed out")

    gate = RobotsGate(fetch)
    assert gate.is_allowed(URL) is False


def test_robots_connection_error_blocks_the_url() -> None:
    def fetch(url: str) -> str:
        raise ConnectionError("robots.txt connection refused")

    gate = RobotsGate(fetch)
    assert gate.is_allowed(URL) is False


def test_robots_malformed_binary_content_blocks_the_url() -> None:
    # Control-character/binary garbage cannot plausibly be a text robots.txt
    # policy -- an HTML error page or binary payload served with a 200 status
    # falls here.
    gate = RobotsGate(lambda url: "\x00\x01\x02BINARY-NOT-A-POLICY\x03")
    assert gate.is_allowed(URL) is False


def test_robots_unparseable_policy_blocks_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.robotparser import RobotFileParser

    def _raise_parse(self: RobotFileParser, lines: list[str]) -> None:
        raise ValueError("simulated unparseable robots policy")

    monkeypatch.setattr(RobotFileParser, "parse", _raise_parse)
    gate = RobotsGate(lambda url: ROBOTS_TXT_ALLOW_ALL)
    assert gate.is_allowed(URL) is False


def test_robots_empty_body_with_successful_fetch_is_permissive() -> None:
    # A genuinely fetched, empty robots.txt (HTTP 200, empty body) is the
    # standard "no restrictions" convention, distinct from "unreachable".
    gate = RobotsGate(lambda url: "")
    assert gate.is_allowed(URL) is True


def test_robots_failure_is_cached_and_never_refetched() -> None:
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        raise RuntimeError("robots.txt unreachable")

    gate = RobotsGate(fetch)
    assert gate.is_allowed(URL) is False
    assert gate.is_allowed(URL) is False
    assert calls == ["https://example.test/robots.txt"]
