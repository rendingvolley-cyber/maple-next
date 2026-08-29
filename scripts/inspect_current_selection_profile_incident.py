"""Read-only production Selection incident inspector.

This script intentionally opens the configured production SQLite database in
read-only mode. It performs no migration, no write, no provider call, and
prints only Selection contract/binding facts needed to classify a fixed-package
violation.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is not set")
    return Path(local) / "MapleNext" / "Battle1" / "state" / "maple-next.db"


def _json_or_none(value: object) -> Any:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return "<INVALID_JSON>"


def main() -> None:
    path = _db_path().resolve()
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        session = con.execute(
            "SELECT * FROM battle_sessions WHERE active_slot = 1"
        ).fetchone()
        if session is None:
            print(json.dumps({"status": "NO_ACTIVE_SESSION"}, ensure_ascii=False, indent=2))
            return

        reviewed_id = session["current_reviewed_selection_id"]
        advice_id = session["current_selection_advice_id"]
        applied_id = session["current_applied_selection_id"]

        facts = (
            con.execute(
                "SELECT * FROM reviewed_selection_facts WHERE reviewed_selection_id = ?",
                (reviewed_id,),
            ).fetchone()
            if reviewed_id is not None
            else None
        )
        advice = (
            con.execute(
                "SELECT * FROM selection_advices WHERE advice_id = ?", (advice_id,)
            ).fetchone()
            if advice_id is not None
            else None
        )
        applied = (
            con.execute(
                "SELECT * FROM applied_selections WHERE applied_selection_id = ?", (applied_id,)
            ).fetchone()
            if applied_id is not None
            else None
        )
        job = None
        if advice is not None:
            job = con.execute(
                "SELECT * FROM async_jobs WHERE job_id = ?", (advice["job_id"],)
            ).fetchone()

        build = _json_or_none(facts["self_team_build_json"]) if facts is not None else None
        profile = build.get("selection_profile") if isinstance(build, dict) else None
        packages = profile.get("packages") if isinstance(profile, dict) else None

        output = {
            "status": "OK",
            "database": str(path),
            "session": {
                "session_id": session["session_id"],
                "match_id": session["match_id"],
                "generation": session["generation"],
                "state": session["state"],
                "battle_revision": session["battle_revision"],
                "reviewed_selection_id": reviewed_id,
                "selection_advice_id": advice_id,
                "applied_selection_id": applied_id,
            },
            "selection_facts": {
                "self_team": _json_or_none(facts["self_team_json"]) if facts is not None else None,
                "opponent_team": _json_or_none(facts["opponent_team_json"]) if facts is not None else None,
                "build_schema_version": build.get("schema_version") if isinstance(build, dict) else None,
                "build_sha256": facts["self_team_build_sha256"] if facts is not None else None,
                "selection_profile_present": isinstance(profile, dict),
                "profile_mode": profile.get("mode") if isinstance(profile, dict) else None,
                "mixing_allowed": profile.get("mixing_allowed") if isinstance(profile, dict) else None,
                "packages": packages,
            },
            "accepted_selection_advice": (
                {
                    "advice_id": advice["advice_id"],
                    "job_id": advice["job_id"],
                    "source_type": advice["source_type"],
                    "model": advice["model"],
                    "chosen_package": advice["chosen_package"],
                    "chosen_package_name": advice["chosen_package_name"],
                    "selected_three": _json_or_none(advice["selected_three_json"]),
                    "lead": advice["lead"],
                    "intended_mega": advice["intended_mega"],
                    "selection_reason": advice["selection_reason"],
                }
                if advice is not None
                else None
            ),
            "applied_selection": (
                {
                    "applied_selection_id": applied["applied_selection_id"],
                    "selected_three": _json_or_none(applied["selected_three_json"]),
                    "lead": applied["lead"],
                    "source_advice_id": applied["source_advice_id"],
                }
                if applied is not None
                else None
            ),
            "selection_job": (
                {
                    "job_id": job["job_id"],
                    "status": job["status"],
                    "input_snapshot_id": job["input_snapshot_id"],
                    "base_battle_revision": job["base_battle_revision"],
                    "request_payload_hash": job["request_payload_hash"],
                }
                if job is not None
                else None
            ),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        con.close()


if __name__ == "__main__":
    main()
