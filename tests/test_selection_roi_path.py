"""Repository-local OCR data path contract."""

from __future__ import annotations

from pathlib import Path

from maple_next.__main__ import default_ocr_data_directory


def test_default_ocr_data_directory_is_repository_local(monkeypatch: object) -> None:
    # pytest's MonkeyPatch is intentionally kept out of the production type surface.
    typed = monkeypatch
    getattr(typed, "delenv")("MAPLE_NEXT_OCR_DATA_DIR", raising=False)

    path = default_ocr_data_directory()

    repository_root = Path(__file__).resolve().parents[1]
    assert path.resolve() == (repository_root / "data" / "ocr").resolve()
