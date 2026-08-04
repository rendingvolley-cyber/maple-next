"""Pure, draft-only legal-move prefill helpers."""

from __future__ import annotations

from collections.abc import Sequence

from maple_next.domain.team_build import ChampionsTeamBuild


def moves_for_active(
    team_build: ChampionsTeamBuild | None,
    active_name: str,
) -> tuple[str, ...]:
    """Return confirmed moves for an active member, or no prefill."""

    if team_build is None or not active_name.strip():
        return ()
    try:
        return team_build.member_by_name(active_name).moves
    except KeyError:
        return ()


def prefill_legal_moves(
    *,
    team_build: ChampionsTeamBuild | None,
    active_name: str,
    current_moves: Sequence[str] = (),
    user_edited: Sequence[bool] = (),
) -> tuple[str, ...]:
    """Update only untouched draft slots when the active member changes.

    The returned tuple is UI draft data only.  It is never a canonical
    reviewed-facts snapshot and must still pass through human confirmation.
    """

    confirmed = moves_for_active(team_build, active_name)
    result = list(current_moves[:4])
    while len(result) < 4:
        result.append("")
    edited = list(user_edited[:4])
    while len(edited) < 4:
        edited.append(False)
    for index in range(4):
        if edited[index]:
            continue
        result[index] = confirmed[index] if index < len(confirmed) else ""
    return tuple(result)


legal_move_prefill = prefill_legal_moves
