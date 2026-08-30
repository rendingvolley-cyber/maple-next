"""Unit tests for ``GitHubCliClient`` -- no real ``gh``/subprocess is ever invoked.

Every test injects a fake ``run`` callable, so this file never shells out to
a real process and never makes a network call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Sequence

from maple_next.feedback.github_client import ExistingFile, GitHubCliClient


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class _ScriptedRun:
    """Replays one canned result per call, recording every argv it saw."""

    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return self._results.pop(0)


class _ScriptedRunWithInput:
    """Replays one canned result per call, recording argv and the stdin body.

    Used for the PUT calls (upload_file/upsert_pointer_file), which send
    their JSON body over stdin (``gh api ... --input -``) rather than as a
    ``-f`` argv field -- see github_client.py's WinError 206 comment.
    """

    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self._results = list(results)
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv: Sequence[str], input_text: str) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), input_text))
        return self._results.pop(0)


def test_auth_status_true_on_zero_exit() -> None:
    run = _ScriptedRun([_completed(0)])
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    assert client.auth_status() is True
    assert run.calls == [["gh", "auth", "status"]]


def test_auth_status_false_on_nonzero_exit() -> None:
    run = _ScriptedRun([_completed(1)])
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    assert client.auth_status() is False


def test_auth_status_false_when_gh_missing() -> None:
    def raising_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh not found")

    client = GitHubCliClient("acme/repo", "match-feedback", run=raising_run)

    assert client.auth_status() is False


def test_ensure_branch_exists_true_when_ref_already_present() -> None:
    run = _ScriptedRun([_completed(0)])
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    assert client.ensure_branch_exists() is True
    assert run.calls == [["gh", "api", "repos/acme/repo/git/ref/heads/match-feedback"]]


def test_ensure_branch_exists_creates_ref_from_default_branch() -> None:
    run = _ScriptedRun(
        [
            _completed(1),  # ref/heads/match-feedback -- missing
            _completed(0, json.dumps({"default_branch": "main"})),
            _completed(0, json.dumps({"object": {"sha": "abc123"}})),
            _completed(0),  # create ref
        ]
    )
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    assert client.ensure_branch_exists() is True
    create_call = run.calls[-1]
    assert create_call[:3] == ["gh", "api", "repos/acme/repo/git/refs"]
    assert "ref=refs/heads/match-feedback" in create_call
    assert "sha=abc123" in create_call


def test_ensure_branch_exists_false_when_default_branch_lookup_fails() -> None:
    run = _ScriptedRun(
        [
            _completed(1),  # ref missing
            _completed(1),  # repo lookup fails too
        ]
    )
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    assert client.ensure_branch_exists() is False


def test_get_existing_file_none_on_404() -> None:
    run = _ScriptedRun([_completed(1)])
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    assert client.get_existing_file("feedback/latest.json") is None


def test_get_existing_file_decodes_base64_content() -> None:
    content = b'{"hello": "world"}'
    body = {"content": base64.b64encode(content).decode("ascii"), "sha": "deadbeef"}
    run = _ScriptedRun([_completed(0, json.dumps(body))])
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    result = client.get_existing_file("feedback/matches/2026/08/25/x.json")
    assert result == ExistingFile(sha256=hashlib.sha256(content).hexdigest(), git_sha="deadbeef")


def test_upload_file_uploads_when_absent() -> None:
    content = b'{"a": 1}'
    verify_body = {"content": base64.b64encode(content).decode("ascii"), "sha": "sha1"}
    run = _ScriptedRun(
        [
            _completed(1),  # get_existing_file -- absent
            _completed(0, json.dumps(verify_body)),  # verify
        ]
    )
    run_with_input = _ScriptedRunWithInput([_completed(0)])  # PUT
    client = GitHubCliClient(
        "acme/repo", "match-feedback", run=run, run_with_input=run_with_input
    )

    result = client.upload_file("feedback/matches/x.json", content, "match-feedback: m1")

    assert result.ok is True
    assert not result.already_present
    put_argv, put_body = run_with_input.calls[0]
    assert put_argv[:3] == ["gh", "api", "repos/acme/repo/contents/feedback/matches/x.json"]
    assert "-X" in put_argv and "PUT" in put_argv
    assert "--input" in put_argv and "-" in put_argv
    assert "-f" not in put_argv  # content never passed as a CLI argument (WinError 206)
    parsed_body = json.loads(put_body)
    assert parsed_body["branch"] == "match-feedback"
    assert parsed_body["content"] == base64.b64encode(content).decode("ascii")


def test_upload_file_no_op_when_identical_content_already_present() -> None:
    content = b'{"a": 1}'
    body = {"content": base64.b64encode(content).decode("ascii"), "sha": "sha1"}
    run = _ScriptedRun([_completed(0, json.dumps(body))])
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    result = client.upload_file("feedback/matches/x.json", content, "match-feedback: m1")

    assert result.ok is True
    assert result.already_present is True
    assert len(run.calls) == 1  # never attempts a PUT for identical content


def test_upload_file_fails_closed_on_remote_content_conflict() -> None:
    existing_body = {"content": base64.b64encode(b'{"a": 1}').decode("ascii"), "sha": "sha1"}
    run = _ScriptedRun([_completed(0, json.dumps(existing_body))])
    client = GitHubCliClient("acme/repo", "match-feedback", run=run)

    result = client.upload_file("feedback/matches/x.json", b'{"a": 2}', "match-feedback: m1")

    assert result.ok is False
    assert "CONFLICT" in result.detail
    assert len(run.calls) == 1  # never overwrites


def test_upload_file_never_raises_on_subprocess_failure() -> None:
    def raising_run_with_input(
        argv: Sequence[str], input_text: str
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.SubprocessError("boom")

    run = _ScriptedRun([_completed(1)])  # get_existing_file -- absent
    client = GitHubCliClient(
        "acme/repo", "match-feedback", run=run, run_with_input=raising_run_with_input
    )

    result = client.upload_file("feedback/matches/x.json", b"{}", "message")

    assert result.ok is False


def test_upsert_pointer_file_includes_sha_when_updating_existing() -> None:
    existing_body = {"content": base64.b64encode(b"{}").decode("ascii"), "sha": "old-sha"}
    run = _ScriptedRun([_completed(0, json.dumps(existing_body))])  # get_existing_file
    run_with_input = _ScriptedRunWithInput([_completed(0)])  # PUT
    client = GitHubCliClient(
        "acme/repo", "match-feedback", run=run, run_with_input=run_with_input
    )

    result = client.upsert_pointer_file("feedback/latest.json", b'{"x": 1}', "latest")

    assert result.ok is True
    _put_argv, put_body = run_with_input.calls[0]
    assert json.loads(put_body)["sha"] == "old-sha"


def test_upsert_pointer_file_creates_without_sha_when_absent() -> None:
    run = _ScriptedRun([_completed(1)])  # get_existing_file -- absent
    run_with_input = _ScriptedRunWithInput([_completed(0)])  # PUT
    client = GitHubCliClient(
        "acme/repo", "match-feedback", run=run, run_with_input=run_with_input
    )

    result = client.upsert_pointer_file("feedback/latest.json", b'{"x": 1}', "latest")

    assert result.ok is True
    _put_argv, put_body = run_with_input.calls[0]
    assert "sha" not in json.loads(put_body)


def test_upsert_pointer_file_never_raises_when_gh_missing() -> None:
    def raising_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh not found")

    client = GitHubCliClient("acme/repo", "match-feedback", run=raising_run)

    result = client.upsert_pointer_file("feedback/latest.json", b"{}", "latest")

    assert result.ok is False
