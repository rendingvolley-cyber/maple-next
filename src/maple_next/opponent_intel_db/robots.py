"""robots.txt gating for the opponent-intel scraper.

Every fetch made by ``downloader.py`` must pass through :class:`RobotsGate`
first. ``robots.txt`` for a given domain is fetched at most once per process
and cached in memory (a fresh process -- i.e. a fresh CLI invocation -- gets
a fresh fetch).

Behavior when ``robots.txt`` itself cannot be fetched (network error, 404,
non-200 status, etc.): this module does **not** treat that as "disallow
everything". It falls through to Python's stdlib
``urllib.robotparser.RobotFileParser`` default behavior for an unparsed/empty
rule set, which is to allow. This matches the common real-world convention
that a missing/unreachable robots.txt does not forbid crawling. It does NOT
relax anything a robots.txt that *was* successfully fetched and parsed
actually disallows -- an explicit ``Disallow`` rule is always honored.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

DEFAULT_USER_AGENT = "MapleNextOpponentIntelBot"

FetchText = Callable[[str], str]


class RobotsGate:
    """Fetch-once, cache-for-process-lifetime robots.txt checker."""

    def __init__(self, fetch: FetchText) -> None:
        self._fetch = fetch
        self._parsers: dict[str, RobotFileParser] = {}

    def _robots_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    def _parser_for(self, url: str) -> RobotFileParser:
        robots_url = self._robots_url(url)
        cached = self._parsers.get(robots_url)
        if cached is not None:
            return cached

        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            raw_text = self._fetch(robots_url)
        except Exception:
            # Unreachable robots.txt: leave the parser with no rules loaded.
            # RobotFileParser.can_fetch() on an empty rule set defaults to
            # allow, which is the documented behavior of this module.
            parser.parse([])
        else:
            parser.parse(raw_text.splitlines())

        self._parsers[robots_url] = parser
        return parser

    def is_allowed(self, url: str, user_agent: str = DEFAULT_USER_AGENT) -> bool:
        """Return whether ``user_agent`` may fetch ``url`` per the domain's robots.txt."""

        parser = self._parser_for(url)
        return parser.can_fetch(user_agent, url)
