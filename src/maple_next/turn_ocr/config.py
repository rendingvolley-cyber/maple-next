"""Strict loader for repository-owned Turn OCR ROI calibration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from maple_next.turn_ocr.contracts import TurnRoiConfig, TurnRoiRect


class TurnRoiConfigError(ValueError):
    """Raised when the repository-owned Turn ROI config is missing or invalid."""


def _raise_provisional_type() -> bool:
    raise TurnRoiConfigError("provisional must be a boolean")


def _rect(value: Any, *, label: str) -> TurnRoiRect:
    if not isinstance(value, dict):
        raise TurnRoiConfigError(f"{label} must be an object")
    try:
        keys = {"x", "y", "width", "height"}
        if set(value) != keys:
            raise TurnRoiConfigError(f"{label} must contain exactly {sorted(keys)}")
        return TurnRoiRect(
            x=int(value["x"]),
            y=int(value["y"]),
            width=int(value["width"]),
            height=int(value["height"]),
        )
    except (TypeError, ValueError) as error:
        raise TurnRoiConfigError(f"{label} is invalid") from error


def load_turn_roi_config(path: Path) -> TurnRoiConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TurnRoiConfigError("turn ROI config is unavailable") from error
    if not isinstance(payload, dict):
        raise TurnRoiConfigError("turn ROI config must be an object")
    required = {
        "contract_version",
        "canvas_width",
        "canvas_height",
        "layout",
        "provisional",
        "rois",
    }
    if set(payload) != required:
        raise TurnRoiConfigError("turn ROI config has unexpected top-level keys")
    rois = payload["rois"]
    if not isinstance(rois, dict) or set(rois) != {
        "self_active",
        "opponent_active",
        "self_hp",
        "opponent_hp",
    }:
        raise TurnRoiConfigError("turn ROI config must define exactly four ROIs")
    try:
        return TurnRoiConfig(
            contract_version=str(payload["contract_version"]),
            canvas_width=int(payload["canvas_width"]),
            canvas_height=int(payload["canvas_height"]),
            layout=str(payload["layout"]),
            provisional=(
                payload["provisional"]
                if isinstance(payload["provisional"], bool)
                else (_raise_provisional_type())
            ),
            self_active=_rect(rois["self_active"], label="self_active"),
            opponent_active=_rect(rois["opponent_active"], label="opponent_active"),
            self_hp=_rect(rois["self_hp"], label="self_hp"),
            opponent_hp=_rect(rois["opponent_hp"], label="opponent_hp"),
            source_path=path.resolve(),
        )
    except (TypeError, ValueError) as error:
        raise TurnRoiConfigError("turn ROI config is invalid") from error
