"""Injectable Turn Advice provider transport boundary — fake/injected only.

Distinct from :mod:`maple_next.providers.transport` (the Selection lane,
which does have a production ``GeminiSelectionAdviceTransport``): Issue #31
explicitly forbids ever sending a real Turn Advice request to a live
provider. This module therefore intentionally contains **no** production
HTTP transport class — only the shared ``ProviderConfig``/``SanitizedProviderResult``
types (imported from :mod:`maple_next.providers.transport`) and a
test/dev-only fake transport that a human explicitly seeds with content
before every send. Nothing in this module imports SQLite, the repository,
or any PySide6 widget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from maple_next.providers.transport import ProviderConfig, SanitizedProviderResult
from maple_next.providers.turn_request import TurnAdviceRequest

#: Turn Advice never has a production Gemini lane in this codebase. Any
#: source_type string is permitted through this fake transport, but the
#: adapter that owns it (see maple_next.ui.gemini_turn_advice) always uses a
#: model name that is clearly distinguishable from a real Gemini model.
FAKE_TURN_ADVICE_SOURCE_TYPE = "GEMINI"


class ProviderTransportError(RuntimeError):
    """Raised for fake-transport failures. Never includes secrets."""


class TurnProviderTransport(Protocol):
    """Injectable transport boundary. Only fake/injected implementations exist."""

    def send(
        self, request: TurnAdviceRequest, config: ProviderConfig
    ) -> SanitizedProviderResult: ...


@dataclass
class FakeTurnAdviceTransport:
    """Fake/injected transport. Records every call; never touches the network.

    A human (via the UI adapter) or a test enqueues the exact
    :class:`SanitizedProviderResult` (or exception) to return next, then
    triggers exactly one trusted send. This is the Turn-side analog of
    :class:`maple_next.providers.transport.FakeSelectionAdviceTransport`.
    """

    responses: list[SanitizedProviderResult | Exception] = field(default_factory=list)
    calls: list[tuple[TurnAdviceRequest, ProviderConfig]] = field(default_factory=list)

    def send(
        self, request: TurnAdviceRequest, config: ProviderConfig
    ) -> SanitizedProviderResult:
        self.calls.append((request, config))
        if not self.responses:
            raise ProviderTransportError("FAKE_TRANSPORT_NO_RESPONSE_CONFIGURED")
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def call_count(self) -> int:
        return len(self.calls)
