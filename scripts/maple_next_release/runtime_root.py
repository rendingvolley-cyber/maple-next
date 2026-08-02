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

import ntpath
import os
from pathlib import Path

RUNTIME_ROOT_ENV_VAR = "MAPLE_NEXT_RUNTIME_ROOT"
APP_DIRECTORY_NAME = "MapleNext"
PROFILE_DIRECTORY_NAME = "Battle1"


class RuntimeRootInsideRepositoryError(ValueError):
    """Raised when a runtime root resolves to the repository itself or a descendant.

    Carries the canonicalized (Windows-normalized) paths so callers can build
    a fail-closed diagnostic without recomputing the comparison.
    """

    def __init__(self, runtime_root: str, repository_root: str) -> None:
        self.runtime_root = runtime_root
        self.repository_root = repository_root
        super().__init__(
            "runtime root must be outside repository: "
            f"resolved repository path={repository_root!r} "
            f"resolved runtime path={runtime_root!r}"
        )


def canonicalize_windows_path(path: str | Path) -> str:
    """Canonicalize a path using Windows path semantics, independent of host OS.

    Applies absolute-path resolution, ``.``/``..`` resolution, and separator
    normalization via :mod:`ntpath` (rather than the host OS's own path
    module) so the same comparison logic behaves identically whether this
    runs on the real Windows launcher or on Linux CI. Case is preserved here;
    callers that need case-insensitive comparison must fold case themselves.
    """
    raw = os.fspath(path)
    normalized = ntpath.normpath(raw)
    if not ntpath.isabs(normalized):
        normalized = ntpath.normpath(ntpath.abspath(normalized))
    return normalized


def ensure_runtime_root_outside_repository(
    runtime_root: str | Path,
    repository_root: str | Path,
) -> Path:
    """Fail closed if ``runtime_root`` is the repository root or a descendant.

    Comparison is case-insensitive and prefix-based only after canonicalizing
    both paths (never a raw string-prefix compare), so a sibling directory
    that merely starts with the repository's name (e.g. ``maple-next-runtime``
    next to ``maple-next``) is correctly treated as outside the repository.
    """
    canonical_runtime = canonicalize_windows_path(runtime_root)
    canonical_repository = canonicalize_windows_path(repository_root)

    runtime_fold = canonical_runtime.lower()
    repository_fold = canonical_repository.lower().rstrip("\\") + "\\"
    repository_fold_bare = repository_fold[:-1]

    is_same = runtime_fold == repository_fold_bare
    is_descendant = runtime_fold.startswith(repository_fold)

    if is_same or is_descendant:
        raise RuntimeRootInsideRepositoryError(canonical_runtime, canonical_repository)

    return Path(canonical_runtime)


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
