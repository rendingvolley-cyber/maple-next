"""Human-triggered Pokemon Champions team import and export."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from maple_next.domain.team_build import ChampionsTeamBuild

MAX_TEAM_IMPORT_BYTES = 64 * 1024
TEAM_SCHEMA_V1 = "maple-team.v1"
TEAM_SCHEMA_V2 = "maple-team.v2"


class TeamImportError(ValueError):
    """Raised when an explicitly selected team import/export is invalid."""


@dataclass(frozen=True, slots=True)
class ImportedTeam:
    pokemon: tuple[str, str, str, str, str, str]
    name: str | None = None
    team_build: ChampionsTeamBuild | None = None

    def __post_init__(self) -> None:
        if self.team_build is not None and self.team_build.pokemon_names != self.pokemon:
            raise ValueError("imported team names and build members must match")

    @property
    def status(self) -> str:
        return "DETAILED" if self.team_build is not None else "NAMES_ONLY"

    @property
    def build_schema_version(self) -> str:
        return TEAM_SCHEMA_V2 if self.team_build is not None else TEAM_SCHEMA_V1


def read_team_import(path: str | Path) -> ImportedTeam:
    """Read one human-selected UTF-8 file without automatic discovery."""

    source = Path(path)
    if source.is_symlink():
        raise TeamImportError("symbolic links are not accepted")
    if not source.exists():
        raise TeamImportError("file not found")
    if not source.is_file():
        raise TeamImportError("only regular files are accepted")
    if source.suffix.lower() not in {".json", ".txt"}:
        raise TeamImportError("unsupported team file type")
    try:
        if source.stat().st_size > MAX_TEAM_IMPORT_BYTES:
            raise TeamImportError("大きすぎます (螟ｧ縺阪☆縺弱∪縺・)")
        text = source.read_text(encoding="utf-8-sig")
    except TeamImportError:
        raise
    except (OSError, UnicodeError) as exc:
        raise TeamImportError("team file is not valid UTF-8") from exc
    return parse_team_import(text)


def parse_team_import(text: str) -> ImportedTeam:
    """Parse Maple v1/v2 JSON or six-name text."""

    if not text.strip():
        raise TeamImportError("team file is empty")

    try:
        payload = json.loads(text)
    except RecursionError as exc:
        raise TeamImportError("JSON ネストが深すぎます (繝阪せ繝・)") from exc
    except json.JSONDecodeError as exc:
        if text.lstrip().startswith(("{", "[")):
            raise TeamImportError("JSON parse error") from exc
        return ImportedTeam(pokemon=_validate_team(_parse_text_team(text)))

    if not isinstance(payload, dict):
        raise TeamImportError("Maple JSON はオブジェクトで指定してください")

    schema_version = payload.get("schema_version")
    if schema_version == TEAM_SCHEMA_V2:
        expected = {"schema_version", "game", "name", "battle_format", "members"}
        if set(payload) != expected:
            # This also rejects tera_type/tera/terastal and any future
            # unreviewed field before it can enter a canonical build.
            raise TeamImportError("Maple JSON v2 fields are invalid")
        try:
            build = ChampionsTeamBuild.from_dict(payload)
        except (TypeError, ValueError, KeyError, RecursionError) as exc:
            raise TeamImportError("Maple JSON v2 build is invalid") from exc
        return ImportedTeam(
            pokemon=build.pokemon_names,
            name=build.name,
            team_build=build,
        )

    if schema_version != TEAM_SCHEMA_V1:
        raise TeamImportError("unsupported Maple team schema")

    # v1 deliberately retains its historical permissiveness: older exports
    # may contain an ignored source object, and names-only imports have no
    # detailed build snapshot.
    pokemon = payload.get("pokemon")
    if not isinstance(pokemon, list):
        raise TeamImportError("Maple v1 pokemon must be a six-entry array")
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise TeamImportError("Maple v1 name must be a string")
    return ImportedTeam(
        pokemon=_validate_team(pokemon),
        name=(name.strip() or None) if isinstance(name, str) else None,
    )


def _parse_text_team(text: str) -> tuple[Any, ...]:
    normalized = text.strip()
    return tuple(piece.strip() for piece in re.split(r"[\r\n,、，]", normalized))


def _validate_team(
    entries: tuple[Any, ...] | list[Any],
) -> tuple[str, str, str, str, str, str]:
    if len(entries) != 6:
        raise TeamImportError("team must contain exactly six names")
    if any(not isinstance(entry, str) or not entry.strip() for entry in entries):
        raise TeamImportError("team names must not be blank")
    normalized = tuple(entry.strip() for entry in entries)
    if len(set(normalized)) != 6:
        raise TeamImportError("team names must be unique")
    return (
        normalized[0],
        normalized[1],
        normalized[2],
        normalized[3],
        normalized[4],
        normalized[5],
    )


def team_export_payload(imported: ImportedTeam) -> dict[str, Any]:
    """Build a deterministic v1 or v2 export object."""

    if imported.team_build is not None:
        return imported.team_build.to_canonical_dict()
    payload: dict[str, Any] = {
        "schema_version": TEAM_SCHEMA_V1,
        "pokemon": list(imported.pokemon),
    }
    if imported.name is not None:
        payload["name"] = imported.name
    return payload


def encode_team_export(imported: ImportedTeam) -> bytes:
    return (
        json.dumps(
            team_export_payload(imported),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_team_export(
    path: str | Path,
    imported: ImportedTeam,
    *,
    repository_root: str | Path | None = None,
) -> Path:
    """Atomically write one human-requested export."""

    destination = Path(path).expanduser().resolve()
    if repository_root is not None:
        root = Path(repository_root).expanduser().resolve()
        if destination.is_relative_to(root):
            raise TeamImportError("export destination must be outside the repository")
    content = encode_team_export(imported)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except TeamImportError:
        raise
    except OSError as exc:
        raise TeamImportError("team export could not be written") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return destination


export_team_json = write_team_export
build_team_export_payload = team_export_payload
