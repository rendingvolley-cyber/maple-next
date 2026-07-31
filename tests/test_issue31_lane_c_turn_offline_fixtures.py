"""Network-zero, secret-zero, and golden-value guarantees for the Turn Advice contract.

These tests assert the offline nature of the whole Lane C contract
(``turn_request``, ``turn_response``, ``turn_validation``, ``turn_boundary``)
rather than any single module.
"""

from __future__ import annotations

import inspect
import json
import os
import socket
import urllib.request

import pytest

import maple_next.providers.turn_boundary as turn_boundary
import maple_next.providers.turn_request as turn_request
import maple_next.providers.turn_response as turn_response
import maple_next.providers.turn_validation as turn_validation
from maple_next.providers.turn_boundary import (
    build_turn_advice_prompt,
    build_turn_provider_request_body,
)
from maple_next.providers.turn_request import (
    canonical_request_dict,
    compute_reviewed_snapshot_hash,
    encode_canonical_request,
    request_payload_hash,
)
from maple_next.providers.turn_validation import (
    TurnAdviceResultCode,
    build_normalized_turn_advice_result,
    parse_turn_advice_body,
    validate_turn_advice_result,
)
from tests.fixtures.turn_advice import (
    TRUSTED_MODEL,
    TRUSTED_SOURCE_TYPE,
    VALID_PROVIDER_TEXT,
    build_sample_request,
    golden_values,
)

_LANE_C_MODULES = (turn_request, turn_response, turn_validation, turn_boundary)


def _run_full_offline_suite() -> None:
    """Exercises builder + parser + validator + fixtures end to end."""

    request = build_sample_request()
    encode_canonical_request(request)
    canonical_request_dict(request)
    request_payload_hash(request)
    compute_reviewed_snapshot_hash(request.reviewed_snapshot)
    build_turn_advice_prompt(request)
    build_turn_provider_request_body(request)

    body = parse_turn_advice_body(VALID_PROVIDER_TEXT)
    result = build_normalized_turn_advice_result(
        request=request,
        body=body,
        request_payload_hash_value=request_payload_hash(request),
        source_type=TRUSTED_SOURCE_TYPE,
        model=TRUSTED_MODEL,
    )
    assert validate_turn_advice_result(request, result) is TurnAdviceResultCode.VALID


# --- network-zero guard ------------------------------------------------------


def test_network_zero_guard_full_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blow_up_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("socket.socket must never be constructed by Lane C code")

    def _blow_up_urlopen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("urllib.request.urlopen must never be called by Lane C code")

    monkeypatch.setattr(socket, "socket", _blow_up_socket)
    monkeypatch.setattr(urllib.request, "urlopen", _blow_up_urlopen)

    _run_full_offline_suite()


# --- API-key / env guard -----------------------------------------------------


def test_no_api_key_or_env_access_during_full_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    real_getenv = os.getenv

    def _blow_up_getenv(key: str, *args: object, **kwargs: object) -> object:
        raise AssertionError(f"os.getenv must never be called by Lane C code (key={key!r})")

    monkeypatch.setattr(os, "getenv", _blow_up_getenv)
    # os.environ.get is a bound method of a dict subclass; patching the
    # class method is intrusive, so instead we assert (below, statically)
    # that none of the Lane C modules reference os.environ at all, and here
    # we only guard the more common os.getenv call surface.
    try:
        _run_full_offline_suite()
    finally:
        monkeypatch.setattr(os, "getenv", real_getenv)


def test_lane_c_modules_never_call_os_getenv_or_os_environ_statically() -> None:
    """Static-source guard: no module imports dotenv or touches os.getenv/os.environ."""

    for module in _LANE_C_MODULES:
        source = inspect.getsource(module)
        assert "os.getenv" not in source
        assert "os.environ" not in source
        assert "dotenv" not in source
        assert "import os" not in source


def test_lane_c_modules_never_open_dotenv_files_statically() -> None:
    for module in _LANE_C_MODULES:
        source = inspect.getsource(module)
        assert ".env" not in source


def test_lane_c_modules_have_zero_network_imports_statically() -> None:
    for module in _LANE_C_MODULES:
        source = inspect.getsource(module)
        for forbidden in ("import socket", "import urllib", "import requests", "import httpx"):
            assert forbidden not in source


# --- golden values ------------------------------------------------------------


def test_golden_canonical_request_bytes() -> None:
    request = build_sample_request()
    assert encode_canonical_request(request) == golden_values.ENCODED_BYTES


def test_golden_request_payload_hash() -> None:
    request = build_sample_request()
    assert request_payload_hash(request) == golden_values.REQUEST_HASH


def test_golden_reviewed_snapshot_hash() -> None:
    request = build_sample_request()
    assert compute_reviewed_snapshot_hash(request.reviewed_snapshot) == golden_values.SNAPSHOT_HASH


def test_golden_prompt_string() -> None:
    request = build_sample_request()
    assert build_turn_advice_prompt(request) == golden_values.PROMPT


def test_golden_provider_request_body() -> None:
    request = build_sample_request()
    body = build_turn_provider_request_body(request)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert encoded == golden_values.BODY_JSON
