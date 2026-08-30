"""``.env`` bootstrap must actually load the match-feedback GitHub keys.

Adding a new env var name to a spec/README is not enough on its own --
``bootstrap_repo_root_dotenv`` only ever loads names present in its own
``_CURRENT_KEYS`` allowlist (``src/maple_next/runtime_env.py``). This proves
the three ``MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_*`` keys are in that allowlist
and actually flow from a repository-root ``.env`` into ``os.environ`` (and,
from there, into ``FeedbackPublishConfig.from_env()``), the same way the
existing Gemini keys already do.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from maple_next.feedback.service import FeedbackPublishConfig
from maple_next.runtime_env import bootstrap_repo_root_dotenv

_FEEDBACK_ENV_KEYS = (
    "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_ENABLED",
    "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO",
    "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_BRANCH",
)


@pytest.fixture(autouse=True)
def _clean_environment():
    previous = {key: os.environ.get(key) for key in _FEEDBACK_ENV_KEYS}
    for key in _FEEDBACK_ENV_KEYS:
        os.environ.pop(key, None)
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_bootstrap_loads_feedback_keys_from_repo_root_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_ENABLED=1\n"
        "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO=acme/repo\n"
        "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_BRANCH=match-feedback\n",
        encoding="utf-8",
    )

    loaded = bootstrap_repo_root_dotenv(tmp_path)

    assert set(_FEEDBACK_ENV_KEYS).issubset(loaded)
    assert os.environ["MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_ENABLED"] == "1"
    assert os.environ["MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO"] == "acme/repo"
    assert os.environ["MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_BRANCH"] == "match-feedback"

    config = FeedbackPublishConfig.from_env()
    assert config == FeedbackPublishConfig(
        enabled=True, repo="acme/repo", branch="match-feedback"
    )


def test_bootstrap_never_overrides_an_existing_process_value(tmp_path: Path) -> None:
    os.environ["MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO"] = "already-set/repo"
    (tmp_path / ".env").write_text(
        "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO=from-dotenv/repo\n",
        encoding="utf-8",
    )

    bootstrap_repo_root_dotenv(tmp_path)

    assert os.environ["MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO"] == "already-set/repo"
