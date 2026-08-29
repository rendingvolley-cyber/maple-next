from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from maple_next.domain.team_build import ChampionsTeamBuild

TARGET_TEAM_FILE = Path("data/teams/m-b-tournament-p1-metagross-p2-rain-v1.json")
TARGET_TEAM_NAMES = (
    "メタグロス",
    "サザンドラ",
    "アシレーヌ",
    "ペリッパー",
    "ラグラージ",
    "ブリジュラス",
)


def _db_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA_NOT_SET")
    return Path(local_app_data) / "MapleNext" / "Battle1" / "state" / "maple-next.db"


def _target_profile(repository_root: Path):
    source = repository_root / TARGET_TEAM_FILE
    if not source.is_file():
        raise RuntimeError(f"TARGET_V3_TEAM_FILE_MISSING:{source}")
    target = ChampionsTeamBuild.from_json_bytes(source.read_bytes())
    if target.schema_version != "maple-team.v3" or target.selection_profile is None:
        raise RuntimeError("TARGET_V3_SELECTION_PROFILE_INVALID")
    if target.pokemon_names != TARGET_TEAM_NAMES:
        raise RuntimeError("TARGET_V3_TEAM_NAMES_MISMATCH")
    return target.selection_profile


def _candidate_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT preset_id, name, self_team_json, build_schema_version, "
        "team_build_json, team_build_sha256, updated_at FROM self_team_presets"
    ).fetchall()
    result: list[sqlite3.Row] = []
    for row in rows:
        try:
            names = tuple(json.loads(str(row["self_team_json"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if names == TARGET_TEAM_NAMES:
            result.append(row)
    return result


def _last_used_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT last_used_self_team_preset_id FROM operator_preferences "
        "WHERE singleton_id = 1"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _active_session(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT session_id, match_id, state FROM battle_sessions "
        "WHERE active_slot = 1 LIMIT 1"
    ).fetchone()


def _inspect_payload(connection: sqlite3.Connection) -> dict[str, object]:
    candidates = _candidate_rows(connection)
    last_used_id = _last_used_id(connection)
    return {
        "status": "INSPECTED",
        "last_used_preset_id": last_used_id,
        "active_session": (
            {
                "session_id": active["session_id"],
                "match_id": active["match_id"],
                "state": active["state"],
            }
            if (active := _active_session(connection)) is not None
            else None
        ),
        "matching_presets": [
            {
                "preset_id": row["preset_id"],
                "name": row["name"],
                "build_schema_version": row["build_schema_version"],
                "is_last_used": str(row["preset_id"]) == last_used_id,
                "has_build_json": row["team_build_json"] is not None,
                "has_build_hash": row["team_build_sha256"] is not None,
                "updated_at": row["updated_at"],
            }
            for row in candidates
        ],
        "writes_executed": 0,
    }


def inspect() -> int:
    db_path = _db_path()
    if not db_path.is_file():
        raise RuntimeError(f"PRODUCTION_DB_NOT_FOUND:{db_path}")
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        print(json.dumps(_inspect_payload(connection), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        connection.close()


def apply(repository_root: Path, preset_id: str | None) -> int:
    db_path = _db_path()
    if not db_path.is_file():
        raise RuntimeError(f"PRODUCTION_DB_NOT_FOUND:{db_path}")
    profile = _target_profile(repository_root)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        active = _active_session(connection)
        if active is not None:
            raise RuntimeError(
                "ACTIVE_SESSION_PRESENT:finish/abort/export the current match before preset upgrade"
            )

        candidates = _candidate_rows(connection)
        last_used_id = _last_used_id(connection)
        chosen_id = preset_id or last_used_id
        if chosen_id is None:
            raise RuntimeError("NO_PRESET_SELECTED_AND_NO_LAST_USED_PRESET")
        selected = [row for row in candidates if str(row["preset_id"]) == chosen_id]
        if len(selected) != 1:
            available = [str(row["preset_id"]) for row in candidates]
            raise RuntimeError(
                f"TARGET_PRESET_NOT_UNIQUE_OR_NOT_MATCHING:{chosen_id}:candidates={available}"
            )
        row = selected[0]
        raw_build = row["team_build_json"]
        raw_hash = row["team_build_sha256"]
        if raw_build is None or raw_hash is None:
            raise RuntimeError("TARGET_PRESET_HAS_NO_DETAILED_BUILD")

        current = ChampionsTeamBuild.from_json_bytes(str(raw_build).encode("utf-8"))
        if current.sha256() != str(raw_hash):
            raise RuntimeError("TARGET_PRESET_BUILD_HASH_MISMATCH")
        if current.pokemon_names != TARGET_TEAM_NAMES:
            raise RuntimeError("TARGET_PRESET_BUILD_NAMES_MISMATCH")

        if current.schema_version == "maple-team.v3":
            if current.selection_profile != profile:
                raise RuntimeError("EXISTING_V3_PROFILE_DIFFERS_FROM_CANONICAL_TOURNAMENT_PROFILE")
            # Repair only a stale metadata label if necessary.
            connection.execute(
                "UPDATE self_team_presets SET build_schema_version = ? WHERE preset_id = ?",
                ("maple-team.v3", chosen_id),
            )
            connection.commit()
            print(
                json.dumps(
                    {
                        "status": "ALREADY_V3",
                        "preset_id": chosen_id,
                        "build_schema_before": row["build_schema_version"],
                        "build_schema_after": "maple-team.v3",
                        "build_sha256": current.sha256(),
                        "member_data_preserved": True,
                        "profile": profile.to_canonical_dict(),
                        "writes_executed": 1,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if current.schema_version != "maple-team.v2":
            raise RuntimeError(f"UNSUPPORTED_SOURCE_BUILD_SCHEMA:{current.schema_version}")

        upgraded = ChampionsTeamBuild(
            schema_version="maple-team.v3",
            game=current.game,
            name=current.name,
            battle_format=current.battle_format,
            members=current.members,
            selection_profile=profile,
        )
        if tuple(member.to_canonical_dict() for member in upgraded.members) != tuple(
            member.to_canonical_dict() for member in current.members
        ):
            raise RuntimeError("MEMBER_DATA_CHANGED_DURING_UPGRADE")

        encoded = upgraded.canonical_json_bytes().decode("utf-8")
        digest = upgraded.sha256()
        cursor = connection.execute(
            "UPDATE self_team_presets SET build_schema_version = ?, team_build_json = ?, "
            "team_build_sha256 = ?, updated_at = ? WHERE preset_id = ?",
            (
                "maple-team.v3",
                encoded,
                digest,
                datetime.now(UTC).isoformat(),
                chosen_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("TARGET_PRESET_UPDATE_ROWCOUNT_INVALID")
        connection.commit()
        print(
            json.dumps(
                {
                    "status": "UPGRADED_TO_V3",
                    "preset_id": chosen_id,
                    "build_schema_before": current.schema_version,
                    "build_schema_after": upgraded.schema_version,
                    "build_sha256_before": str(raw_hash),
                    "build_sha256_after": digest,
                    "member_data_preserved": True,
                    "profile": profile.to_canonical_dict(),
                    "writes_executed": 1,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preset-id")
    parser.add_argument("--repository-root", default=str(Path.cwd()))
    args = parser.parse_args()
    if not args.apply:
        return inspect()
    return apply(Path(args.repository_root).resolve(), args.preset_id)


if __name__ == "__main__":
    raise SystemExit(main())
