"""Runtime root resolution for the Maple Next Windows field-use launcher.

Resolution priority (highest first):
  1. an explicit ``cli_runtime_root`` (the launcher's ``--runtime-root``)
  2. the ``MAPLE_NEXT_RUNTIME_ROOT`` environment variable
  3. ``%LOCALAPPDATA%\\MapleNext\\Battle1``
  4. ``%USERPROFILE%\\.maple-next\\Battle1`` (used when LOCALAPPDATA is unset)

The resolved root is always kept outside the repository/worktree so state,
exports, logs, and smoke artifacts never land in version control.
"""

from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ROOT_ENV_VAR = "MAPLE_NEXT_RUNTIME_ROOT"
APP_DIRECTORY_NAME = "MapleNext"
PROFILE_DIRECTORY_NAME = "Battle1"


def resolve_runtime_root(
    cli_runtime_root: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the official runtime root using the documented priority order."""
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


class RuntimeLayout:
    """The fixed subdirectory layout under a resolved runtime root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.state_directory = self.root / "state"
        self.exports_directory = self.root / "exports"
        self.logs_directory = self.root / "logs"
        self.smoke_directory = self.root / "smoke"

    @property
    def database_path(self) -> Path:
        return self.state_directory / "maple-next.db"

    def ensure_created(self) -> RuntimeLayout:
        """Create every runtime subdirectory. Never touches existing files."""
        for directory in (
            self.state_directory,
            self.exports_directory,
            self.logs_directory,
            self.smoke_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self
