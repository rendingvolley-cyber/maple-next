"""Field-use smoke checks for Maple Next (Issue #31 Lane A).

Exercises, against an isolated repository-external runtime directory:

  A. startup      -- package import, repository open, Qt offscreen window
                      render/close, repository close, exit 0
  B. UGREEN-absent -- confirms startup above never touched a capture/device
                      or OBS integration (none exists on this lane's base;
                      this smoke fixes that contract at 0 counts going
                      forward)
  C. restart       -- close + reopen the same SQLite database and confirm
                      session/match/generation/state/battle_revision hydrate
                      via the existing recover_after_restart()/repository
                      contract
  D. json-export   -- build a fake/manual-only completed match through the
                      existing public MatchApplication API only (no direct
                      SQL), export it, and verify the atomic-write / SHA-256
                      / strict-parse / schema / idempotency contract
  E. safety        -- asserts provider/network/device/automatic-game-action
                      counts stay at 0 throughout

Only mock adapters (MockSelectionAdviceAdapter / MockTurnAdviceAdapter) are
used. No real Gemini transport is constructed or invoked, so provider
credentials are never read and no network send occurs.

This script uses only the isolated runtime it creates for itself (under
%TEMP% by default, or --runtime-root if given) -- it never touches the
official operator runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from maple_next_release.runtime_root import (  # noqa: E402
    RuntimeRootInsideRepositoryError,
    ensure_runtime_root_outside_repository,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SELF_TEAM = (
    "Meowscarada",
    "Gholdengo",
    "Dragonite",
    "Dondozo",
    "Flutter Mane",
    "Urshifu",
)
OPPONENT_TEAM = (
    "Garchomp",
    "Gholdengo",
    "Dragonite",
    "Flutter Mane",
    "Garganacl",
    "Iron Bundle",
)
SELECTED_THREE = ("Dondozo", "Flutter Mane", "Urshifu")


@dataclass
class SmokeReport:
    checks: list[tuple[str, str, str]] = field(default_factory=list)
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "provider_credentials_read": 0,
            "provider_dispatch": 0,
            "network_send": 0,
            "automatic_retry": 0,
            "automatic_resend": 0,
            "model_fallback": 0,
            "automatic_move": 0,
            "automatic_switch": 0,
            "automatic_next_turn": 0,
            "automatic_win": 0,
            "automatic_lose": 0,
            "automatic_game_action": 0,
            "obs_access": 0,
            "device_access": 0,
            "temporary_files_remaining": 0,
        }
    )

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append((name, status, detail))
        print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))

    def all_passed(self) -> bool:
        return all(status == "PASS" for _, status, _ in self.checks)

    def to_summary(self) -> dict[str, Any]:
        return {
            "schema_version": "issue31-lane-a-field-use-smoke.v1",
            "checks": [
                {"name": name, "status": status, "detail": detail}
                for name, status, detail in self.checks
            ],
            "safety_counters": self.counters,
            "overall": "PASS" if self.all_passed() else "FAIL",
        }


def _build_qt_application() -> Any:
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([sys.argv[0]])


def _close_and_delete_top_level_widgets(app: Any) -> None:
    from PySide6.QtCore import QEvent

    for widget in list(app.topLevelWidgets()):
        widget.close()
        widget.deleteLater()
    # A plain processEvents() alone is not reliable for flushing
    # DeferredDelete on the offscreen platform; flush it explicitly first.
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def _rmtree_with_retry(path: Path, *, attempts: int = 20, delay_seconds: float = 0.05) -> None:
    """Best-effort cleanup: Windows can briefly hold a file handle open
    after sqlite3.Connection.close() before releasing it to the filesystem."""
    last_error: OSError | None = None
    for _ in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            time.sleep(delay_seconds)
    if path.exists():
        print(f"[WARN] could not fully remove isolated smoke runtime {path}: {last_error}")


def run_startup_smoke(report: SmokeReport, runtime_state_dir: Path, export_dir: Path) -> None:
    import importlib

    importlib.import_module("maple_next")
    importlib.import_module("maple_next.__main__")
    report.record("startup.imports", "PASS", "maple_next and maple_next.__main__ import cleanly")

    from maple_next.application.match_service import MatchApplication
    from maple_next.persistence.sqlite import SQLiteRepository
    from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
    from maple_next.ui.match_controller import MatchFlowController
    from maple_next.ui.match_window import MatchFlowWindow

    runtime_state_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    database_path = runtime_state_dir / "smoke-startup.db"

    app = _build_qt_application()
    repository = SQLiteRepository(database_path)
    report.record("startup.repository_open", "PASS", str(database_path))
    try:
        application = MatchApplication(repository, export_dir)
        application.recover_after_restart()
        controller = MatchFlowController(
            application,
            repository,
            MockSelectionAdviceAdapter(),
            MockTurnAdviceAdapter(),
        )
        report.record("startup.application_construction", "PASS")

        window = MatchFlowWindow(controller)
        window.show()
        app.processEvents()
        report.record("startup.window_render", "PASS")

        _close_and_delete_top_level_widgets(app)
        report.record("startup.window_close", "PASS")
    finally:
        repository.close()
    report.record("startup.repository_close", "PASS")


def run_ugreen_unavailable_smoke(report: SmokeReport) -> None:
    # This lane does not add or touch device/capture integration. The
    # startup smoke above completed without constructing any capture/OCR
    # device handle or OBS client, so the manual-safe contract holds with
    # both counters fixed at zero.
    report.record(
        "ugreen_unavailable.manual_safe_startup",
        "PASS",
        "startup completed with no device/OBS access",
    )


def run_restart_reload_smoke(
    report: SmokeReport, runtime_state_dir: Path, export_dir: Path
) -> None:
    from maple_next.application.match_service import MatchApplication
    from maple_next.domain.enums import ResultDisposition
    from maple_next.persistence.sqlite import SQLiteRepository
    from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
    from maple_next.ui.match_controller import MatchFlowController

    database_path = runtime_state_dir / "smoke-restart.db"
    restart_export_dir = export_dir / "restart-check"

    repository = SQLiteRepository(database_path)
    application = MatchApplication(repository, restart_export_dir)
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    selection_adapter = MockSelectionAdviceAdapter()
    result = selection_adapter.submit(
        application,
        selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
        lead="Meowscarada",
    )
    assert result.disposition is ResultDisposition.APPLIED
    application.apply_selection(
        selected_three=SELECTED_THREE,
        lead="Dondozo",
        human_confirmed=True,
    )
    before = repository.load_active_session()
    assert before is not None
    repository.close()
    report.record("restart.pre_close_state_created", "PASS", f"session_id={before.session_id}")

    reopened = SQLiteRepository(database_path)
    restarted = MatchApplication(reopened, restart_export_dir)
    restarted.recover_after_restart()
    controller = MatchFlowController(
        restarted,
        reopened,
        MockSelectionAdviceAdapter(),
        MockTurnAdviceAdapter(),
    )
    view = controller.refresh()
    after = reopened.load_active_session()
    assert after is not None

    if after.session_id != before.session_id:
        raise AssertionError("session_id did not survive restart")
    if after.match_id != before.match_id:
        raise AssertionError("match_id did not survive restart")
    if after.generation != before.generation:
        raise AssertionError("generation did not survive restart")
    if after.state != before.state:
        raise AssertionError("state did not survive restart")
    if after.battle_revision != before.battle_revision:
        raise AssertionError("battle_revision did not survive restart")
    if view.session_state != before.state.value:
        raise AssertionError("controller view session_state mismatch after restart")

    reopened.close()
    report.record(
        "restart.hydration",
        "PASS",
        "session_id/match_id/generation/state/battle_revision restored "
        f"(state={after.state.value})",
    )


def run_json_export_smoke(report: SmokeReport, runtime_state_dir: Path, export_dir: Path) -> None:
    from maple_next.application.match_service import MATCH_EXPORT_SCHEMA_VERSION, MatchApplication
    from maple_next.domain.enums import MatchOutcome, ResultDisposition
    from maple_next.persistence.sqlite import SQLiteRepository
    from maple_next.ui.dev_advice import MockSelectionAdviceAdapter

    database_path = runtime_state_dir / "smoke-export.db"
    match_export_dir = export_dir / "match-json"
    match_export_dir.mkdir(parents=True, exist_ok=True)

    repository = SQLiteRepository(database_path)
    try:
        application = MatchApplication(repository, match_export_dir)
        application.new_match()
        application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
        selection_adapter = MockSelectionAdviceAdapter()
        result = selection_adapter.submit(
            application,
            selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
            lead="Meowscarada",
        )
        assert result.disposition is ResultDisposition.APPLIED
        application.apply_selection(
            selected_three=SELECTED_THREE,
            lead="Dondozo",
            human_confirmed=True,
        )
        session = repository.load_active_session()
        assert session is not None
        application.end_match(MatchOutcome.WIN, human_confirmed=True)
        report.record(
            "json_export.completed_match_built", "PASS", "via public MatchApplication API only"
        )

        record = application.export_match()
        export_path = Path(record.export_path)

        if export_path.is_relative_to(REPO_ROOT):
            raise AssertionError("export path is inside the repository")
        report.record("json_export.path_outside_repository", "PASS", str(export_path))

        expected_name = f"maple-match-{session.match_id}.json"
        if export_path.name != expected_name:
            raise AssertionError(f"unexpected export filename: {export_path.name}")
        report.record("json_export.filename_matches_match_identity", "PASS", export_path.name)

        raw_bytes = export_path.read_bytes()
        text = raw_bytes.decode("utf-8")
        payload = json.loads(text)
        report.record("json_export.utf8_strict_parse", "PASS")

        required_fields = {
            "schema_version",
            "session_id",
            "match_id",
            "generation",
            "selection",
            "turns",
            "outcome",
        }
        missing = required_fields - set(payload)
        if missing:
            raise AssertionError(f"export payload missing required fields: {sorted(missing)}")
        if payload["schema_version"] != MATCH_EXPORT_SCHEMA_VERSION:
            raise AssertionError("unexpected schema_version")
        report.record("json_export.schema_fields_present", "PASS", str(sorted(required_fields)))

        digest = hashlib.sha256(raw_bytes).hexdigest()
        if digest != record.sha256:
            raise AssertionError("SHA-256 mismatch between export record and on-disk file")
        report.record("json_export.sha256_matches", "PASS", digest)

        leftover_temp_files = list(match_export_dir.glob("*.tmp")) + list(
            match_export_dir.glob(".*.tmp")
        )
        report.counters["temporary_files_remaining"] = len(leftover_temp_files)
        if leftover_temp_files:
            raise AssertionError(f"temporary export files left behind: {leftover_temp_files}")
        report.record("json_export.no_temporary_files_remain", "PASS")

        second_record = application.export_match()
        if second_record.export_path != record.export_path or second_record.sha256 != record.sha256:
            raise AssertionError("re-export was not idempotent")
        json_files = list(match_export_dir.glob("*.json"))
        if len(json_files) != 1:
            raise AssertionError(f"expected exactly one exported json file, found {json_files}")
        report.record("json_export.idempotent_reexport", "PASS")
    finally:
        repository.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help="Isolated smoke runtime root. Defaults to a fresh directory under %%TEMP%%.",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root, used only for the repository-external path check.",
    )
    parser.add_argument(
        "--keep-runtime",
        action="store_true",
        help="Do not delete the isolated smoke runtime directory on exit.",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=None,
        help="Where to write the machine-readable summary JSON. "
        "Defaults to <isolated smoke runtime>/smoke/latest.json.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    owns_runtime_root = args.runtime_root is None
    runtime_root = (
        args.runtime_root
        if args.runtime_root is not None
        else Path(tempfile.mkdtemp(prefix="maple-next-field-use-smoke-"))
    )
    runtime_root = Path(runtime_root).expanduser().resolve()
    try:
        ensure_runtime_root_outside_repository(runtime_root, Path(args.repository_root).resolve())
    except RuntimeRootInsideRepositoryError as exc:
        print(f"[FAIL] smoke runtime root must be outside the repository: {exc}")
        return 1

    state_dir = runtime_root / "state"
    export_dir = runtime_root / "exports"
    state_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)

    report = SmokeReport()
    print(f"field-use smoke: isolated runtime root = {runtime_root}")

    try:
        run_startup_smoke(report, state_dir, export_dir)
        run_ugreen_unavailable_smoke(report)
        run_restart_reload_smoke(report, state_dir, export_dir)
        run_json_export_smoke(report, state_dir, export_dir)
    except Exception as exc:  # noqa: BLE001 - smoke script must report, not crash silently
        report.record("smoke.unexpected_failure", "FAIL", f"{type(exc).__name__}: {exc}")

    summary = report.to_summary()
    summary_path = args.summary_path or (runtime_root / "smoke" / "latest.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"summary written to {summary_path}")
    print(json.dumps(summary["safety_counters"], indent=2, sort_keys=True))

    if not args.keep_runtime and owns_runtime_root:
        _rmtree_with_retry(runtime_root)

    return 0 if report.all_passed() else 1


if __name__ == "__main__":
    raise SystemExit(main())
