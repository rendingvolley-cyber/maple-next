from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _json_or_none(value: object) -> object | None:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"parse_error": True, "raw_length": len(str(value))}


def main() -> int:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA_NOT_SET")
    db_path = Path(local_app_data) / "MapleNext" / "Battle1" / "state" / "maple-next.db"
    if not db_path.exists():
        raise RuntimeError(f"PRODUCTION_DB_NOT_FOUND:{db_path}")

    # Read-only URI: no migration, no journal mode change, no writes.
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        session = connection.execute(
            "SELECT * FROM battle_sessions WHERE active_slot = 1 LIMIT 1"
        ).fetchone()
        if session is None:
            print(
                json.dumps(
                    {
                        "status": "NO_ACTIVE_SESSION",
                        "database": str(db_path),
                        "writes_executed": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2

        session_id = str(session["session_id"])
        reviewed_selection_id = session["current_reviewed_selection_id"]

        selection_facts = None
        if reviewed_selection_id is not None:
            selection_facts = connection.execute(
                "SELECT * FROM reviewed_selection_facts WHERE reviewed_selection_id = ?",
                (reviewed_selection_id,),
            ).fetchone()

        build: object | None = None
        if selection_facts is not None:
            build = _json_or_none(selection_facts["self_team_build_json"])

        latest_job = connection.execute(
            """
            SELECT * FROM async_jobs
            WHERE session_id = ? AND job_type = 'SELECTION_ADVICE'
            ORDER BY rowid DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()

        provider_attempts: list[dict[str, Any]] = []
        result_audits: list[dict[str, Any]] = []
        ledger = None
        if latest_job is not None:
            job_id = str(latest_job["job_id"])
            provider_attempts = [
                _row_dict(row) or {}
                for row in connection.execute(
                    """
                    SELECT attempt_ordinal, model, outcome, reason,
                           started_at_utc, completed_at_utc
                    FROM provider_attempt_audits
                    WHERE job_id = ? AND lane = 'GEMINI_SELECTION_PROVIDER'
                    ORDER BY attempt_ordinal
                    """,
                    (job_id,),
                ).fetchall()
            ]
            result_audits = [
                _row_dict(row) or {}
                for row in connection.execute(
                    """
                    SELECT disposition, reason, created_at
                    FROM async_job_results
                    WHERE job_id = ? ORDER BY audit_id
                    """,
                    (job_id,),
                ).fetchall()
            ]
            ledger = connection.execute(
                """
                SELECT session_id, match_id, generation, battle_revision,
                       reviewed_selection_id, lane, job_id, consumed_at_utc
                FROM gemini_selection_attempt_ledger
                WHERE session_id = ? AND reviewed_selection_id = ?
                ORDER BY rowid DESC LIMIT 1
                """,
                (session_id, reviewed_selection_id),
            ).fetchone()

        current_advice = None
        advice_id = session["current_selection_advice_id"]
        if advice_id is not None:
            current_advice = connection.execute(
                "SELECT * FROM selection_advices WHERE advice_id = ?",
                (advice_id,),
            ).fetchone()

        build_schema = None
        selection_profile = None
        if isinstance(build, dict) and not build.get("parse_error"):
            build_schema = build.get("schema_version")
            selection_profile = build.get("selection_profile")

        payload: dict[str, Any] = {
            "status": "ACTIVE_SESSION_INSPECTED",
            "database": str(db_path),
            "writes_executed": 0,
            "session": {
                "session_id": session_id,
                "match_id": session["match_id"],
                "generation": session["generation"],
                "state": session["state"],
                "battle_revision": session["battle_revision"],
                "current_reviewed_selection_id": reviewed_selection_id,
                "current_selection_advice_id": advice_id,
                "current_applied_selection_id": session["current_applied_selection_id"],
            },
            "selection_facts": {
                "self_team": _json_or_none(selection_facts["self_team_json"])
                if selection_facts is not None
                else None,
                "opponent_team": _json_or_none(selection_facts["opponent_team_json"])
                if selection_facts is not None
                else None,
                "build_schema": build_schema,
                "selection_profile": selection_profile,
                "self_team_build_sha256": selection_facts["self_team_build_sha256"]
                if selection_facts is not None
                else None,
            },
            "latest_selection_job": (
                {
                    "job_id": latest_job["job_id"],
                    "command_id": latest_job["command_id"],
                    "status": latest_job["status"],
                    "dispatch_count": latest_job["dispatch_count"],
                    "base_battle_revision": latest_job["base_battle_revision"],
                    "input_snapshot_id": latest_job["input_snapshot_id"],
                    "request_payload_hash": latest_job["request_payload_hash"],
                    "created_at": latest_job["created_at"],
                    "updated_at": latest_job["updated_at"],
                }
                if latest_job is not None
                else None
            ),
            "provider_attempts": provider_attempts,
            "result_audits": result_audits,
            "attempt_ledger": _row_dict(ledger),
            "current_selection_advice": (
                {
                    "advice_id": current_advice["advice_id"],
                    "job_id": current_advice["job_id"],
                    "source_type": current_advice["source_type"],
                    "model": current_advice["model"],
                    "chosen_package": current_advice["chosen_package"],
                    "chosen_package_name": current_advice["chosen_package_name"],
                    "selected_three": _json_or_none(current_advice["selected_three_json"]),
                    "lead": current_advice["lead"],
                    "intended_mega": current_advice["intended_mega"],
                    "selection_reason": current_advice["selection_reason"],
                }
                if current_advice is not None
                else None
            ),
        }

        # Convenience classification; evidence above remains authoritative.
        if latest_job is None:
            classification = "NO_SELECTION_JOB_CREATED"
        elif provider_attempts and provider_attempts[-1].get("outcome") == "FAILED":
            classification = "PROVIDER_ATTEMPT_FAILED"
        elif result_audits and result_audits[-1].get("disposition") == "INVALID_REJECTED":
            classification = "PROVIDER_RESPONSE_INVALID_REJECTED"
        elif str(latest_job["status"]) in {"FAILED", "TIMED_OUT", "DELIVERY_UNKNOWN"}:
            classification = f"JOB_{latest_job['status']}"
        elif str(latest_job["status"]) in {"QUEUED", "IN_FLIGHT"}:
            classification = f"JOB_STILL_{latest_job['status']}"
        elif current_advice is not None:
            classification = "ADVICE_PRESENT"
        else:
            classification = "OTHER"
        payload["classification"] = classification

        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
