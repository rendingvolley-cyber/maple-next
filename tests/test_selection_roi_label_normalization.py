"""Selection corpus folder variants resolve to one operator-visible label."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QImage

from maple_next.selection_roi.contracts import (
    SelectionRoiCrop,
    SelectionRoiError,
    SelectionRoiRect,
    normalize_selection_label,
    safe_label_directory,
)
from maple_next.selection_roi.matcher import ReferenceImageIndex


def _image(color: str) -> QImage:
    image = QImage(16, 16, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def test_label_normalization_merges_spacing_around_form_parentheses() -> None:
    assert normalize_selection_label(" イダイトウ （ オス ） ") == "イダイトウ(オス)"
    assert normalize_selection_label("イダイトウ (オス)") == "イダイトウ(オス)"
    assert safe_label_directory("イダイトウ (オス)") == "イダイトウ(オス)"


def test_label_directory_replaces_only_path_unsafe_characters() -> None:
    assert safe_label_directory("テスト/フォーム") == "テスト_フォーム"
    try:
        safe_label_directory("CON")
    except SelectionRoiError:
        pass
    else:  # pragma: no cover - explicit fail message is clearer than pytest.raises here
        raise AssertionError("Windows reserved directory names must be rejected")


def test_reference_index_combines_historical_folder_variants(tmp_path: Path) -> None:
    root = tmp_path / "labeled"
    first = _image("#112233")
    second = _image("#223344")
    first_path = root / "イダイトウ (オス)" / "first.png"
    second_path = root / "イダイトウ(オス)" / "second.png"
    first_path.parent.mkdir(parents=True)
    second_path.parent.mkdir(parents=True)
    assert first.save(str(first_path))
    assert second.save(str(second_path))

    index = ReferenceImageIndex(root)
    index.refresh()
    crop = SelectionRoiCrop(
        slot=1,
        image=first,
        rect=SelectionRoiRect(slot=1, x=0, y=0, width=16, height=16),
    )
    candidates = index.candidates_for(crop, top_k=3)

    assert len(candidates) == 1
    assert candidates[0].label == "イダイトウ(オス)"
    assert candidates[0].reference_count == 2
    assert candidates[0].score == 1.0
