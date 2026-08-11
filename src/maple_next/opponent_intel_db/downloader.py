"""Rate-limited, robots.txt-respecting HTML downloader.

Everything network-facing in this package flows through
:class:`SnapshotDownloader`. It is deliberately injectable (the ``fetch``
callable) so tests can exercise rate limiting, robots.txt gating, and retry
behavior without ever touching the real network.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

from maple_next.opponent_intel_db.robots import DEFAULT_USER_AGENT, RobotsGate

FetchText = Callable[[str], str]

#: Honest, non-impersonating identification string sent on every request.
USER_AGENT = (
    f"{DEFAULT_USER_AGENT}/1.0 "
    "(+local offline usage-stats snapshot tool; run manually before battle "
    "sessions; contact: rendingvolley@gmail.com)"
)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MIN_INTERVAL_SECONDS = 1.5
MAX_RETRIES = 1  # i.e. at most one retry after the first attempt fails


class DownloadError(Exception):
    """Raised when a URL cannot be fetched (network failure or robots.txt disallow)."""


def _requests_fetch(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.text


@dataclass
class SnapshotDownloader:
    """Fetch HTML pages, honoring rate limits and robots.txt.

    ``fetch`` defaults to a ``requests``-backed GET; tests should inject a
    fake to avoid real network access. ``sleep`` defaults to
    ``time.sleep`` and is likewise injectable so tests stay fast.
    """

    fetch: FetchText = field(default=_requests_fetch)
    sleep: Callable[[float], None] = field(default=time.sleep)
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    now: Callable[[], float] = field(default=time.monotonic)

    def __post_init__(self) -> None:
        self._robots_gate = RobotsGate(self.fetch)
        self._last_request_at: float | None = None
        self._fetched_urls: set[str] = set()
        self._page_cache_store: dict[str, str] = {}

    def _respect_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = self.now() - self._last_request_at
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            self.sleep(remaining)

    def get(self, url: str) -> str:
        """Fetch ``url`` as text, respecting robots.txt, rate limits, and retries.

        Never fetches the same URL twice within the lifetime of this
        downloader instance -- a repeat call returns a cached result instead
        of re-hitting the network.
        """

        if url in self._page_cache:
            return self._page_cache[url]

        if not self._robots_gate.is_allowed(url, self.user_agent):
            raise DownloadError(f"ROBOTS_DISALLOWED:{url}")

        self._respect_rate_limit()

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                text = self.fetch(url)
                break
            except Exception as exc:  # noqa: BLE001 - deliberately broad, retried once
                last_error = exc
                if attempt < MAX_RETRIES:
                    continue
                raise DownloadError(f"FETCH_FAILED:{url}") from exc
        else:  # pragma: no cover - loop always returns or raises
            raise DownloadError(f"FETCH_FAILED:{url}") from last_error

        self._last_request_at = self.now()
        self._fetched_urls.add(url)
        self._page_cache[url] = text
        return text

    @property
    def _page_cache(self) -> dict[str, str]:
        return self._page_cache_store

    @property
    def fetched_urls(self) -> frozenset[str]:
        return frozenset(self._fetched_urls)
