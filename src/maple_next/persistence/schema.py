"""SQLite schema migration for the Battle-1 canonical lifecycle."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 23


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _widen_selection_provider_attempt_audits(connection: sqlite3.Connection) -> None:
    """Upgrade the former two-model audit constraint without losing evidence."""

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("provider_attempt_audits",),
    ).fetchone()
    if row is None or "attempt_ordinal IN (1, 2)" not in str(row[0]):
        return
    connection.execute(
        "ALTER TABLE provider_attempt_audits RENAME TO provider_attempt_audits_v21"
    )
    connection.execute(
        """
        CREATE TABLE provider_attempt_audits (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            lane TEXT NOT NULL,
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
            model TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('STARTED', 'SUCCEEDED', 'FAILED')),
            reason TEXT NOT NULL DEFAULT '',
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT NULL,
            UNIQUE(job_id, lane, attempt_ordinal),
            FOREIGN KEY(job_id) REFERENCES async_jobs(job_id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO provider_attempt_audits (
            audit_id, job_id, lane, attempt_ordinal, model, outcome, reason,
            started_at_utc, completed_at_utc
        )
        SELECT audit_id, job_id, lane, attempt_ordinal, model, outcome, reason,
               started_at_utc, completed_at_utc
        FROM provider_attempt_audits_v21
        """
    )
    connection.execute("DROP TABLE provider_attempt_audits_v21")


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
            mega_state_json TEXT NOT NULL DEFAULT '{}',
            active_slot INTEGER NULL CHECK (active_slot IS NULL OR active_slot = 1)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_single_active_session
        ON battle_sessions(active_slot);

        CREATE TABLE IF NOT EXISTS reviewed_selection_facts (
            reviewed_selection_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            self_team_json TEXT NOT NULL,
            opponent_team_json TEXT NOT NULL,
            self_team_build_json TEXT NULL,
            self_team_build_sha256 TEXT NULL,
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
            chosen_package TEXT NULL,
            chosen_package_name TEXT NULL,
            intended_mega TEXT NULL,
            selection_reason TEXT NULL,
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
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal >= 1),
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
            build_schema_version TEXT NOT NULL DEFAULT 'maple-team.v1',
            team_build_json TEXT NULL,
            team_build_sha256 TEXT NULL,
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

        CREATE TABLE IF NOT EXISTS fixed_evidence_metadata (
            evidence_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            recorded_at_utc TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS confirmed_turn_states (
            confirmed_state_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            battle_revision INTEGER NOT NULL CHECK (battle_revision >= 0),
            previous_confirmed_state_id TEXT NULL,
            self_side_json TEXT NOT NULL,
            opponent_side_json TEXT NOT NULL,
            weather_json TEXT NOT NULL,
            terrain_json TEXT NOT NULL,
            confirmed_by_human INTEGER NOT NULL CHECK (confirmed_by_human IN (0, 1)),
            confirmed_at_utc TEXT NOT NULL,
            provenance TEXT NOT NULL,
            evidence_id TEXT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, turn_id, battle_revision),
            FOREIGN KEY(previous_confirmed_state_id)
                REFERENCES confirmed_turn_states(confirmed_state_id),
            FOREIGN KEY(evidence_id) REFERENCES fixed_evidence_metadata(evidence_id)
        );

        CREATE TABLE IF NOT EXISTS action_result_deltas (
            delta_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            battle_revision INTEGER NOT NULL CHECK (battle_revision >= 0),
            based_on_confirmed_state_id TEXT NOT NULL,
            self_side_json TEXT NOT NULL,
            opponent_side_json TEXT NOT NULL,
            weather_json TEXT NOT NULL,
            terrain_json TEXT NOT NULL,
            confirmed_by_human INTEGER NOT NULL CHECK (confirmed_by_human IN (0, 1)),
            confirmed_at_utc TEXT NOT NULL,
            provenance TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(based_on_confirmed_state_id)
                REFERENCES confirmed_turn_states(confirmed_state_id)
        );

        CREATE TABLE IF NOT EXISTS next_turn_state_drafts (
            draft_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            battle_revision INTEGER NOT NULL CHECK (battle_revision >= 0),
            based_on_confirmed_state_id TEXT NOT NULL,
            source_delta_id TEXT NOT NULL,
            self_side_json TEXT NOT NULL,
            opponent_side_json TEXT NOT NULL,
            weather_json TEXT NOT NULL,
            terrain_json TEXT NOT NULL,
            derived_at_utc TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, turn_id, battle_revision),
            FOREIGN KEY(based_on_confirmed_state_id)
                REFERENCES confirmed_turn_states(confirmed_state_id),
            FOREIGN KEY(source_delta_id) REFERENCES action_result_deltas(delta_id)
        );

        CREATE TABLE IF NOT EXISTS legal_action_prefill_drafts (
            prefill_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            battle_revision INTEGER NOT NULL CHECK (battle_revision >= 0),
            based_on_confirmed_state_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            action_name TEXT NOT NULL,
            confidence REAL NULL,
            derived_at_utc TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(based_on_confirmed_state_id)
                REFERENCES confirmed_turn_states(confirmed_state_id)
        );

        CREATE TABLE IF NOT EXISTS confirmed_legal_action_selections (
            confirmation_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            battle_revision INTEGER NOT NULL CHECK (battle_revision >= 0),
            action_type TEXT NOT NULL,
            action_name TEXT NOT NULL,
            confirmed_by_human INTEGER NOT NULL CHECK (confirmed_by_human IN (0, 1)),
            confirmed_at_utc TEXT NOT NULL,
            provenance TEXT NOT NULL,
            source_prefill_id TEXT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(source_prefill_id) REFERENCES legal_action_prefill_drafts(prefill_id)
        );

        CREATE TABLE IF NOT EXISTS rich_action_completions (
            transaction_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_id TEXT NOT NULL UNIQUE,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            battle_revision INTEGER NOT NULL CHECK (battle_revision >= 0),
            based_on_confirmed_state_id TEXT NOT NULL,
            own_action_type TEXT NOT NULL,
            own_action_name TEXT NOT NULL,
            opponent_action_type TEXT NOT NULL,
            opponent_action_name TEXT NOT NULL,
            action_order TEXT NOT NULL,
            delta_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(delta_id) REFERENCES action_result_deltas(delta_id),
            FOREIGN KEY(based_on_confirmed_state_id)
                REFERENCES confirmed_turn_states(confirmed_state_id)
        );

        UPDATE schema_meta SET schema_version = 14 WHERE singleton_id = 1;

        -- Event-entry UI v3 (Issue #31, 00 comment 5224627634): per-Pokemon
        -- match-local memory. Additive only -- no existing table is changed.
        -- Holds the most recently confirmed HP bucket / major status for one
        -- (session, match, generation, side, pokemon_name) so a Pokemon that
        -- switches out and later switches back in can have its HP/status
        -- restored instead of the operator re-entering it. Ability stages are
        -- deliberately NOT stored here -- they are active-slot-only per the
        -- accepted design and always reset to 0 on an ordinary switch.
        CREATE TABLE IF NOT EXISTS pokemon_local_state (
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('SELF', 'OPPONENT')),
            pokemon_name TEXT NOT NULL,
            hp_bucket_json TEXT NOT NULL,
            status_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (session_id, match_id, generation, side, pokemon_name)
        );

        UPDATE schema_meta SET schema_version = 15 WHERE singleton_id = 1;

        -- Battle Record v5: human-confirmed opponent ability memory. UNKNOWN
        -- is represented by absence, never by a false confirmed value.
        CREATE TABLE IF NOT EXISTS opponent_ability_memory (
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            opponent_entity_id TEXT NOT NULL,
            species TEXT NOT NULL,
            ability TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (session_id, match_id, generation, opponent_entity_id)
        );

        UPDATE schema_meta SET schema_version = 16 WHERE singleton_id = 1;

        -- Canonical opponent-entry events. Rows are created only while a
        -- human-confirmed turn state is appended. UI rendering/text changes
        -- can read or handle an event but can never manufacture one.
        CREATE TABLE IF NOT EXISTS opponent_entry_events (
            event_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            entry_ordinal INTEGER NOT NULL CHECK (entry_ordinal >= 1),
            confirmed_state_id TEXT NOT NULL UNIQUE,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            species_id TEXT NULL,
            species_name TEXT NOT NULL,
            opponent_entity_id TEXT NOT NULL,
            handled_at_utc TEXT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(session_id, match_id, generation, entry_ordinal),
            FOREIGN KEY(confirmed_state_id)
                REFERENCES confirmed_turn_states(confirmed_state_id)
        );

        UPDATE schema_meta SET schema_version = 17 WHERE singleton_id = 1;

        -- Bundle 2 (Gemini V2): explicit legal-switch confirmation status.
        -- Additive only. One row per exact binding (identity + the
        -- confirmed-state/applied-selection facts the confirmed set is
        -- bound to); a re-confirmation within the same binding updates that
        -- row in place rather than accumulating history noise. Absence of a
        -- matching row is the durable representation of
        -- NOT_CAPTURED_OR_UNRESOLVED -- that status value is never itself
        -- written to the ``status`` column.
        CREATE TABLE IF NOT EXISTS legal_switch_confirmations (
            confirmation_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            match_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL CHECK (turn_number >= 1),
            battle_revision INTEGER NOT NULL CHECK (battle_revision >= 0),
            based_on_confirmed_state_id TEXT NOT NULL,
            applied_selection_id TEXT NOT NULL,
            legal_switches_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('CONFIRMED_NONEMPTY', 'CONFIRMED_NONE')),
            confirmed_by_human INTEGER NOT NULL CHECK (confirmed_by_human IN (0, 1)),
            confirmed_at_utc TEXT NOT NULL,
            provenance TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(
                session_id, match_id, generation, turn_id, turn_number,
                battle_revision, based_on_confirmed_state_id, applied_selection_id
            ),
            FOREIGN KEY(based_on_confirmed_state_id)
                REFERENCES confirmed_turn_states(confirmed_state_id),
            FOREIGN KEY(applied_selection_id)
                REFERENCES applied_selections(applied_selection_id)
        );

        UPDATE schema_meta SET schema_version = 18 WHERE singleton_id = 1;
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
    _ensure_column(
        connection,
        "reviewed_selection_facts",
        "self_team_build_json",
        "TEXT NULL",
    )
    _ensure_column(connection, "selection_advices", "chosen_package", "TEXT NULL")
    _ensure_column(connection, "selection_advices", "chosen_package_name", "TEXT NULL")
    _ensure_column(connection, "selection_advices", "intended_mega", "TEXT NULL")
    _ensure_column(connection, "selection_advices", "selection_reason", "TEXT NULL")
    _ensure_column(
        connection,
        "reviewed_selection_facts",
        "self_team_build_sha256",
        "TEXT NULL",
    )
    _ensure_column(
        connection,
        "self_team_presets",
        "build_schema_version",
        "TEXT NOT NULL DEFAULT 'maple-team.v1'",
    )
    _ensure_column(
        connection,
        "self_team_presets",
        "team_build_json",
        "TEXT NULL",
    )
    _ensure_column(
        connection,
        "self_team_presets",
        "team_build_sha256",
        "TEXT NULL",
    )
    # Additive migration for existing candidate DBs whose rich_action_completions
    # table predates these columns. No backfill: historical rows keep NULL for
    # values that were never recorded, rather than guessing them.
    _ensure_column(connection, "rich_action_completions", "match_id", "TEXT NULL")
    _ensure_column(connection, "rich_action_completions", "generation", "INTEGER NULL")
    _ensure_column(connection, "rich_action_completions", "battle_revision", "INTEGER NULL")
    _ensure_column(
        connection, "rich_action_completions", "based_on_confirmed_state_id", "TEXT NULL"
    )
    # Bundle 4 (Gemini V2): pin the immutable official Champions rules
    # snapshot identity to the match at creation time. NULL on every
    # existing/legacy row -- an unpinned match is never backfilled with a
    # guessed pin; it simply stays unpinned and fails closed if a rich
    # provider-ready request is attempted for it (see
    # ``domain/champions_rules.py`` / ``application/service.py``).
    _ensure_column(connection, "battle_sessions", "rules_ruleset_id", "TEXT NULL")
    _ensure_column(connection, "battle_sessions", "rules_ruleset_version", "TEXT NULL")
    _ensure_column(connection, "battle_sessions", "rules_snapshot_id", "TEXT NULL")
    _ensure_column(connection, "battle_sessions", "rules_facts_sha256", "TEXT NULL")
    # Bundle 5 (Gemini V2): pin the immutable opponent-INTEL population
    # generation identity to the match at creation time. NULL on every
    # existing/legacy row and never backfilled: a pre-Bundle-5 match must
    # deterministically resolve as UNAVAILABLE population INTEL rather
    # than retroactively adopting whatever snapshot is provisioned now
    # (see ``domain/opponent_intel_context.py::OpponentIntelPin.
    # from_session_fields``). ``opponent_intel_pin_status`` is
    # ``PINNED``/``UNAVAILABLE`` for any match created from Bundle 5
    # onward, so "explicitly no INTEL" stays distinguishable from
    # "predates INTEL".
    _ensure_column(connection, "battle_sessions", "opponent_intel_pin_status", "TEXT NULL")
    _ensure_column(connection, "battle_sessions", "opponent_intel_generation_id", "TEXT NULL")
    _ensure_column(connection, "battle_sessions", "opponent_intel_snapshot_sha256", "TEXT NULL")
    # Bundle 6 (Gemini V2): versioned Turn Advice response contract.
    # ``response_schema_version`` defaults every existing/legacy row to
    # ``maple-turn-advice-response.v1`` -- historical rows are never
    # retroactively reclassified as v2, since their richer structured detail
    # (recommendation_robustness, support/support_basis, alternatives) was
    # never captured and must remain genuinely unknown rather than
    # fabricated. ``advice_json`` is NULL for every v1 row; it carries the
    # canonical, strictly re-validated v2 body only for a row explicitly
    # tagged v2 (see ``domain/models.py::TurnAdviceSnapshot``).
    _ensure_column(
        connection,
        "turn_advices",
        "response_schema_version",
        "TEXT NOT NULL DEFAULT 'maple-turn-advice-response.v1'",
    )
    _ensure_column(connection, "turn_advices", "advice_json", "TEXT NULL")
    # Tournament Battle Mega: one additive, match-level canonical actual
    # state. Historical sessions intentionally retain the empty JSON default;
    # no event is inferred or backfilled from Selection intent or prior rows.
    _ensure_column(
        connection,
        "battle_sessions",
        "mega_state_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _widen_selection_provider_attempt_audits(connection)
    _sanitize_async_job_result_payloads(connection)
    connection.execute(
        "UPDATE schema_meta SET schema_version = ? WHERE singleton_id = 1",
        (SCHEMA_VERSION,),
    )
    connection.commit()


def _sanitize_async_job_result_payloads(connection: sqlite3.Connection) -> None:
    """Scrub any historical raw provider payload from async_job_results.

    Older runtime databases may still hold raw ``ResultEnvelope.payload``
    content written before ``JobStore.audit_result`` was changed to persist
    only a fixed canonical value. This runs on every startup migration and
    is idempotent: rows already at the canonical value are left untouched.
    """

    connection.execute(
        "UPDATE async_job_results SET payload_json = '{}' WHERE payload_json != '{}'"
    )
