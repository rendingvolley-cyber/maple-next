"""Event-entry UI v3 (Issue #31, 00 comment 5224627634): pure ability-stage
event presets and per-Pokemon switch-transition math.

Nothing here performs I/O or persistence. Everything is pure and
side-effect-free: given current confirmed values, compute the *candidate*
next values. The caller (UI/controller) is solely responsible for turning a
computed candidate into an actual :class:`~maple_next.domain.turn_state.
SideDelta` and only persisting it through the existing
``record_actual_action`` human-confirmed path -- nothing here writes a
canonical fact by itself.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_STAGE = -6
MAX_STAGE = 6

STAGE_FIELD_NAMES: tuple[str, ...] = (
    "attack_stage",
    "defense_stage",
    "special_attack_stage",
    "special_defense_stage",
    "speed_stage",
    "accuracy_stage",
    "evasion_stage",
)


def clamp_stage(value: int) -> int:
    """Fail-safe clamp to the canonical -6..+6 stage range."""

    return max(MIN_STAGE, min(MAX_STAGE, value))


@dataclass(frozen=True, slots=True)
class StageEventPreset:
    """One quick ability-stage event. ``reset`` sets every stage to 0."""

    key: str
    label: str
    deltas: tuple[tuple[str, int], ...]
    reset: bool = False

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("key must be explicit")
        if not self.label.strip():
            raise ValueError("label must be explicit")
        for field_name, _delta in self.deltas:
            if field_name not in STAGE_FIELD_NAMES:
                raise ValueError(f"unknown stage field: {field_name}")


STAGE_EVENT_PRESETS: tuple[StageEventPreset, ...] = (
    StageEventPreset(
        key="karawoyaburu",
        label="からをやぶる",
        deltas=(
            ("attack_stage", 2),
            ("defense_stage", -1),
            ("special_attack_stage", 2),
            ("special_defense_stage", -1),
            ("speed_stage", 2),
        ),
    ),
    StageEventPreset(
        key="turuginomai",
        label="つるぎのまい",
        deltas=(("attack_stage", 2),),
    ),
    StageEventPreset(
        key="ryuunomai",
        label="りゅうのまい",
        deltas=(("attack_stage", 1), ("speed_stage", 1)),
    ),
    StageEventPreset(
        key="meisou",
        label="めいそう",
        deltas=(("special_attack_stage", 1), ("special_defense_stage", 1)),
    ),
    StageEventPreset(
        key="chounomai",
        label="ちょうのまい",
        deltas=(
            ("special_attack_stage", 1),
            ("special_defense_stage", 1),
            ("speed_stage", 1),
        ),
    ),
    StageEventPreset(
        key="reset",
        label="能力変化リセット",
        deltas=(),
        reset=True,
    ),
)

STAGE_EVENT_PRESETS_BY_KEY: dict[str, StageEventPreset] = {
    preset.key: preset for preset in STAGE_EVENT_PRESETS
}


def apply_stage_event(
    current_stages: dict[str, int], preset: StageEventPreset
) -> dict[str, int]:
    """Return the candidate post-event stage values for the fields the
    preset touches. Every field is clamped to -6..+6. Fields the preset does
    not touch are omitted from the result (caller treats them as
    UNCHANGED)."""

    if preset.reset:
        return dict.fromkeys(STAGE_FIELD_NAMES, 0)
    result: dict[str, int] = {}
    for field_name, delta in preset.deltas:
        current = current_stages.get(field_name, 0)
        result[field_name] = clamp_stage(current + delta)
    return result


# -- major status quick presets --------------------------------------------

MAJOR_STATUS_PRESETS: tuple[str, ...] = ("やけど", "まひ", "どく", "ねむり", "こおり")
MAJOR_STATUS_CLEAR_LABEL = "状態異常なし"


def ordinary_switch_reset_stages() -> dict[str, int]:
    """Ability stages an ordinary confirmed switch always resets to."""

    return dict.fromkeys(STAGE_FIELD_NAMES, 0)
