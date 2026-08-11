"""robots.txt gating for the opponent-intel scraper.

Every fetch made by ``downloader.py`` must pass through :class:`RobotsGate`
first. ``robots.txt`` for a given domain is fetched at most once per process
and cached in memory (a fresh process -- i.e. a fresh CLI invocation -- gets
a fresh fetch).

Fail-closed policy: permission to crawl a domain is granted *only* when
``robots.txt`` was actually retrieved, is *recognized as a valid robots
policy* (not merely "HTTP 200"), was parsed without error, and the parsed
policy explicitly (or by ordinary absence of a matching ``Disallow``)
permits the URL. Every other outcome -- an unreachable robots.txt (network
error, timeout, non-2xx status; ``Response.raise_for_status()`` in
``downloader.py``'s default fetch already turns a non-success status into an
exception here), an HTML response (login/error/interstitial page served
with HTTP 200), arbitrary non-robots text with no recognizable directives,
malformed/binary content, or a parser error -- blocks that URL. There is no
retry-with-different-user-agent, alternate-endpoint, or other
access-control-evasion behavior: a blocked domain simply isn't crawled this
run.

Recognizing "a valid robots policy" is not just "no control characters" --
HTTP 200 alone, or plain ASCII text alone, is not sufficient: a body must
either be genuinely empty/whitespace-only (the standard "no restrictions"
convention for a real, successfully-fetched robots.txt) or contain at least
one recognizable robots directive line (``User-agent:``, ``Disallow:``,
``Allow:``, ``Sitemap:``, ``Crawl-delay:``, ``Host:``). Content carrying an
HTML document signature (``<html``, ``<!doctype html``, ``<body``) is always
rejected regardless of what HTTP ``Content-Type`` header (if any) the
transport reported -- a text/plain-labeled HTML error page must not be
trusted just because of its declared content type, so this module
deliberately does not rely on ``Content-Type`` at all and instead inspects
the body itself, which is a strictly stronger check.
"""

from __future__ import annotations

import re
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

#: Body signatures that mark a response as an HTML document rather than a
#: text/plain robots policy -- checked regardless of any declared
#: Content-Type, since a misleading/incorrect Content-Type (an HTML error
#: page served as ``text/plain``) must not be trusted.
_HTML_SIGNATURE_RE = re.compile(r"<\s*(?:!doctype\s+html|html\b|body\b)", re.IGNORECASE)

#: A real robots.txt directive line. Presence of at least one of these is
#: what distinguishes "a genuine (if minimal) robots policy" from "arbitrary
#: unrelated text that happens to contain no control characters" -- e.g. a
#: plain ``hello world`` 200 response must not be treated as an empty,
#: permissive policy just because it isn't binary garbage.
_ROBOTS_DIRECTIVE_RE = re.compile(
    r"^\s*(user-agent|disallow|allow|sitemap|crawl-delay|host)\s*:", re.IGNORECASE | re.MULTILINE
)


class RobotsBodyClassification:
    """Sentinel result of :func:`_classify_robots_body`."""

    EMPTY = "EMPTY"
    VALID = "VALID"
    INVALID = "INVALID"


def _classify_robots_body(raw_text: object) -> str:
    """Classify a fetched robots.txt body before ever handing it to a parser.

    * ``EMPTY`` -- genuinely empty or whitespace/comment-only. Standard
      "no restrictions" convention for a real, successfully fetched file.
    * ``VALID`` -- non-empty, no HTML signature, no control-character/binary
      content, and contains at least one recognizable robots directive line.
    * ``INVALID`` -- everything else: not a string, HTML signature present,
      binary/control-character content, or non-empty text with zero
      recognizable directive lines (arbitrary prose, a malformed directive
      body with no valid directive names, etc).
    """

    if not isinstance(raw_text, str):
        return RobotsBodyClassification.INVALID
    if any(char in raw_text for char in _DISALLOWED_CONTROL_CHARS):
        return RobotsBodyClassification.INVALID
    if _HTML_SIGNATURE_RE.search(raw_text):
        return RobotsBodyClassification.INVALID
    stripped = raw_text.strip()
    if not stripped:
        return RobotsBodyClassification.EMPTY
    # A comment-only body (every non-blank line starts with '#') is treated
    # the same as genuinely empty -- comments carry no directives, but they
    # are unambiguously *robots.txt-shaped* content, not arbitrary prose.
    non_comment_lines = [
        line for line in stripped.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    if not non_comment_lines:
        return RobotsBodyClassification.EMPTY
    if not _ROBOTS_DIRECTIVE_RE.search(raw_text):
        return RobotsBodyClassification.INVALID
    return RobotsBodyClassification.VALID


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
            classification = _classify_robots_body(raw_text)
            if classification == RobotsBodyClassification.INVALID:
                parser = None
            else:
                # EMPTY or VALID: both are genuine, successfully-fetched
                # robots.txt content and are handed to the real parser.
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
