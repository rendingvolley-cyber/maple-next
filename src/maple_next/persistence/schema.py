"""Minimal schema migration for the Issue #23 foundation."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1


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
        """
    )
    connection.commit()
