"""Human-triggered self-team import parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_TEAM_IMPORT_BYTES = 64 * 1024


class TeamImportError(ValueError):
    """Raised when an explicitly selected team import is invalid."""


@dataclass(frozen=True, slots=True)
class ImportedTeam:
    pokemon: tuple[str, str, str, str, str, str]
    name: str | None = None


def read_team_import(path: str | Path) -> ImportedTeam:
    """Read one human-selected UTF-8 file without any automatic discovery."""

    source = Path(path)
    if not source.exists():
        raise TeamImportError("ファイルが見つかりません。")
    if source.is_symlink():
        raise TeamImportError("シンボリックリンクは読み込めません。")
    if not source.is_file():
        raise TeamImportError("通常ファイルのみ読み込めます。")
    if source.suffix.lower() not in {".json", ".txt"}:
        raise TeamImportError("対応していないファイル形式です。")
    try:
        if source.stat().st_size > MAX_TEAM_IMPORT_BYTES:
            raise TeamImportError("構築ファイルが大きすぎます。")
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise TeamImportError("ファイルをUTF-8として読み込めません。") from exc
    return parse_team_import(text)


def parse_team_import(text: str) -> ImportedTeam:
    """Parse Maple JSON or six-name newline/comma separated text."""

    if not text.strip():
        raise TeamImportError("構築ファイルが空です。")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        if text.lstrip().startswith(("{", "[")):
            raise TeamImportError("JSONを解析できません。") from exc
        return ImportedTeam(pokemon=_validate_team(_parse_text_team(text)))

    if not isinstance(payload, dict):
        raise TeamImportError("Maple JSONはオブジェクト形式で指定してください。")
    if payload.get("schema_version") != "maple-team.v1":
        raise TeamImportError("Maple JSONのschema_versionがmaple-team.v1ではありません。")

    pokemon = payload.get("pokemon")
    if not isinstance(pokemon, list):
        raise TeamImportError("Maple JSONのpokemonは6体の配列で指定してください。")
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise TeamImportError("Maple JSONのnameは文字列で指定してください。")
    return ImportedTeam(
        pokemon=_validate_team(pokemon),
        name=(name.strip() or None) if isinstance(name, str) else None,
    )


def _parse_text_team(text: str) -> tuple[Any, ...]:
    normalized = text.strip()
    return tuple(piece.strip() for piece in re.split(r"[\r\n,、]", normalized))


def _validate_team(entries: tuple[Any, ...] | list[Any]) -> tuple[str, str, str, str, str, str]:
    if len(entries) != 6:
        raise TeamImportError("構築は6体ちょうどで指定してください。")
    if any(not isinstance(entry, str) or not entry.strip() for entry in entries):
        raise TeamImportError("構築名に空欄があります。")
    normalized = tuple(entry.strip() for entry in entries)
    if len(set(normalized)) != 6:
        raise TeamImportError("構築名に重複があります。")
    return (
        normalized[0],
        normalized[1],
        normalized[2],
        normalized[3],
        normalized[4],
        normalized[5],
    )
