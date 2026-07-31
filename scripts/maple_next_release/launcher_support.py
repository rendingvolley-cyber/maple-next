"""Pure helpers backing the Windows official launcher.

The actual interpreter search order (``.venv\\Scripts\\python.exe`` then
``py -3.11`` then fail closed) is implemented in ``scripts/start_maple_next.ps1``
because it requires real subprocess/filesystem probing on the operator's
machine. The helpers here are the parts that are meaningfully unit-testable
on any OS: repository-root resolution from a launcher's own path, the
``.venv`` interpreter candidate path, the exact argv passed to the app, and
the timestamped log file name.
"""

from __future__ import annotations

import datetime as _datetime
import platform
from pathlib import Path


class PythonResolutionError(RuntimeError):
    """Raised by the launcher when no supported Python 3.11 interpreter is found."""


def resolve_repository_root(launcher_path: str | Path) -> Path:
    """Resolve the repository root from a launcher script's own location.

    Scripts live at ``<repository-root>/scripts/<launcher-file>``, so the
    repository root is always two levels up from the launcher file — this
    must not depend on the process current working directory.
    """
    return Path(launcher_path).resolve().parent.parent


def venv_python_candidate(repository_root: str | Path) -> Path:
    """The highest-priority interpreter candidate: the project's own venv."""
    root = Path(repository_root)
    if platform.system() == "Windows":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def build_launch_argv(
    python_executable: str | Path,
    database_path: str | Path,
    export_directory: str | Path,
) -> list[str]:
    """Build the exact argv used to launch the official entrypoint.

    Reuses the existing ``python -m maple_next --database ... --export-directory ...``
    contract from ``src/maple_next/__main__.py`` verbatim; no new CLI surface.
    """
    return [
        str(python_executable),
        "-m",
        "maple_next",
        "--database",
        str(database_path),
        "--export-directory",
        str(export_directory),
    ]


def build_log_path(logs_directory: str | Path, *, now: _datetime.datetime | None = None) -> Path:
    """Build the ``maple-next-YYYYMMDD-HHMMSS.log`` path for this run."""
    moment = now or _datetime.datetime.now()
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    return Path(logs_directory) / f"maple-next-{stamp}.log"


SECRET_LOG_MARKERS = (
    "api_key",
    "API_KEY",
    "apikey",
    "authorization:",
    "Authorization:",
)


def assert_no_secret_markers(text: str) -> None:
    """Defensive check used by tests: log content must never contain secret markers."""
    lowered = text.lower()
    for marker in SECRET_LOG_MARKERS:
        if marker.lower() in lowered:
            raise AssertionError(f"secret marker {marker!r} found in log content")
