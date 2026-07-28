# Issue #23 foundation boundaries

## Canonical ownership

- SQLite is the sole persisted authority.
- Only `SQLiteRepository` opens a writable SQLite connection.
- Worker contract modules are immutable data contracts and do not import persistence code.
- UI code will consume `DomainProjection`; it must not infer CTA state from entity presence.

## Restart safety

- Provider `QUEUED` jobs become `INTERRUPTED` on restart and are never auto-dispatched.
- Provider `IN_FLIGHT` jobs become `DELIVERY_UNKNOWN` on restart.
- Capture and OCR unfinished jobs become `INTERRUPTED`.
- A new provider job can only be created by a new human command.

## Revisions

- `battle_revision` changes for strategic or canonical battle facts.
- `metadata_revision` changes only for non-strategic metadata.
- Advice binding uses `battle_revision`; metadata-only changes do not stale advice.
