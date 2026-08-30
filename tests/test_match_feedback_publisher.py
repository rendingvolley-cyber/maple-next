"""Match Feedback Loop v1: publisher/queue/service tests against a real export.

Every test here proves behavior against an export produced by the existing,
unmodified production path (``MatchApplication.export_match()``) -- no
fabricated schema. GitHub itself is always a fake/stub client; no test in
this file makes a real network call.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from maple_next.application.match_service import MatchApplication
from maple_next.domain.enums import MatchOutcome, ResultDisposition
from maple_next.domain.match_models import MatchOutcomeRecord
from maple_next.feedback.github_client import UploadResult
from maple_next.feedback.publisher import (
    build_latest_pointer_payload,
    build_remote_match_path,
    sha256_hex,
    validate_canonical_export,
)
from maple_next.feedback.queue import FeedbackConflictError, FeedbackQueue
from maple_next.feedback.service import (
    FeedbackPublishConfig,
    FeedbackPublishService,
    FeedbackStatus,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter
from maple_next.ui.match_controller import MatchFlowController

REPO_ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_PACKAGE = REPO_ROOT / "src" / "maple_next" / "feedback"

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


def _build_ended_application(
    tmp_path: Path,
) -> tuple[SQLiteRepository, MatchApplication, MatchOutcomeRecord]:
    """Real completed match through the existing production API only."""

    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir()
    export_directory = tmp_path / "user-data" / "exports"
    repository = SQLiteRepository(runtime_directory / "maple.db")
    application = MatchApplication(repository, export_directory, repository_root=repository_root)
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    result = MockSelectionAdviceAdapter().submit(
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
    outcome = application.end_match(MatchOutcome.WIN, human_confirmed=True)
    return repository, application, outcome


class _FakeGitHubClient:
    """Always-succeeding fake -- accepted by ``FeedbackPublishService`` structurally."""

    def __init__(self, repo: str, branch: str) -> None:
        self.repo = repo
        self.branch = branch
        self.upload_calls = 0
        self.pointer_calls = 0
        self.store: dict[str, bytes] = {}

    def auth_status(self) -> bool:
        return True

    def ensure_branch_exists(self) -> bool:
        return True

    def upload_file(self, path: str, content_bytes: bytes, message: str) -> UploadResult:
        self.upload_calls += 1
        existing = self.store.get(path)
        if existing is not None:
            if existing == content_bytes:
                return UploadResult(ok=True, already_present=True)
            return UploadResult(ok=False, detail="conflict")
        self.store[path] = content_bytes
        return UploadResult(ok=True)

    def upsert_pointer_file(self, path: str, content_bytes: bytes, message: str) -> UploadResult:
        self.pointer_calls += 1
        self.store[path] = content_bytes
        return UploadResult(ok=True)


class _AuthFailingClient:
    """Simulates ``gh`` being unavailable/unauthenticated. Never uploads anything."""

    def __init__(self, repo: str, branch: str) -> None:
        self.repo = repo
        self.branch = branch

    def auth_status(self) -> bool:
        return False

    def ensure_branch_exists(self) -> bool:  # pragma: no cover - must not be reached
        raise AssertionError("ensure_branch_exists must not run when auth_status() is False")

    def upload_file(self, path: str, content_bytes: bytes, message: str) -> UploadResult:
        raise AssertionError("upload_file must not run when auth_status() is False")

    def upsert_pointer_file(self, path: str, content_bytes: bytes, message: str) -> UploadResult:
        raise AssertionError("upsert_pointer_file must not run when auth_status() is False")


def _make_tracking_factory():
    created: list[_FakeGitHubClient] = []

    def factory(repo: str, branch: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(repo, branch)
        created.append(client)
        return client

    return factory, created


# --- (1)/(4) canonical export is validated, not mutated, and stays secret-free ---


def test_validate_canonical_export_returns_the_real_payload_unchanged(tmp_path: Path) -> None:
    _, application, _ = _build_ended_application(tmp_path)
    export_record = application.export_match()
    export_path = Path(export_record.export_path)
    before = export_path.read_bytes()

    payload = validate_canonical_export(before)

    assert payload["match_id"] == export_record.match_id
    assert payload["schema_version"] == export_record.schema_version
    assert export_path.read_bytes() == before  # validation never rewrites the file


def test_canonical_export_excludes_forbidden_fields(tmp_path: Path) -> None:
    _, application, _ = _build_ended_application(tmp_path)
    export_record = application.export_match()
    text = Path(export_record.export_path).read_text(encoding="utf-8")

    for forbidden in (
        '"api_key"',
        '"prompt"',
        '"provider_request"',
        '"provider_response"',
        '"authorization"',
        '"raw_request"',
        '"raw_response"',
        '"image_base64"',
        "API_KEY",
    ):
        assert forbidden not in text


# --- (2)/(7) a GitHub failure never raises and never touches the pending file ---


def test_handle_match_exported_is_pending_when_github_unavailable(tmp_path: Path) -> None:
    _, application, outcome = _build_ended_application(tmp_path)
    export_record = application.export_match()
    encoded = Path(export_record.export_path).read_bytes()

    feedback_directory = tmp_path / "feedback"
    config = FeedbackPublishConfig(enabled=True, repo="acme/repo", branch="match-feedback")
    service = FeedbackPublishService(feedback_directory, config, client_factory=_AuthFailingClient)

    status = service.handle_match_exported(
        match_id=export_record.match_id,
        ended_at_utc=outcome.ended_at_utc,
        outcome=outcome.outcome.value,
        export_path=export_record.export_path,
    )

    assert status is FeedbackStatus.PENDING
    pending_path = feedback_directory / "pending" / f"{export_record.match_id}.json"
    assert pending_path.exists()
    assert pending_path.read_bytes() == encoded
    published_path = feedback_directory / "published" / f"{export_record.match_id}.json"
    assert not published_path.exists()


def test_handle_match_exported_pending_when_not_enabled(tmp_path: Path) -> None:
    _, application, outcome = _build_ended_application(tmp_path)
    export_record = application.export_match()

    feedback_directory = tmp_path / "feedback"
    config = FeedbackPublishConfig(enabled=False, repo=None, branch="match-feedback")
    factory, created = _make_tracking_factory()
    service = FeedbackPublishService(feedback_directory, config, client_factory=factory)

    status = service.handle_match_exported(
        match_id=export_record.match_id,
        ended_at_utc=outcome.ended_at_utc,
        outcome=outcome.outcome.value,
        export_path=export_record.export_path,
    )

    assert status is FeedbackStatus.PENDING
    assert created == []  # never even constructs a GitHub client when disabled


# --- (3)/(5) deterministic path, latest.json hash, and idempotent retry ---


def test_handle_match_exported_synced_with_deterministic_path_and_latest_pointer(
    tmp_path: Path,
) -> None:
    _, application, outcome = _build_ended_application(tmp_path)
    export_record = application.export_match()
    encoded = Path(export_record.export_path).read_bytes()

    feedback_directory = tmp_path / "feedback"
    config = FeedbackPublishConfig(enabled=True, repo="acme/repo", branch="match-feedback")
    factory, created = _make_tracking_factory()
    service = FeedbackPublishService(feedback_directory, config, client_factory=factory)

    status = service.handle_match_exported(
        match_id=export_record.match_id,
        ended_at_utc=outcome.ended_at_utc,
        outcome=outcome.outcome.value,
        export_path=export_record.export_path,
    )
    assert status is FeedbackStatus.SYNCED

    expected_path = build_remote_match_path(export_record.match_id, outcome.ended_at_utc)
    assert len(created) == 1
    client = created[0]
    assert client.store[expected_path] == encoded

    pointer = json.loads(client.store["feedback/latest.json"])
    expected_pointer = build_latest_pointer_payload(
        match_id=export_record.match_id,
        ended_at_utc=outcome.ended_at_utc,
        outcome=outcome.outcome.value,
        source_schema_version=export_record.schema_version,
        match_path=expected_path,
        sha256=sha256_hex(encoded),
    )
    assert pointer == expected_pointer

    published_path = feedback_directory / "published" / f"{export_record.match_id}.json"
    assert published_path.read_bytes() == encoded
    assert not (feedback_directory / "pending" / f"{export_record.match_id}.json").exists()


def test_handle_match_exported_retry_is_idempotent(tmp_path: Path) -> None:
    _, application, outcome = _build_ended_application(tmp_path)
    export_record = application.export_match()

    feedback_directory = tmp_path / "feedback"
    config = FeedbackPublishConfig(enabled=True, repo="acme/repo", branch="match-feedback")
    factory, created = _make_tracking_factory()
    service = FeedbackPublishService(feedback_directory, config, client_factory=factory)

    kwargs = dict(
        match_id=export_record.match_id,
        ended_at_utc=outcome.ended_at_utc,
        outcome=outcome.outcome.value,
        export_path=export_record.export_path,
    )
    first = service.handle_match_exported(**kwargs)
    second = service.handle_match_exported(**kwargs)

    assert first is FeedbackStatus.SYNCED
    assert second is FeedbackStatus.SYNCED
    assert len(created) == 1  # second call never even contacts GitHub again
    assert created[0].upload_calls == 1
    assert created[0].pointer_calls == 1


# --- conflict: same match_id, different bytes -- never silently overwritten ---


def test_enqueue_pending_conflict_preserves_both_files(tmp_path: Path) -> None:
    queue = FeedbackQueue(tmp_path / "feedback")
    queue.enqueue_pending("match-1", b'{"a": 1}\n')

    with pytest.raises(FeedbackConflictError):
        queue.enqueue_pending("match-1", b'{"a": 2}\n')

    pending_dir = tmp_path / "feedback" / "pending"
    original = pending_dir / "match-1.json"
    assert original.exists()
    assert original.read_bytes() == b'{"a": 1}\n'
    conflicts = list(pending_dir.glob("match-1.conflict-*.json"))
    assert len(conflicts) == 1
    assert conflicts[0].read_bytes() == b'{"a": 2}\n'


# --- (12) the existing save_match_json flow is unaffected by feedback failures ---


def test_save_match_json_unaffected_by_feedback_failure(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir()
    export_directory = tmp_path / "user-data" / "exports"
    repository = SQLiteRepository(runtime_directory / "maple.db")
    application = MatchApplication(repository, export_directory, repository_root=repository_root)
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    result = MockSelectionAdviceAdapter().submit(
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
    application.end_match(MatchOutcome.WIN, human_confirmed=True)

    feedback_directory = tmp_path / "feedback"
    config = FeedbackPublishConfig(enabled=True, repo="acme/repo", branch="match-feedback")
    feedback_service = FeedbackPublishService(
        feedback_directory, config, client_factory=_AuthFailingClient
    )

    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        feedback_service=feedback_service,
    )
    view = controller.save_match_json()

    assert view.error_message is None
    assert view.export_path is not None
    assert view.export_sha256 is not None
    assert view.feedback_status == "Feedback: 同期待ち"


def test_save_match_json_reports_synced_status(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir()
    export_directory = tmp_path / "user-data" / "exports"
    repository = SQLiteRepository(runtime_directory / "maple.db")
    application = MatchApplication(repository, export_directory, repository_root=repository_root)
    application.new_match()
    application.confirm_selection_facts(SELF_TEAM, OPPONENT_TEAM)
    MockSelectionAdviceAdapter().submit(
        application,
        selected_three=("Meowscarada", "Gholdengo", "Dragonite"),
        lead="Meowscarada",
    )
    application.apply_selection(
        selected_three=SELECTED_THREE,
        lead="Dondozo",
        human_confirmed=True,
    )
    application.end_match(MatchOutcome.WIN, human_confirmed=True)

    feedback_directory = tmp_path / "feedback"
    config = FeedbackPublishConfig(enabled=True, repo="acme/repo", branch="match-feedback")
    factory, _created = _make_tracking_factory()
    feedback_service = FeedbackPublishService(feedback_directory, config, client_factory=factory)

    controller = MatchFlowController(
        application,
        repository,
        MockSelectionAdviceAdapter(),
        feedback_service=feedback_service,
    )
    view = controller.save_match_json()

    assert view.feedback_status == "Feedback: GitHub同期済み"


# --- (9)/(10)/(11) structural guarantees ---


def _direct_import_names(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


_NETWORK_OR_DB_MODULE_PREFIXES = (
    "subprocess",
    "socket",
    "urllib",
    "requests",
    "sqlite3",
    "maple_next.persistence",
    "maple_next.providers.turn_transport",
    "maple_next.providers.transport",
    "maple_next.ui",
    "git",
)


@pytest.mark.parametrize("relative_path", ["publisher.py", "queue.py"])
def test_validation_and_queue_modules_never_import_network_or_db(relative_path: str) -> None:
    imports = _direct_import_names(FEEDBACK_PACKAGE / relative_path)
    for name in imports:
        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in _NETWORK_OR_DB_MODULE_PREFIXES
        ), f"{relative_path} must not import {name!r}"


@pytest.mark.parametrize(
    "relative_path", ["publisher.py", "queue.py", "github_client.py", "service.py"]
)
def test_feedback_package_never_imports_sqlite_or_persistence(relative_path: str) -> None:
    imports = _direct_import_names(FEEDBACK_PACKAGE / relative_path)
    for name in imports:
        assert name != "sqlite3"
        assert not name.startswith("maple_next.persistence")


@pytest.mark.parametrize(
    "relative_path", ["publisher.py", "queue.py", "github_client.py", "service.py"]
)
def test_feedback_package_never_imports_provider_or_ocr_modules(relative_path: str) -> None:
    imports = _direct_import_names(FEEDBACK_PACKAGE / relative_path)
    for name in imports:
        assert not name.startswith("maple_next.providers")
        assert not name.startswith("maple_next.ocr")
        assert not name.startswith("maple_next.capture")


def test_github_client_only_ever_shells_out_to_gh() -> None:
    """Every ``self._run(...)``/``self._run_with_input(...)`` argv resolves to
    a literal list starting with ``"gh"``.

    Structurally guarantees this module never runs local ``git``
    checkout/switch/push against the production repository -- every write
    goes through the GitHub REST API via the ``gh`` CLI instead. Handles both
    an inline list literal (``self._run(["gh", ...])``) and a list built up
    in a local variable first (``argv = ["gh", ...]; ...; self._run(argv)``).
    """

    def first_element_is_gh(list_node: ast.List) -> bool:
        if not list_node.elts:
            return False
        first = list_node.elts[0]
        return isinstance(first, ast.Constant) and first.value == "gh"

    tree = ast.parse((FEEDBACK_PACKAGE / "github_client.py").read_text(encoding="utf-8"))
    checked = 0
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        local_list_literals: dict[str, ast.List] = {}
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        local_list_literals[target.id] = node.value
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"_run", "_run_with_input"}
            ):
                assert node.args, "self._run(...) must be called with an argv list"
                argv = node.args[0]
                if isinstance(argv, ast.List):
                    assert first_element_is_gh(argv)
                elif isinstance(argv, ast.Name):
                    resolved = local_list_literals.get(argv.id)
                    assert resolved is not None, f"could not resolve argv variable {argv.id!r}"
                    assert first_element_is_gh(resolved)
                else:  # pragma: no cover - would indicate a non-literal argv shape
                    raise AssertionError(f"unexpected argv node shape: {ast.dump(argv)}")
                checked += 1
    assert checked >= 4


# --- config: exact "1", not merely truthy ---


def test_feedback_publish_config_requires_exact_authorized_value() -> None:
    disabled = FeedbackPublishConfig.from_env({"MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_ENABLED": "true"})
    assert disabled.enabled is False

    enabled = FeedbackPublishConfig.from_env(
        {
            "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_ENABLED": "1",
            "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_REPO": "acme/repo",
            "MAPLE_NEXT_MATCH_FEEDBACK_GITHUB_BRANCH": "custom-branch",
        }
    )
    assert enabled.enabled is True
    assert enabled.repo == "acme/repo"
    assert enabled.branch == "custom-branch"
