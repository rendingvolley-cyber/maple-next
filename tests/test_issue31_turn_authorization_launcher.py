"""Official Windows launcher propagation for Turn provider authorization."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "scripts" / "start_maple_next.ps1"

_WINDOWS_ONLY = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="official launcher is PowerShell on Windows",
)


def _build_launcher_environment_probe(tmp_path: Path, *, authorized: bool) -> tuple[Path, Path]:
    probe_repo = tmp_path / ("authorized-repo" if authorized else "unauthorized-repo")
    scripts = probe_repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(LAUNCHER, scripts / LAUNCHER.name)

    probe_path = tmp_path / ("authorized.txt" if authorized else "unauthorized.txt")
    (scripts / "field_use_smoke.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['MAPLE_TEST_PROBE_PATH']).write_text(\n"
        "    'authorized' if os.environ.get('MAPLE_NEXT_GEMINI_TURN_AUTHORIZED') == '1' "
        "else 'unauthorized', encoding='utf-8')\n",
        encoding="utf-8",
    )

    target_venv = probe_repo / ".venv"
    (target_venv / "Scripts").mkdir(parents=True)
    base_python = Path(sys.executable)
    (target_venv / "pyvenv.cfg").write_text(
        f"home = {sys.base_prefix}\n"
        "include-system-site-packages = false\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n"
        f"executable = {base_python}\n",
        encoding="utf-8",
    )
    shutil.copy2(base_python, target_venv / "Scripts" / "python.exe")
    python_dll = Path(sys.base_prefix) / (
        f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    )
    shutil.copy2(python_dll, target_venv / "Scripts" / python_dll.name)
    if authorized:
        (probe_repo / ".env").write_text(
            "MAPLE_NEXT_GEMINI_TURN_AUTHORIZED=1\n", encoding="utf-8"
        )
    return scripts / LAUNCHER.name, probe_path


@_WINDOWS_ONLY
@pytest.mark.parametrize("authorized", [True, False], ids=["dotenv-authorized", "unset"])
def test_launcher_propagates_turn_authorization_to_python_runtime(
    tmp_path: Path, authorized: bool
) -> None:
    launcher, probe_path = _build_launcher_environment_probe(
        tmp_path, authorized=authorized
    )
    runtime_root = tmp_path / ("authorized-runtime" if authorized else "unauthorized-runtime")
    env = dict(os.environ)
    env.pop("MAPLE_NEXT_GEMINI_TURN_AUTHORIZED", None)
    env["MAPLE_TEST_PROBE_PATH"] = str(probe_path)

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            "-Smoke",
            "-RuntimeRoot",
            str(runtime_root),
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert probe_path.read_text(encoding="utf-8") == (
        "authorized" if authorized else "unauthorized"
    )
    assert "MAPLE_NEXT_GEMINI_TURN_AUTHORIZED" not in completed.stdout
    assert "MAPLE_NEXT_GEMINI_TURN_AUTHORIZED" not in completed.stderr
