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


def test_robots_html_200_body_blocks_the_url() -> None:
    # An HTML error/interstitial page served with HTTP 200 must not be
    # treated as an empty, permissive robots.txt.
    html_body = (
        "<!DOCTYPE html><html><head><title>Error</title></head>"
        "<body>Not Found</body></html>"
    )
    gate = RobotsGate(lambda url: html_body)
    assert gate.is_allowed(URL) is False


def test_robots_html_disguised_as_text_plain_blocks_the_url() -> None:
    # Same HTML body -- this module never trusts a declared Content-Type
    # (which our fetch abstraction doesn't even carry), only the body
    # itself, so a text/plain-labeled HTML page is caught identically.
    html_body = "<html><body>please sign in</body></html>"
    gate = RobotsGate(lambda url: html_body)
    assert gate.is_allowed(URL) is False


def test_robots_arbitrary_hello_world_text_blocks_the_url() -> None:
    # Non-empty, non-HTML, non-binary -- but contains zero recognizable
    # robots directives, so it must not be treated as an empty/permissive
    # policy just because it "looks like plain text".
    gate = RobotsGate(lambda url: "hello world\nthis is not a robots policy at all\n")
    assert gate.is_allowed(URL) is False


def test_robots_malformed_directive_body_blocks_the_url() -> None:
    # Colon-separated lines that resemble directives but use no recognized
    # directive name.
    gate = RobotsGate(lambda url: "foo: bar\nbaz: qux\nlorem: ipsum\n")
    assert gate.is_allowed(URL) is False


def test_robots_comment_only_body_is_treated_as_empty_permissive() -> None:
    # A comment-only body is unambiguously robots.txt-shaped (just carries
    # no directives) -- treated the same as genuinely empty, not rejected
    # as "arbitrary text".
    gate = RobotsGate(lambda url: "# nothing to see here\n# just comments\n")
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
