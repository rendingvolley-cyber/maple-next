"""Negative tests: raw/untrusted provider payload must never persist.

Covers the 00-canonical security repair (Issue #31 comment 5156676848):
``JobStore.audit_result`` must never write ``ResultEnvelope.payload`` (or any
dynamic exception text derived from it) into ``async_job_results``, for
Selection and Turn, across every disposition (APPLIED / INVALID_REJECTED /
STALE_REJECTED / DUPLICATE_IGNORED / unknown-job). Historical rows written
before this repair must be scrubbed by idempotent startup sanitation.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from maple_next.application.service import BattleApplication
from maple_next.domain.enums import ActionType, ResultDisposition
from maple_next.persistence.schema import migrate
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.workers.contracts.models import JobEnvelope, ResultEnvelope

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPP_TEAM = ("Garchomp", "Gholdengo", "Dragonite", "Flutter Mane", "Garganacl", "Iron Bundle")
SELECTED_THREE = ("Dondozo", "Flutter Mane", "Urshifu")

SENSITIVE_ECHO_MARKER = "SENSITIVE_ECHO_MARKER_9f3a2b7c"


def _all_audit_rows(repo: SQLiteRepository) -> list[tuple[str, str, str, str]]:
    rows = repo.connection.execute(
        "SELECT job_id, disposition, reason, payload_json FROM async_job_results"
    ).fetchall()
    return [
        (str(r["job_id"]), str(r["disposition"]), str(r["reason"]), str(r["payload_json"]))
        for r in rows
    ]


def _assert_marker_absent_everywhere(repo: SQLiteRepository) -> None:
    for job_id, disposition, reason, payload_json in _all_audit_rows(repo):
        assert SENSITIVE_ECHO_MARKER not in job_id
        assert SENSITIVE_ECHO_MARKER not in disposition
        assert SENSITIVE_ECHO_MARKER not in reason
        assert SENSITIVE_ECHO_MARKER not in payload_json
        assert payload_json == "{}"


def _selection_setup(tmp_path: Path) -> tuple[SQLiteRepository, BattleApplication, JobEnvelope]:
    repo = SQLiteRepository(tmp_path / "maple.db")
    app = BattleApplication(repo)
    app.new_match()
    app.confirm_selection_facts(SELF_TEAM, OPP_TEAM)
    job = app.request_selection_advice("human-command-1")
    return repo, app, job


def _valid_selection_result(job: JobEnvelope, **changes: object) -> ResultEnvelope:
    base = ResultEnvelope(
        contract_version="maple-worker.v1",
        result_id=str(uuid4()),
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation,
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id=job.input_snapshot_id,
        request_payload_hash=job.request_payload_hash,
        payload={
            "selected_three": ["Meowscarada", "Gholdengo", "Dragonite"],
            "lead": "Meowscarada",
        },
    )
    return replace(base, **changes)


def _turn_setup(tmp_path: Path) -> tuple[SQLiteRepository, BattleApplication, JobEnvelope]:
    repo = SQLiteRepository(tmp_path / "maple.db")
    app = BattleApplication(repo)
    app.new_match()
    app.confirm_selection_facts(SELF_TEAM, OPP_TEAM)
    result = _valid_selection_result(app.request_selection_advice("human-command-1"))
    assert app.apply_selection_advice_result(result) is ResultDisposition.APPLIED
    app.apply_selection(
        selected_three=SELECTED_THREE,
        lead="Dondozo",
        human_confirmed=True,
    )
    app.start_turn_capture()
    from maple_next.domain.enums import HpBucket

    app.confirm_turn_facts(
        self_active="Dondozo",
        opponent_active="Garchomp",
        self_hp=HpBucket.FULL,
        opponent_hp=HpBucket.EIGHTY_ONE_TO_NINETY,
        legal_moves=("Protect", "Wave Crash", "Earthquake"),
        legal_switches=("Flutter Mane", "Urshifu"),
        human_note="manual review",
        human_confirmed=True,
    )
    job = app.request_turn_advice("turn-command-1")
    return repo, app, job


def _valid_turn_result(job: JobEnvelope, **changes: object) -> ResultEnvelope:
    payload = {
        "recommended_action": {
            "action_id": f"{ActionType.MOVE.value}:Protect",
            "action_type": ActionType.MOVE.value,
            "action_name": "Protect",
        },
        "reasons": ["Scout before committing."],
        "warnings": [],
        "opponent_prediction": {
            "category": "UNKNOWN",
            "predicted_action": "Earthquake",
            "summary": "Earthquake",
            "confidence": 0.5,
        },
    }
    base = ResultEnvelope(
        contract_version=job.contract_version,
        result_id=str(uuid4()),
        job_id=job.job_id,
        command_id=job.command_id,
        job_type=job.job_type,
        session_id=job.session_id,
        match_id=job.match_id,
        generation=job.generation,
        turn_number=job.turn_number,
        base_battle_revision=job.base_battle_revision,
        expected_state=job.expected_state,
        input_snapshot_id=job.input_snapshot_id,
        request_payload_hash=job.request_payload_hash,
        payload=payload,
        source_type="MOCK",
        model="mock-dev",
    )
    return replace(base, **changes)


def test_turn_invalid_payload_marker_not_persisted(tmp_path: Path) -> None:
    repo, app, job = _turn_setup(tmp_path)
    result = _valid_turn_result(job)
    result.payload["recommended_action"]["action_name"] = "Illegal Move"
    result.payload["reasons"] = [SENSITIVE_ECHO_MARKER]

    disposition = app.apply_turn_advice_result(result)

    assert disposition is ResultDisposition.INVALID_REJECTED
    audits = repo.result_audits(job.job_id)
    assert audits == [("INVALID_REJECTED", "INVALID_PAYLOAD")]
    _assert_marker_absent_everywhere(repo)


def test_turn_stale_payload_marker_not_persisted(tmp_path: Path) -> None:
    repo, app, job = _turn_setup(tmp_path)
    result = _valid_turn_result(job, base_battle_revision=999)
    result.payload["reasons"] = [SENSITIVE_ECHO_MARKER]

    disposition = app.apply_turn_advice_result(result)

    assert disposition is ResultDisposition.STALE_REJECTED
    _assert_marker_absent_everywhere(repo)


def test_selection_invalid_payload_marker_not_persisted(tmp_path: Path) -> None:
    repo, app, job = _selection_setup(tmp_path)
    result = _valid_selection_result(
        job,
        payload={
            "selected_three": [SENSITIVE_ECHO_MARKER, "Gholdengo", "Dragonite"],
            "lead": SENSITIVE_ECHO_MARKER,
        },
    )

    disposition = app.apply_selection_advice_result(result)

    assert disposition is ResultDisposition.INVALID_REJECTED
    audits = repo.result_audits(job.job_id)
    assert audits == [("INVALID_REJECTED", "INVALID_PAYLOAD")]
    _assert_marker_absent_everywhere(repo)


def test_unknown_job_payload_marker_not_persisted(tmp_path: Path) -> None:
    repo, app, job = _selection_setup(tmp_path)
    result = _valid_selection_result(
        job,
        job_id="unknown-job-xyz",
        payload={
            "selected_three": ["Meowscarada", "Gholdengo", "Dragonite"],
            "lead": SENSITIVE_ECHO_MARKER,
        },
    )

    disposition = app.apply_selection_advice_result(result)

    assert disposition is ResultDisposition.STALE_REJECTED
    audits = repo.result_audits("unknown-job-xyz")
    assert audits == [("STALE_REJECTED", "JOB_ID_MISMATCH")]
    _assert_marker_absent_everywhere(repo)


def test_applied_audit_row_has_no_raw_payload(tmp_path: Path) -> None:
    repo, app, job = _turn_setup(tmp_path)
    result = _valid_turn_result(job)

    disposition = app.apply_turn_advice_result(result)

    assert disposition is ResultDisposition.APPLIED
    audits = repo.result_audits(job.job_id)
    assert audits == [("APPLIED", "BINDING_ACCEPTED")]
    turn_audit_rows = [row for row in _all_audit_rows(repo) if row[0] == job.job_id]
    assert len(turn_audit_rows) == 1
    assert turn_audit_rows[0][3] == "{}"
    assert "Protect" not in turn_audit_rows[0][3]
    assert "Earthquake" not in turn_audit_rows[0][3]


def test_applied_canonical_advice_store_has_only_allowlist_fields(tmp_path: Path) -> None:
    """APPLIED writes go through the fixed canonical allowlist, not a raw payload dump.

    ``turn_advices`` must never gain a column that stores the full provider
    payload verbatim; every column is a specific extracted/derived field.
    """

    repo, app, job = _turn_setup(tmp_path)
    result = _valid_turn_result(job)

    disposition = app.apply_turn_advice_result(result)
    assert disposition is ResultDisposition.APPLIED

    columns = {
        str(row[1]) for row in repo.connection.execute("PRAGMA table_info(turn_advices)")
    }
    assert columns == {
        "turn_advice_id",
        "session_id",
        "turn_id",
        "turn_number",
        "job_id",
        "input_snapshot_id",
        "action_type",
        "action_name",
        "opponent_prediction",
        "rationale",
        "is_mock",
        "source_type",
        "model",
        "warnings_json",
        "created_at",
    }
    assert not any("payload" in column for column in columns)

    row = repo.connection.execute(
        "SELECT action_type, action_name, opponent_prediction, rationale FROM turn_advices"
        " WHERE job_id = ?",
        (job.job_id,),
    ).fetchone()
    assert row is not None
    assert row["action_type"] == "MOVE"
    assert row["action_name"] == "Protect"
    assert row["opponent_prediction"] == "Earthquake"
    assert row["rationale"] == "Scout before committing."

    turn_audit_rows = [r for r in _all_audit_rows(repo) if r[0] == job.job_id]
    assert turn_audit_rows == [(job.job_id, "APPLIED", "BINDING_ACCEPTED", "{}")]


def test_historical_marker_row_is_scrubbed_by_migration(tmp_path: Path) -> None:
    repo, _app, job = _selection_setup(tmp_path)
    repo.connection.execute(
        """
        INSERT INTO async_job_results (
            result_id, job_id, disposition, reason, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            job.job_id,
            "INVALID_REJECTED",
            "INVALID_PAYLOAD",
            f'{{"leaked": "{SENSITIVE_ECHO_MARKER}"}}',
            "2020-01-01T00:00:00+00:00",
        ),
    )
    repo.connection.commit()
    before_scrub = _all_audit_rows(repo)
    assert any(SENSITIVE_ECHO_MARKER in row[3] for row in before_scrub)

    migrate(repo.connection)

    _assert_marker_absent_everywhere(repo)


def test_second_migration_is_idempotent_after_scrub(tmp_path: Path) -> None:
    repo, _app, job = _selection_setup(tmp_path)
    repo.connection.execute(
        """
        INSERT INTO async_job_results (
            result_id, job_id, disposition, reason, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            job.job_id,
            "STALE_REJECTED",
            "JOB_ID_MISMATCH",
            f'{{"leaked": "{SENSITIVE_ECHO_MARKER}"}}',
            "2020-01-01T00:00:00+00:00",
        ),
    )
    repo.connection.commit()

    migrate(repo.connection)
    first_pass = _all_audit_rows(repo)

    migrate(repo.connection)
    second_pass = _all_audit_rows(repo)

    assert first_pass == second_pass
    _assert_marker_absent_everywhere(repo)
