"""SQLite schema migration for the Battle-1 canonical lifecycle."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 11


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            schema_version INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO schema_meta(singleton_id, schema_version) VALUES (1, 1);

        CREATE TABLE IF NOT EXISTS battle_sessions (
            session_id TEXT PRIMARY KEY,
            match_id TEXT NOT NULL UNIQUE,
            generation INTEGER NOT NULL UNIQUE,
            state TEXT NOT NULL,
            battle_revision INTEGER NOT NULL,
            metadata_revision INTEGER NOT NULL,
            current_reviewed_selection_id TEXT NULL,
            current_selection_advice_id TEXT NULL,
            current_applied_selection_id TEXT NULL,
            current_turn_id TEXT NULL,
            current_observation_id TEXT NULL,
            current_reviewed_board_id TEXT NULL,
            current_turn_advice_id TEXT NULL,
            active_slot INTEGER NULL CHECK (active_slot IS NULL OR active_slot = 1)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_single_active_session
        ON battle_sessions(active_slot);

        CREATE TABLE IF NOT EXISTS reviewed_selection_facts (
            reviewed_selection_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            self_team_json TEXT NOT NULL,
            opponent_team_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS selection_advices (
            advice_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            job_id TEXT NOT NULL UNIQUE,
            selected_three_json TEXT NOT NULL,
            lead TEXT NOT NULL,
            backline_json TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'MOCK',
            model TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS applied_selections (
            applied_selection_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            selected_three_json TEXT NOT NULL,
            lead TEXT NOT NULL,
            backline_json TEXT NOT NULL,
            source_advice_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS battle_turns (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            created_at TEXT NOT NULL,
            UNIQUE(session_id, turn_number),
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS reviewed_turn_facts (
            turn_facts_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            self_active TEXT NOT NULL,
            opponent_active TEXT NOT NULL,
            self_hp TEXT NOT NULL,
            opponent_hp TEXT NOT NULL,
            legal_moves_json TEXT NOT NULL,
            legal_switches_json TEXT NOT NULL,
            human_note TEXT NOT NULL,
            previous_snapshot_id TEXT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id),
            FOREIGN KEY(turn_id) REFERENCES battle_turns(turn_id),
            FOREIGN KEY(previous_snapshot_id) REFERENCES reviewed_turn_facts(turn_facts_id)
        );

        CREATE TABLE IF NOT EXISTS turn_advices (
            turn_advice_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            job_id TEXT NOT NULL UNIQUE,
            input_snapshot_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            action_name TEXT NOT NULL,
            opponent_prediction TEXT NOT NULL,
            rationale TEXT NOT NULL,
            is_mock INTEGER NOT NULL CHECK (is_mock IN (0, 1)),
            source_type TEXT NOT NULL DEFAULT 'MOCK',
            model TEXT NOT NULL DEFAULT 'mock-dev',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id),
            FOREIGN KEY(turn_id) REFERENCES battle_turns(turn_id),
            FOREIGN KEY(input_snapshot_id) REFERENCES reviewed_turn_facts(turn_facts_id)
        );

        CREATE TABLE IF NOT EXISTS recorded_actions (
            action_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL UNIQUE,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            action_type TEXT NOT NULL,
            action_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id),
            FOREIGN KEY(turn_id) REFERENCES battle_turns(turn_id)
        );

        CREATE TABLE IF NOT EXISTS match_outcomes (
            session_id TEXT PRIMARY KEY,
            match_id TEXT NOT NULL UNIQUE,
            generation INTEGER NOT NULL UNIQUE,
            outcome TEXT NOT NULL CHECK (outcome IN ('WIN', 'LOSE')),
            ended_at_utc TEXT NOT NULL,
            final_battle_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS match_exports (
            session_id TEXT PRIMARY KEY,
            match_id TEXT NOT NULL UNIQUE,
            schema_version TEXT NOT NULL,
            export_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            exported_at_utc TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS async_jobs (
            job_id TEXT PRIMARY KEY,
            command_id TEXT NOT NULL,
            job_type TEXT NOT NULL,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_number INTEGER NULL,
            base_battle_revision INTEGER NOT NULL,
            expected_state TEXT NOT NULL,
            input_snapshot_id TEXT NOT NULL,
            request_payload_hash TEXT NOT NULL,
            human_authorized_at TEXT NOT NULL,
            status TEXT NOT NULL,
            dispatch_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES battle_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS async_job_results (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            disposition TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gemini_selection_attempt_ledger (
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            battle_revision INTEGER NOT NULL,
            reviewed_selection_id TEXT NOT NULL,
            lane TEXT NOT NULL,
            job_id TEXT NOT NULL,
            consumed_at_utc TEXT NOT NULL,
            PRIMARY KEY (
                session_id, match_id, generation, battle_revision,
                reviewed_selection_id, lane
            )
        );

        CREATE TABLE IF NOT EXISTS turn_advice_attempt_ledger (
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_number INTEGER NOT NULL,
            battle_revision INTEGER NOT NULL,
            reviewed_snapshot_id TEXT NOT NULL,
            request_payload_hash TEXT NOT NULL,
            lane TEXT NOT NULL,
            job_id TEXT NOT NULL,
            consumed_at_utc TEXT NOT NULL,
            PRIMARY KEY (
                session_id, match_id, generation, turn_number, battle_revision,
                reviewed_snapshot_id, lane
            )
        );

        CREATE TABLE IF NOT EXISTS provider_attempt_audits (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            lane TEXT NOT NULL,
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal IN (1, 2)),
            model TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('STARTED', 'SUCCEEDED', 'FAILED')),
            reason TEXT NOT NULL DEFAULT '',
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT NULL,
            UNIQUE(job_id, lane, attempt_ordinal),
            FOREIGN KEY(job_id) REFERENCES async_jobs(job_id)
        );

        CREATE TABLE IF NOT EXISTS self_team_presets (
            preset_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL UNIQUE,
            self_team_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS operator_preferences (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            last_used_self_team_preset_id TEXT NULL,
            FOREIGN KEY(last_used_self_team_preset_id)
                REFERENCES self_team_presets(preset_id) ON DELETE SET NULL
        );
        INSERT OR IGNORE INTO operator_preferences(singleton_id) VALUES (1);

        UPDATE schema_meta SET schema_version = 11 WHERE singleton_id = 1;
        """
    )
    _ensure_column(
        connection,
        "selection_advices",
        "source_type",
        "TEXT NOT NULL DEFAULT 'MOCK'",
    )
    _ensure_column(
        connection,
        "selection_advices",
        "model",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection,
        "turn_advices",
        "source_type",
        "TEXT NOT NULL DEFAULT 'MOCK'",
    )
    _ensure_column(
        connection,
        "turn_advices",
        "model",
        "TEXT NOT NULL DEFAULT 'mock-dev'",
    )
    _ensure_column(
        connection,
        "turn_advices",
        "warnings_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "recorded_actions",
        "opponent_action_type",
        "TEXT NULL",
    )
    _ensure_column(
        connection,
        "recorded_actions",
        "opponent_action_name",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection,
        "recorded_actions",
        "action_order",
        "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    )
    connection.commit()
