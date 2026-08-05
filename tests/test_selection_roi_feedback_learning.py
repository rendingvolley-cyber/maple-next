"""Origin-aware Selection ROI feedback remains deduplicated and fail-closed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtGui import QColor, QImage

from maple_next.selection_roi.contracts import SelectionSlotMatch
from maple_next.selection_roi.input_policy import SelectionInputOrigin
from maple_next.selection_roi.matcher import ImageFingerprint
from maple_next.selection_roi.service import (
    SelectionFeedbackTuple,
    SelectionRoiService,
    SelectionSlotFeedback,
    StoredSelectionObservation,
)

_LABELS = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")


def _image(level: int) -> QImage:
    image = QImage(20, 12, QImage.Format.Format_RGB32)
    image.fill(QColor(level, level, level))
    return image


def _install_observation(
    service: SelectionRoiService,
    root: Path,
    *,
    observation_id: str = "observation",
) -> StoredSelectionObservation:
    capture_dir = root / "selection" / "captures" / observation_id
    capture_dir.mkdir(parents=True, exist_ok=True)
    images = tuple(_image(level) for level in (0, 40, 80, 120, 160, 200))
    paths: list[Path] = []
    matches: list[SelectionSlotMatch] = []
    for slot, image in enumerate(images, start=1):
        path = capture_dir / f"slot_{slot:02d}.png"
        assert image.save(str(path))
        paths.append(path)
        matches.append(
            SelectionSlotMatch(
                slot=slot,
                crop=image,
                assigned_label=_LABELS[slot - 1],
                assigned_score=0.9,
                top_candidates=(),
            )
        )
    fingerprints = tuple(ImageFingerprint.from_image(image) for image in images)
    observation = StoredSelectionObservation(
        observation_id=observation_id,
        frame_id="frame",
        captured_at_utc=datetime.now(UTC),
        slot_files=tuple(paths),
        slot_hashes=tuple(item.exact_hash for item in fingerprints),
        fingerprints=fingerprints,
        matches=tuple(matches),
    )
    service._observations[observation_id] = observation  # noqa: SLF001
    return observation


def _feedback(
    labels: tuple[str, str, str, str, str, str],
    origin: SelectionInputOrigin,
) -> SelectionFeedbackTuple:
    return cast(
        SelectionFeedbackTuple,
        tuple(
            SelectionSlotFeedback(label=label, value_origin=origin, ocr_score=0.9)
            for label in labels
        ),
    )


def test_exact_cross_label_conflict_is_found_even_with_legacy_filename(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data" / "ocr"
    service = SelectionRoiService(root)
    observation = _install_observation(service, root)

    legacy = root / "selection" / "reference" / "labeled" / "Alpha" / "seed.png"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    assert observation.matches[0].crop.save(str(legacy))

    result = service.record_sent_observation(
        observation_id=observation.observation_id,
        slot_feedback=_feedback(
            ("WrongAlpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"),
            SelectionInputOrigin.MANUAL_TEXT,
        ),
        reviewed_selection_id="reviewed",
        session_id="session",
        match_id="match",
        generation=1,
    )

    assert result.conflict_count == 1
    assert not (
        root / "selection" / "reference" / "labeled" / "WrongAlpha"
    ).exists()
    rows = [
        json.loads(line)
        for line in service.paths.feedback_file.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["disposition"] == "CONFLICT"
    assert rows[0]["conflicting_labels"] == ["Alpha"]
    assert len(list((service.paths.quarantine_root / "label_conflicts").glob("*.png"))) == 1


def test_duplicate_provisional_evidence_still_runs_promotion_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data" / "ocr"
    service = SelectionRoiService(root)
    observation = _install_observation(service, root)
    for label, path in zip(_LABELS, observation.slot_files, strict=True):
        destination = service.paths.provisional_root / label / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())

    called: list[str] = []

    def fake_promote(label: str) -> int:
        called.append(label)
        return 0

    monkeypatch.setattr(service, "_promote_eligible_provisional", fake_promote)
    result = service.record_sent_observation(
        observation_id=observation.observation_id,
        slot_feedback=_feedback(_LABELS, SelectionInputOrigin.OCR_AUTO),
        reviewed_selection_id="reviewed",
        session_id="session",
        match_id="match-3",
        generation=1,
    )

    assert result.duplicate_count == 6
    assert called == sorted(_LABELS)


def test_conflict_rows_do_not_count_as_provisional_match_evidence(tmp_path: Path) -> None:
    service = SelectionRoiService(tmp_path / "data" / "ocr")
    rows = [
        {
            "safe_label_directory": "Alpha",
            "trust_state": "PROVISIONAL",
            "disposition": "ADDED_PROVISIONAL",
            "match_id": "match-1",
        },
        {
            "safe_label_directory": "Alpha",
            "trust_state": "PROVISIONAL",
            "disposition": "DUPLICATE",
            "match_id": "match-2",
        },
        {
            "safe_label_directory": "Alpha",
            "trust_state": "PROVISIONAL",
            "disposition": "CONFLICT",
            "match_id": "match-3",
        },
    ]
    service.paths.feedback_file.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    assert service._provisional_match_ids("Alpha") == {  # noqa: SLF001
        "match-1",
        "match-2",
    }
