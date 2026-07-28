"""Immutable canonical records for match completion and export."""

from __future__ import annotations

from dataclasses import dataclass

from maple_next.domain.enums import MatchOutcome


@dataclass(frozen=True, slots=True)
class MatchOutcomeRecord:
    session_id: str
    match_id: str
    generation: int
    outcome: MatchOutcome
    ended_at_utc: str
    final_battle_revision: int

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.final_battle_revision < 1:
            raise ValueError("final battle revision must be positive")
        if not self.ended_at_utc.strip():
            raise ValueError("ended_at_utc must be explicit")


@dataclass(frozen=True, slots=True)
class MatchExportRecord:
    session_id: str
    match_id: str
    schema_version: str
    export_path: str
    sha256: str
    exported_at_utc: str

    def __post_init__(self) -> None:
        if not self.schema_version.strip():
            raise ValueError("schema version must be explicit")
        if not self.export_path.strip():
            raise ValueError("export path must be explicit")
        if len(self.sha256) != 64:
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise ValueError("sha256 must be hexadecimal") from exc
        if not self.exported_at_utc.strip():
            raise ValueError("exported_at_utc must be explicit")
