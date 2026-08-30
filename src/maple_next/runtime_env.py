"""Repository-root runtime environment bootstrap without secret disclosure.

Maple Next reads provider configuration from ``os.environ``.  The official
Windows checkout also keeps operator-owned secrets in ``<repo>/.env``.  This
module loads only an allowlisted set from that exact file, never searches parent
directories, never overrides an already-injected process value, and never returns
or logs secret values.
"""

from __future__ import annotations

import os
from pathlib import Path

_CURRENT_KEYS = frozenset(
    {
        "MAPLE_NEXT_GEMINI_API_KEY",
        "MAPLE_NEXT_GEMINI_SELECTION_PRIMARY_MODEL",
        "MAPLE_NEXT_GEMINI_SELECTION_FALLBACK_MODEL",
        "MAPLE_NEXT_GEMINI_SELECTION_MODEL_CHAIN",
        "MAPLE_NEXT_GEMINI_TIMEOUT_SECONDS",
        "MAPLE_NEXT_GEMINI_TURN_AUTHORIZED",
        "MAPLE_NEXT_GEMINI_TURN_MODEL",
        "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_ENABLED",
        "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO",
        "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_BRANCH",
    }
)
_LEGACY_SELECTION_KEY = "MAPLE_SELECTION_ADVISOR_API_KEY"
_LEGACY_SELECTION_MODEL_CHAIN_KEY = "MAPLE_SELECTION_MODEL_CHAIN"
_LEGACY_TURN_KEY = "MAPLE_TURN_ADVISOR_API_KEY"


def _decode_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_repo_dotenv(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError, UnicodeError):
        return {}

    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        if key not in _CURRENT_KEYS | {
            _LEGACY_SELECTION_KEY,
            _LEGACY_SELECTION_MODEL_CHAIN_KEY,
            _LEGACY_TURN_KEY,
        }:
            continue
        parsed[key] = _decode_value(raw_value)
    return parsed


def bootstrap_repo_root_dotenv(repo_root: Path) -> tuple[str, ...]:
    """Load allowlisted provider values from ``repo_root/.env`` once.

    Existing process variables always win.  Legacy Maple Selection/Turn key
    names are accepted only as a fallback source for
    ``MAPLE_NEXT_GEMINI_API_KEY`` so the operator does not have to duplicate a
    credential already present in the official checkout's local ``.env``.

    The return value contains variable names only and is intended for tests or
    non-secret diagnostics.
    """

    values = _parse_repo_dotenv(repo_root.resolve() / ".env")
    loaded: list[str] = []

    current_chain = "MAPLE_NEXT_GEMINI_SELECTION_MODEL_CHAIN"
    legacy_chain = values.get(_LEGACY_SELECTION_MODEL_CHAIN_KEY, "").strip()
    if (
        legacy_chain
        and not os.environ.get(current_chain, "").strip()
        and not values.get(current_chain, "").strip()
    ):
        os.environ[current_chain] = legacy_chain
        loaded.append(current_chain)

    for key in sorted(_CURRENT_KEYS):
        value = values.get(key, "").strip()
        if value and not os.environ.get(key, "").strip():
            os.environ[key] = value
            loaded.append(key)

    target = "MAPLE_NEXT_GEMINI_API_KEY"
    if not os.environ.get(target, "").strip():
        for legacy_key in (_LEGACY_SELECTION_KEY, _LEGACY_TURN_KEY):
            value = values.get(legacy_key, "").strip()
            if value:
                os.environ[target] = value
                loaded.append(target)
                break

    return tuple(loaded)
