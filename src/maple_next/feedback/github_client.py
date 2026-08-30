"""GitHub Contents/Refs API access via the authenticated ``gh`` CLI.

This is the only module in ``maple_next.feedback`` that touches
``subprocess``/network. No GitHub token is ever read, stored, or printed by
this module -- authorization is entirely delegated to the caller's own
``gh auth login`` session. Every method catches its own subprocess/parsing
failures and returns a typed result instead of raising, so a GitHub outage
can never propagate into the match-export flow. Every write goes through the
GitHub Contents/Refs REST API (``gh api ...``) -- this module never runs
``git`` against the local working tree, and never runs any checkout/switch/
pull/push subcommand against the production repository.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

RunFunc = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
RunWithInputFunc = Callable[[Sequence[str], str], "subprocess.CompletedProcess[str]"]


def _default_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # ``gh`` always emits UTF-8 -- decode explicitly rather than via
    # ``text=True``'s locale-dependent default, which on a non-UTF-8 Windows
    # console codepage (e.g. cp932) raises UnicodeDecodeError on ordinary
    # ``gh`` output.
    return subprocess.run(
        list(argv),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _default_run_with_input(
    argv: Sequence[str], input_text: str
) -> subprocess.CompletedProcess[str]:
    # A real multi-turn match export's base64-encoded content is easily
    # ~100KB+ -- passed as a single ``-f content=...`` argv element this
    # exceeds Windows' ~32KB command-line length limit (WinError 206,
    # "the filename or extension is too long"), which surfaces as an opaque
    # OSError from CreateProcess. Every write that carries file content
    # therefore sends the whole JSON request body over stdin instead
    # (``gh api ... --input -``), which has no such limit.
    return subprocess.run(
        list(argv),
        input=input_text,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


@dataclass(frozen=True, slots=True)
class ExistingFile:
    sha256: str
    git_sha: str


@dataclass(frozen=True, slots=True)
class UploadResult:
    ok: bool
    already_present: bool = False
    detail: str = ""


class GitHubPublishClient(Protocol):
    """The narrow interface :mod:`maple_next.feedback.service` depends on."""

    def auth_status(self) -> bool: ...

    def ensure_branch_exists(self) -> bool: ...

    def upload_file(self, path: str, content_bytes: bytes, message: str) -> UploadResult: ...

    def upsert_pointer_file(
        self, path: str, content_bytes: bytes, message: str
    ) -> UploadResult: ...


class GitHubCliClient:
    """Thin, fail-closed wrapper around ``gh api`` for one repo/branch pair."""

    def __init__(
        self,
        repo: str,
        branch: str,
        *,
        run: RunFunc = _default_run,
        run_with_input: RunWithInputFunc = _default_run_with_input,
    ) -> None:
        self._repo = repo
        self._branch = branch
        self._run = run
        self._run_with_input = run_with_input

    def auth_status(self) -> bool:
        try:
            result = self._run(["gh", "auth", "status"])
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _default_branch_sha(self) -> str | None:
        try:
            repo_info = self._run(["gh", "api", f"repos/{self._repo}"])
            if repo_info.returncode != 0:
                return None
            default_branch = json.loads(repo_info.stdout)["default_branch"]
            ref = self._run(
                ["gh", "api", f"repos/{self._repo}/git/ref/heads/{default_branch}"]
            )
            if ref.returncode != 0:
                return None
            return str(json.loads(ref.stdout)["object"]["sha"])
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError):
            return None

    def ensure_branch_exists(self) -> bool:
        """Create ``branch`` from the repo's default branch if it doesn't exist yet.

        Only ever creates a ref via the API (``gh api .../git/refs``) -- never
        runs local ``git checkout``/``git switch``, and never touches the
        local working tree at all.
        """

        try:
            existing = self._run(
                ["gh", "api", f"repos/{self._repo}/git/ref/heads/{self._branch}"]
            )
            if existing.returncode == 0:
                return True
            base_sha = self._default_branch_sha()
            if base_sha is None:
                return False
            created = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{self._repo}/git/refs",
                    "-f",
                    f"ref=refs/heads/{self._branch}",
                    "-f",
                    f"sha={base_sha}",
                ]
            )
            return created.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def get_existing_file(self, path: str) -> ExistingFile | None:
        try:
            # ``ref`` is embedded in the URL query string rather than passed
            # as ``-f ref=...`` -- ``gh api`` silently switches its default
            # HTTP method from GET to POST whenever any ``-f``/``-F`` flag is
            # present (unless ``-X`` is given explicitly), which would send
            # this read as a POST against the read-only contents endpoint.
            result = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{self._repo}/contents/{path}?ref={self._branch}",
                ]
            )
            if result.returncode != 0:
                return None
            body = json.loads(result.stdout)
            content = base64.b64decode(body["content"])
            return ExistingFile(
                sha256=hashlib.sha256(content).hexdigest(),
                git_sha=str(body["sha"]),
            )
        except (
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ):
            return None

    def upload_file(self, path: str, content_bytes: bytes, message: str) -> UploadResult:
        """Upload an immutable match file. Never overwrites existing content.

        If the remote file already exists with identical bytes, this is a
        successful no-op (idempotent retry). If it exists with different
        bytes, this fails closed rather than overwriting -- callers surface
        that as a conflict, matching the local queue's same rule.
        """

        try:
            expected_sha256 = hashlib.sha256(content_bytes).hexdigest()
            existing = self.get_existing_file(path)
            if existing is not None:
                if existing.sha256 == expected_sha256:
                    return UploadResult(ok=True, already_present=True, detail="unchanged")
                return UploadResult(ok=False, detail="FEEDBACK_REMOTE_CONTENT_CONFLICT")

            encoded_content = base64.b64encode(content_bytes).decode("ascii")
            body = json.dumps(
                {"message": message, "content": encoded_content, "branch": self._branch}
            )
            result = self._run_with_input(
                ["gh", "api", f"repos/{self._repo}/contents/{path}", "-X", "PUT", "--input", "-"],
                body,
            )
            if result.returncode != 0:
                return UploadResult(ok=False, detail="FEEDBACK_UPLOAD_FAILED")

            verify = self.get_existing_file(path)
            if verify is None or verify.sha256 != expected_sha256:
                return UploadResult(ok=False, detail="FEEDBACK_UPLOAD_VERIFY_FAILED")
            return UploadResult(ok=True)
        except (OSError, subprocess.SubprocessError):
            return UploadResult(ok=False, detail="FEEDBACK_UPLOAD_EXCEPTION")

    def upsert_pointer_file(self, path: str, content_bytes: bytes, message: str) -> UploadResult:
        """Create or overwrite a mutable pointer file (``feedback/latest.json``).

        Unlike :meth:`upload_file` (immutable match files, never overwritten),
        this always writes the given bytes -- reserved for the one pointer
        file that is meant to change on every match.
        """

        try:
            existing = self.get_existing_file(path)
            encoded_content = base64.b64encode(content_bytes).decode("ascii")
            body_fields: dict[str, str] = {
                "message": message,
                "content": encoded_content,
                "branch": self._branch,
            }
            if existing is not None:
                body_fields["sha"] = existing.git_sha
            result = self._run_with_input(
                ["gh", "api", f"repos/{self._repo}/contents/{path}", "-X", "PUT", "--input", "-"],
                json.dumps(body_fields),
            )
            if result.returncode != 0:
                return UploadResult(ok=False, detail="FEEDBACK_POINTER_UPDATE_FAILED")
            return UploadResult(ok=True)
        except (OSError, subprocess.SubprocessError):
            return UploadResult(ok=False, detail="FEEDBACK_POINTER_UPDATE_EXCEPTION")
