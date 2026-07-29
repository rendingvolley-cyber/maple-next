from __future__ import annotations

import os
import socket
import urllib.error
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QApplication

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import BattleState, JobStatus, JobType
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.providers.selection_request import build_selection_advice_request
from maple_next.providers.transport import (
    GeminiSelectionAdviceTransport,
    ProviderConfig,
    ProviderTransportError,
)
from maple_next.ui.controller import SelectionFlowController
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.gemini_advice import GeminiSelectionAdviceAdapter

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")


def _request() -> object:
    return build_selection_advice_request(
        session_id="session-1",
        match_id="match-1",
        generation=1,
        battle_revision=1,
        reviewed_selection_id="reviewed-1",
        self_team=SELF_TEAM,
        opponent_team=OPPONENT_TEAM,
    )


def _patch_urlopen_raises(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    import maple_next.providers.transport as transport_module

    def raise_it(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(transport_module.urllib.request, "urlopen", raise_it)


class _RaisingOnReadResponse:
    """A urlopen() context-manager result whose .read() itself times out."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __enter__(self) -> _RaisingOnReadResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        raise self._exc


# --- 1/2/3. production urllib exception shapes classify as GEMINI_TIMEOUT ---


def test_bare_timeout_error_from_urlopen_is_gemini_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen_raises(monkeypatch, TimeoutError("timed out"))
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError, match="^GEMINI_TIMEOUT$"):
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]


def test_url_error_wrapping_timeout_error_is_gemini_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen_raises(
        monkeypatch, urllib.error.URLError(TimeoutError("connect timed out"))
    )
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError, match="^GEMINI_TIMEOUT$"):
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]


def test_url_error_wrapping_socket_timeout_is_gemini_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # In Python 3.11, socket.timeout is TimeoutError itself (unified in 3.10),
    # so this is the same isinstance check exercised by a distinct spelling.
    assert socket.timeout is TimeoutError
    _patch_urlopen_raises(
        monkeypatch, urllib.error.URLError(socket.timeout("timed out"))  # noqa: UP041
    )
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError, match="^GEMINI_TIMEOUT$"):
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]


def test_direct_timeout_error_from_response_read_is_gemini_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import maple_next.providers.transport as transport_module

    monkeypatch.setattr(
        transport_module.urllib.request,
        "urlopen",
        lambda *_a, **_k: _RaisingOnReadResponse(TimeoutError("timed out")),
    )
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError, match="^GEMINI_TIMEOUT$"):
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]


# --- 4. non-timeout URLError still classifies as GEMINI_NETWORK_ERROR -------


def test_non_timeout_url_error_is_gemini_network_error_not_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen_raises(monkeypatch, urllib.error.URLError(OSError("offline")))
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError) as excinfo:
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert message.startswith("GEMINI_NETWORK_ERROR:")
    assert message != "GEMINI_TIMEOUT"


# --- 5. HTTPError still classifies as GEMINI_HTTP_ERROR, never timeout ------


def test_http_error_is_gemini_http_error_not_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    http_error = urllib.error.HTTPError(
        "https://example.invalid", 500, "Internal Server Error", None, None
    )
    _patch_urlopen_raises(monkeypatch, http_error)
    transport = GeminiSelectionAdviceTransport()
    with pytest.raises(ProviderTransportError, match="^GEMINI_HTTP_ERROR:500$"):
        transport.send(_request(), ProviderConfig(api_key="k"))  # type: ignore[arg-type]


# --- 6. end-to-end through the application: wrapped timeout -> TIMED_OUT ----


def qt_application() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([])


class _SyncDispatch:
    """Same-thread stand-in: calls transport.send() synchronously, no QThread.

    Exercises only *which thread the test itself runs on*; the production
    GeminiSelectionAdviceTransport code path under test is unchanged. Real
    cross-thread dispatch is covered elsewhere (test_gemini_selection_advice_
    window.py) and is out of scope for this timeout-classification rework.
    """

    def __init__(
        self,
        transport: GeminiSelectionAdviceTransport,
        request: object,
        config: ProviderConfig,
        *,
        on_succeeded: object,
        on_failed: object,
    ) -> None:
        self._transport = transport
        self._request = request
        self._config = config
        self._on_succeeded = on_succeeded
        self._on_failed = on_failed

    def start(self) -> None:
        try:
            result = self._transport.send(self._request, self._config)  # type: ignore[arg-type]
        except ProviderTransportError as exc:
            self._on_failed(str(exc))  # type: ignore[operator]
        else:
            self._on_succeeded(result)  # type: ignore[operator]


def test_wrapped_urlerror_timeout_ends_as_timed_out_job_with_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qt_application()
    _patch_urlopen_raises(
        monkeypatch, urllib.error.URLError(TimeoutError("connect timed out"))
    )

    repository = SQLiteRepository(tmp_path / "maple.db")
    application = BattleApplication(repository)
    transport = GeminiSelectionAdviceTransport()
    gemini_adapter = GeminiSelectionAdviceAdapter(
        transport,
        lambda: ProviderConfig(api_key="test-key", model="test-model"),
        dispatch_factory=_SyncDispatch,  # type: ignore[arg-type]
    )
    controller = SelectionFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
        gemini_adapter=gemini_adapter,
    )
    controller.new_match()
    view = controller.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    assert view.error_message is None
    before = repository.load_active_session()
    assert before is not None

    results: list[object] = []
    controller.send_selection_advice_to_gemini(on_result=results.append)

    assert len(results) == 1  # exactly one attempt, resolved synchronously
    after = repository.load_active_session()
    assert after is not None
    assert after.state is BattleState.SELECTION_OPEN
    assert after.current_selection_advice_id is None
    assert after.battle_revision == before.battle_revision
    assert after.current_reviewed_selection_id == before.current_reviewed_selection_id

    job = repository.latest_job_by_type(after.session_id, JobType.SELECTION_ADVICE)
    assert job is not None
    assert job.status is JobStatus.TIMED_OUT

    view = controller.refresh()
    assert view.error_message is not None
    repository.close()
