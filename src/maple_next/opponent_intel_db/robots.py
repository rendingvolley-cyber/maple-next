"""robots.txt gating for the opponent-intel scraper.

Every fetch made by ``downloader.py`` must pass through :class:`RobotsGate`
first. ``robots.txt`` for a given domain is fetched at most once per process
and cached in memory (a fresh process -- i.e. a fresh CLI invocation -- gets
a fresh fetch).

Fail-closed policy: permission to crawl a domain is granted *only* when
``robots.txt`` was actually retrieved, looks like genuine robots.txt content,
was parsed without error, and the parsed policy explicitly (or by ordinary
absence of a matching ``Disallow``) permits the URL. Every other outcome --
an unreachable robots.txt (network error, timeout, non-2xx status), content
that doesn't look like a parseable robots policy, a parser error, or an
explicit ``Disallow`` -- blocks that URL. There is no retry-with-different-
user-agent, alternate-endpoint, or other access-control-evasion behavior: a
blocked domain simply isn't crawled this run.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

DEFAULT_USER_AGENT = "MapleNextOpponentIntelBot"

FetchText = Callable[[str], str]

#: Control characters (other than plain whitespace) that never appear in a
#: genuine text/plain robots.txt -- their presence indicates the response
#: body is binary garbage or an unrelated payload, not an unparseable-but-
#: real robots policy.
_DISALLOWED_CONTROL_CHARS = frozenset(
    chr(code) for code in range(0, 32) if chr(code) not in "\t\n\r"
)


def _looks_like_robots_policy(raw_text: str) -> bool:
    """Reject content that cannot plausibly be a robots.txt policy.

    A genuinely empty (or comment-only/whitespace-only) body is treated as a
    valid, permissive policy per the standard robots.txt convention -- this
    check only rejects content that is clearly *not* text-based robots.txt
    material (binary payloads, control-character garbage).
    """

    if not isinstance(raw_text, str):
        return False
    return not any(char in raw_text for char in _DISALLOWED_CONTROL_CHARS)


class RobotsGate:
    """Fetch-once, cache-for-process-lifetime, fail-closed robots.txt checker."""

    def __init__(self, fetch: FetchText) -> None:
        self._fetch = fetch
        # ``None`` cached for a domain means "permission could not be
        # established -- always block", so repeated calls in the same
        # process don't re-attempt the fetch.
        self._parsers: dict[str, RobotFileParser | None] = {}

    def _robots_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    def _parser_for(self, url: str) -> RobotFileParser | None:
        robots_url = self._robots_url(url)
        if robots_url in self._parsers:
            return self._parsers[robots_url]

        parser: RobotFileParser | None
        try:
            raw_text = self._fetch(robots_url)
        except Exception:
            # Unreachable robots.txt (network error, timeout, non-2xx status,
            # etc.) -- fail closed rather than assuming "allow".
            parser = None
        else:
            if not _looks_like_robots_policy(raw_text):
                parser = None
            else:
                candidate = RobotFileParser()
                candidate.set_url(robots_url)
                try:
                    candidate.parse(raw_text.splitlines())
                except Exception:
                    parser = None
                else:
                    parser = candidate

        self._parsers[robots_url] = parser
        return parser

    def is_allowed(self, url: str, user_agent: str = DEFAULT_USER_AGENT) -> bool:
        """Whether ``user_agent`` may fetch ``url``, per fail-closed policy.

        Returns ``False`` whenever permission cannot be affirmatively
        established from a successfully fetched and parsed robots.txt --
        never treats an unreachable, unparseable, or malformed policy as
        permission to crawl.
        """

        parser = self._parser_for(url)
        if parser is None:
            return False
        return parser.can_fetch(user_agent, url)
