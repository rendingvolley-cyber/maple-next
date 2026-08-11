from __future__ import annotations

from pathlib import Path

from maple_next.opponent_intel_db.runtime_paths import (
    ensure_intel_db_directory,
    intel_db_directory,
    resolve_intel_runtime_root,
)


def test_explicit_arg_wins_over_everything() -> None:
    env = {
        "MAPLE_NEXT_RUNTIME_ROOT": r"C:\from-env",
        "LOCALAPPDATA": r"C:\from-localappdata",
        "USERPROFILE": r"C:\from-userprofile",
    }
    result = resolve_intel_runtime_root(r"C:\explicit", env=env)
    assert result == Path(r"C:\explicit")


def test_env_var_wins_over_localappdata_and_userprofile() -> None:
    env = {
        "MAPLE_NEXT_RUNTIME_ROOT": r"C:\from-env",
        "LOCALAPPDATA": r"C:\from-localappdata",
        "USERPROFILE": r"C:\from-userprofile",
    }
    result = resolve_intel_runtime_root(None, env=env)
    assert result == Path(r"C:\from-env")


def test_localappdata_wins_over_userprofile() -> None:
    env = {
        "LOCALAPPDATA": r"C:\from-localappdata",
        "USERPROFILE": r"C:\from-userprofile",
    }
    result = resolve_intel_runtime_root(None, env=env)
    assert result == Path(r"C:\from-localappdata") / "MapleNext" / "Battle1"


def test_userprofile_fallback_when_localappdata_missing() -> None:
    env = {"USERPROFILE": r"C:\from-userprofile"}
    result = resolve_intel_runtime_root(None, env=env)
    assert result == Path(r"C:\from-userprofile") / ".maple-next" / "Battle1"


def test_intel_db_directory_naming() -> None:
    root = Path(r"C:\some-root")
    assert intel_db_directory(root) == root / "opponent_intel_db"


def test_ensure_intel_db_directory_creates_directory(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    directory = ensure_intel_db_directory(root)
    assert directory == root / "opponent_intel_db"
    assert directory.is_dir()
