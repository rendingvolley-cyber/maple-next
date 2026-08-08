"""Repository-local OCR data path contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from maple_next.__main__ import default_ocr_data_directory, main


def test_default_ocr_data_directory_is_repository_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPLE_NEXT_OCR_DATA_DIR", "C:/outside/maple-ocr")

    path = default_ocr_data_directory()

    repository_root = Path(__file__).resolve().parents[1]
    assert path.resolve() == (repository_root / "data" / "ocr").resolve()


def test_official_entrypoint_rejects_ocr_data_outside_repository(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-ocr"

    with pytest.raises(SystemExit) as error:
        main(["--ocr-data-directory", str(outside)])

    assert error.value.code == 2
    assert not outside.exists()
