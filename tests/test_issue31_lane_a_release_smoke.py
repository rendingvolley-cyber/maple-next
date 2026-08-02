"""Issue #31 Lane A: Windows field-use launch/release foundation tests.

Covers runtime-root resolution priority, repository-external path
guarantees, launcher command construction, and the field-use smoke script
(startup, UGREEN-unavailable manual-safe continuation, restart/reload
hydration, atomic JSON export, and zero provider/network/automatic-action
accounting). These are the tests for `scripts/` release tooling added by
this lane -- they do not modify or weaken any existing test's expectations.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from maple_next_release.launcher_support import (  # noqa: E402
    build_launch_argv,
    build_log_path,
    resolve_repository_root,
    venv_python_candidate,
)
from maple_next_release.runtime_root import (  # noqa: E402
    RuntimeLayout,
    RuntimeRootInsideRepositoryError,
    canonicalize_windows_path,
    ensure_runtime_root_outside_repository,
    resolve_runtime_root,
)


def _load_field_use_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "field_use_smoke", SCRIPTS_ROOT / "field_use_smoke.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


field_use_smoke = _load_field_use_smoke()


# ---------------------------------------------------------------------------
# Runtime root resolution priority
# ---------------------------------------------------------------------------


def test_cli_runtime_root_wins_over_everything() -> None:
    env = {
        "MAPLE_NEXT_RUNTIME_ROOT": "C:\\env-runtime",
        "LOCALAPPDATA": "C:\\Users\\op\\AppData\\Local",
        "USERPROFILE": "C:\\Users\\op",
    }
    resolved = resolve_runtime_root("C:\\cli-runtime", env=env)
    assert resolved == Path("C:\\cli-runtime")


def test_env_var_wins_over_localappdata_and_userprofile() -> None:
    env = {
        "MAPLE_NEXT_RUNTIME_ROOT": "C:\\env-runtime",
        "LOCALAPPDATA": "C:\\Users\\op\\AppData\\Local",
        "USERPROFILE": "C:\\Users\\op",
    }
    resolved = resolve_runtime_root(None, env=env)
    assert resolved == Path("C:\\env-runtime")


def test_localappdata_fallback_used_when_no_cli_or_env() -> None:
    env = {"LOCALAPPDATA": "C:\\Users\\op\\AppData\\Local", "USERPROFILE": "C:\\Users\\op"}
    resolved = resolve_runtime_root(None, env=env)
    assert resolved == Path(env["LOCALAPPDATA"]) / "MapleNext" / "Battle1"


def test_userprofile_fallback_used_when_localappdata_missing() -> None:
    env = {"USERPROFILE": "C:\\Users\\op"}
    resolved = resolve_runtime_root(None, env=env)
    assert resolved == Path("C:\\Users\\op") / ".maple-next" / "Battle1"


def test_runtime_root_resolution_handles_paths_with_spaces() -> None:
    env = {"LOCALAPPDATA": "C:\\Users\\Op Erator\\AppData\\Local"}
    resolved = resolve_runtime_root(None, env=env)
    assert "Op Erator" in str(resolved)
    assert resolved == Path(env["LOCALAPPDATA"]) / "MapleNext" / "Battle1"

    cli_resolved = resolve_runtime_root("C:\\Program Files\\Maple Runtime", env={})
    assert cli_resolved == Path("C:\\Program Files\\Maple Runtime")


# ---------------------------------------------------------------------------
# Repository-external guarantees
# ---------------------------------------------------------------------------


def test_runtime_layout_paths_are_outside_repository(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path / "runtime-root")
    assert not layout.database_path.is_relative_to(REPO_ROOT)
    assert not layout.exports_directory.is_relative_to(REPO_ROOT)
    assert not layout.logs_directory.is_relative_to(REPO_ROOT)
    assert not layout.smoke_directory.is_relative_to(REPO_ROOT)


def test_log_path_is_outside_repository(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path / "runtime-root").ensure_created()
    log_path = build_log_path(layout.logs_directory)
    assert not log_path.is_relative_to(REPO_ROOT)
    assert log_path.parent == layout.logs_directory


def test_ensure_created_does_not_delete_existing_runtime(tmp_path: Path) -> None:
    layout = RuntimeLayout(tmp_path / "runtime-root")
    layout.ensure_created()
    marker = layout.state_directory / "existing-operator-data.db"
    marker.write_text("do-not-touch", encoding="utf-8")

    layout.ensure_created()  # simulate a second launcher run

    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "do-not-touch"


# ---------------------------------------------------------------------------
# Pure Windows canonical-path helper (REWORK: repository-internal runtime
# rejection). All comparisons must be canonical (absolute, "."/".." resolved,
# separator-normalized, trailing-separator-normalized, case-insensitive) --
# never a raw string-prefix compare.
# ---------------------------------------------------------------------------

REPOSITORY_ROOT_WIN = "C:\\work\\maple-next"


def test_canonicalize_rejects_nothing_but_normalizes_dot_and_dotdot() -> None:
    assert canonicalize_windows_path("C:\\work\\maple-next\\foo\\..\\runtime") == (
        "C:\\work\\maple-next\\runtime"
    )
    assert canonicalize_windows_path("C:\\work\\.\\maple-next") == "C:\\work\\maple-next"


def test_ensure_outside_repository_rejects_repository_root_itself() -> None:
    with pytest.raises(RuntimeRootInsideRepositoryError):
        ensure_runtime_root_outside_repository(REPOSITORY_ROOT_WIN, REPOSITORY_ROOT_WIN)


def test_ensure_outside_repository_rejects_direct_child() -> None:
    with pytest.raises(RuntimeRootInsideRepositoryError):
        ensure_runtime_root_outside_repository(
            REPOSITORY_ROOT_WIN + "\\runtime", REPOSITORY_ROOT_WIN
        )


def test_ensure_outside_repository_rejects_deep_descendant() -> None:
    with pytest.raises(RuntimeRootInsideRepositoryError):
        ensure_runtime_root_outside_repository(
            REPOSITORY_ROOT_WIN + "\\state\\exports\\nested", REPOSITORY_ROOT_WIN
        )


def test_ensure_outside_repository_rejects_after_traversal_resolves_inside() -> None:
    with pytest.raises(RuntimeRootInsideRepositoryError):
        ensure_runtime_root_outside_repository(
            REPOSITORY_ROOT_WIN + "\\foo\\..\\runtime", REPOSITORY_ROOT_WIN
        )


def test_ensure_outside_repository_rejects_case_insensitive_descendant() -> None:
    with pytest.raises(RuntimeRootInsideRepositoryError):
        ensure_runtime_root_outside_repository(
            "C:\\WORK\\MAPLE-NEXT\\RUNTIME", REPOSITORY_ROOT_WIN
        )


def test_ensure_outside_repository_rejects_regardless_of_trailing_separator() -> None:
    with pytest.raises(RuntimeRootInsideRepositoryError):
        ensure_runtime_root_outside_repository(
            REPOSITORY_ROOT_WIN + "\\runtime\\", REPOSITORY_ROOT_WIN + "\\"
        )
    with pytest.raises(RuntimeRootInsideRepositoryError):
        ensure_runtime_root_outside_repository(REPOSITORY_ROOT_WIN, REPOSITORY_ROOT_WIN + "\\")


def test_ensure_outside_repository_allows_sibling_with_repository_name_as_prefix() -> None:
    # A naive string-prefix compare would incorrectly reject this: it must
    # only be rejected via canonical path + separator boundary comparison.
    result = ensure_runtime_root_outside_repository(
        "C:\\work\\maple-next-runtime", REPOSITORY_ROOT_WIN
    )
    assert result == Path("C:\\work\\maple-next-runtime")

    result2 = ensure_runtime_root_outside_repository("C:\\work\\maple-next2", REPOSITORY_ROOT_WIN)
    assert result2 == Path("C:\\work\\maple-next2")


def test_ensure_outside_repository_allows_external_path_with_spaces() -> None:
    result = ensure_runtime_root_outside_repository(
        "D:\\Maple Runtime\\Battle1", REPOSITORY_ROOT_WIN
    )
    assert result == Path("D:\\Maple Runtime\\Battle1")


def test_ensure_outside_repository_allows_different_drive() -> None:
    result = ensure_runtime_root_outside_repository("D:\\Anything", REPOSITORY_ROOT_WIN)
    assert result == Path("D:\\Anything")


# ---------------------------------------------------------------------------
# Launcher command construction
# ---------------------------------------------------------------------------


def test_repository_root_resolved_from_launcher_path_not_cwd() -> None:
    launcher_path = REPO_ROOT / "scripts" / "start_maple_next.ps1"
    assert resolve_repository_root(launcher_path) == REPO_ROOT


def test_venv_python_candidate_is_checked_first(tmp_path: Path) -> None:
    candidate = venv_python_candidate(tmp_path)
    assert candidate.parent.name in {"Scripts", "bin"}
    assert candidate.parent.parent.name == ".venv"


def test_build_launch_argv_reuses_existing_cli_contract(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "maple-next.db"
    export_directory = tmp_path / "exports"
    argv = build_launch_argv("python.exe", database_path, export_directory)
    assert argv == [
        "python.exe",
        "-m",
        "maple_next",
        "--database",
        str(database_path),
        "--export-directory",
        str(export_directory),
    ]


# ---------------------------------------------------------------------------
# Field-use smoke script
# ---------------------------------------------------------------------------


def test_ugreen_unavailable_is_non_fatal_and_zero_device_access() -> None:
    report = field_use_smoke.SmokeReport()
    field_use_smoke.run_ugreen_unavailable_smoke(report)
    assert report.all_passed()
    assert report.counters["device_access"] == 0
    assert report.counters["obs_access"] == 0


def test_startup_smoke_clean_shutdown(tmp_path: Path) -> None:
    report = field_use_smoke.SmokeReport()
    field_use_smoke.run_startup_smoke(report, tmp_path / "state", tmp_path / "exports")
    assert report.all_passed()

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    assert app is not None
    assert app.topLevelWidgets() == []


def test_restart_reload_hydration_smoke(tmp_path: Path) -> None:
    report = field_use_smoke.SmokeReport()
    field_use_smoke.run_restart_reload_smoke(report, tmp_path / "state", tmp_path / "exports")
    assert report.all_passed()


def test_json_export_atomic_write_and_strict_parse_smoke(tmp_path: Path) -> None:
    report = field_use_smoke.SmokeReport()
    field_use_smoke.run_json_export_smoke(report, tmp_path / "state", tmp_path / "exports")
    assert report.all_passed()
    assert report.counters["temporary_files_remaining"] == 0


def test_field_use_smoke_main_end_to_end_zero_side_effect_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the default (no --runtime-root) path: main() owns and
    # auto-deletes the isolated temp runtime it creates for itself, but
    # redirect tempfile.mkdtemp() under tmp_path so we can assert cleanup
    # without touching the real %TEMP%.
    created: dict[str, str] = {}
    original_mkdtemp = field_use_smoke.tempfile.mkdtemp

    def fake_mkdtemp(*, prefix: str) -> str:
        path = original_mkdtemp(prefix=prefix, dir=str(tmp_path))
        created["path"] = path
        return path

    monkeypatch.setattr(field_use_smoke.tempfile, "mkdtemp", fake_mkdtemp)

    summary_path = tmp_path / "summary-out" / "latest.json"
    exit_code = field_use_smoke.main(
        ["--repository-root", str(REPO_ROOT), "--summary-path", str(summary_path)]
    )

    assert exit_code == 0
    assert summary_path.is_file()

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["overall"] == "PASS"
    for counter_name, value in summary["safety_counters"].items():
        assert value == 0, f"expected {counter_name} to be 0, got {value}"

    # main() deletes the isolated runtime it created (default behavior),
    # but must never touch the repository or the externally-provided summary path.
    assert "path" in created
    assert not Path(created["path"]).exists()
    assert summary_path.exists()


def test_field_use_smoke_main_preserves_caller_supplied_runtime_root(tmp_path: Path) -> None:
    # When the caller passes an explicit --runtime-root, main() must not
    # assume ownership and must not delete it -- only the self-created
    # default temp directory is auto-cleaned.
    explicit_runtime = tmp_path / "caller-owned-runtime"

    exit_code = field_use_smoke.main(
        ["--runtime-root", str(explicit_runtime), "--repository-root", str(REPO_ROOT)]
    )

    assert exit_code == 0
    assert explicit_runtime.exists()


def test_field_use_smoke_refuses_runtime_root_inside_repository() -> None:
    inside_repo_runtime = REPO_ROOT / "tmp-smoke-runtime-should-be-rejected"
    exit_code = field_use_smoke.main(
        [
            "--runtime-root",
            str(inside_repo_runtime),
            "--repository-root",
            str(REPO_ROOT),
        ]
    )
    assert exit_code == 1
    assert not inside_repo_runtime.exists()


# ---------------------------------------------------------------------------
# Windows-only subprocess launcher smoke (explicit skip elsewhere)
# ---------------------------------------------------------------------------

_WINDOWS_ONLY = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="scripts/start_maple_next.cmd is a Windows batch/PowerShell launcher",
)


@_WINDOWS_ONLY
def test_windows_launcher_smoke_mode_exits_zero(tmp_path: Path) -> None:
    launcher = REPO_ROOT / "scripts" / "start_maple_next.cmd"
    runtime_root = tmp_path / "windows-launcher-smoke-runtime"
    env = dict(os.environ)
    env["MAPLE_NEXT_RUNTIME_ROOT"] = str(runtime_root)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    completed = subprocess.run(
        [str(launcher), "--smoke"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary_path = runtime_root / "smoke" / "latest.json"
    assert summary_path.is_file()


def _assert_no_runtime_artifacts(runtime_root: Path) -> None:
    assert not runtime_root.exists(), f"forbidden runtime artifacts created at {runtime_root}"


@_WINDOWS_ONLY
def test_windows_launcher_rejects_explicit_repository_internal_runtime_root() -> None:
    launcher = REPO_ROOT / "scripts" / "start_maple_next.cmd"
    forbidden_runtime = REPO_ROOT / "tmp-lane-a-forbidden-explicit-runtime"
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        completed = subprocess.run(
            [str(launcher), "--runtime-root", str(forbidden_runtime)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert completed.returncode != 0
        _assert_no_runtime_artifacts(forbidden_runtime)
    finally:
        if forbidden_runtime.exists():
            shutil.rmtree(forbidden_runtime)


@_WINDOWS_ONLY
def test_windows_launcher_rejects_env_repository_internal_runtime_root() -> None:
    launcher = REPO_ROOT / "scripts" / "start_maple_next.cmd"
    forbidden_runtime = REPO_ROOT / "tmp-lane-a-forbidden-env-runtime"
    env = dict(os.environ)
    env["MAPLE_NEXT_RUNTIME_ROOT"] = str(forbidden_runtime)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        completed = subprocess.run(
            [str(launcher), "--smoke"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert completed.returncode != 0
        _assert_no_runtime_artifacts(forbidden_runtime)
    finally:
        if forbidden_runtime.exists():
            shutil.rmtree(forbidden_runtime)


@_WINDOWS_ONLY
def test_windows_launcher_allows_external_runtime_root_with_spaces(tmp_path: Path) -> None:
    launcher = REPO_ROOT / "scripts" / "start_maple_next.cmd"
    runtime_root = tmp_path / "Maple Runtime" / "Battle1"
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    completed = subprocess.run(
        [str(launcher), "--smoke", "--runtime-root", str(runtime_root)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary_path = runtime_root / "smoke" / "latest.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["overall"] == "PASS"
    for counter_name in ("provider_dispatch", "network_send", "automatic_game_action"):
        assert summary["safety_counters"][counter_name] == 0


@pytest.mark.parametrize(
    "argv_builder",
    [
        lambda root: ["--smoke", "--runtime-root", str(root)],
        lambda root: ["--runtime-root", str(root), "--smoke"],
    ],
    ids=["smoke-then-runtime-root", "runtime-root-then-smoke"],
)
@_WINDOWS_ONLY
def test_windows_launcher_accepts_both_argument_orders(tmp_path, argv_builder) -> None:
    launcher = REPO_ROOT / "scripts" / "start_maple_next.cmd"
    runtime_root = tmp_path / "arg-order-runtime"
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    completed = subprocess.run(
        [str(launcher), *argv_builder(runtime_root)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary_path = runtime_root / "smoke" / "latest.json"
    assert summary_path.is_file()
