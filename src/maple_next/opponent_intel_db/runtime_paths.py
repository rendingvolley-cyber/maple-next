"""Runtime root resolution for the opponent-intel local database.

Resolution priority (highest first) mirrors
``scripts/maple_next_release/runtime_root.py::resolve_runtime_root`` exactly,
including the LOCALAPPDATA/USERPROFILE fallback behavior:

  1. an explicit ``cli_runtime_root`` (the CLI's ``--runtime-root``)
  2. the ``MAPLE_NEXT_RUNTIME_ROOT`` environment variable
  3. ``%LOCALAPPDATA%\\MapleNext\\Battle1``
  4. ``%USERPROFILE%\\.maple-next\\Battle1`` (used when LOCALAPPDATA is unset)

This module intentionally does not import ``scripts.maple_next_release`` --
this package must remain independent of the ``scripts/`` tree so it can be
packaged/imported on its own. The logic is duplicated on purpose; if the
release-launcher resolution order ever changes, this module must be updated
to match it deliberately, not implicitly via a shared import.
"""

from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ROOT_ENV_VAR = "MAPLE_NEXT_RUNTIME_ROOT"
APP_DIRECTORY_NAME = "MapleNext"
PROFILE_DIRECTORY_NAME = "Battle1"
INTEL_DB_DIRECTORY_NAME = "opponent_intel_db"


def resolve_intel_runtime_root(
    cli_runtime_root: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the runtime root using the documented priority order above."""

    environment = env if env is not None else os.environ

    if cli_runtime_root is not None:
        return Path(cli_runtime_root).expanduser()

    configured = environment.get(RUNTIME_ROOT_ENV_VAR)
    if configured:
        return Path(configured).expanduser()

    local_appdata = environment.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / APP_DIRECTORY_NAME / PROFILE_DIRECTORY_NAME

    user_profile = environment.get("USERPROFILE")
    home = Path(user_profile) if user_profile else Path.home()
    return home / ".maple-next" / PROFILE_DIRECTORY_NAME


def intel_db_directory(root: Path) -> Path:
    """Return the (not-necessarily-existing) opponent-intel-db directory under ``root``."""

    return root / INTEL_DB_DIRECTORY_NAME


def ensure_intel_db_directory(root: Path) -> Path:
    """Create (if needed) and return the opponent-intel-db directory under ``root``."""

    directory = intel_db_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory
