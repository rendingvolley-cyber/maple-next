"""Repository-local OCR data path contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.__main__ import default_ocr_data_directory


def test_default_ocr_data_directory_is_repository_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAPLE_NEXT_OCR_DATA_DIR", raising=False)

    path = default_ocr_data_directory()

    repository_root = Path(__file__).resolve().parents[1]
    assert path.resolve() == (repository_root / "data" / "ocr").resolve()
