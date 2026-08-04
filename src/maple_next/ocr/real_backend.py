"""Real, candidate-only OCR backend for the official Maple Next runtime.

The synchronous :class:`TesseractCliOcrBackend` owns image-to-text parsing.
The :class:`LatestOnlyAsyncOcrBackend` keeps that external process off the Qt
UI thread and stores at most one pending request.  Results are suggestions
only: this module has no repository, provider, domain-command, or game-action
imports.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import Final

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from maple_next.capture.contracts import FramePacket
from maple_next.domain.enums import HpBucket
from maple_next.ocr.contracts import (
    OcrCandidate,
    OcrCandidateContext,
    OcrFieldKey,
)

SELECTION_OPPONENT_FIELD_KEYS: Final[tuple[str, ...]] = tuple(
    f"opponent_team_{index}" for index in range(1, 7)
)

_TESSERACT_TIMEOUT_SECONDS: Final[float] = 3.0
_MIN_LINE_CONFIDENCE: Final[float] = 20.0
_MIN_ACTIVE_MATCH_SCORE: Final[float] = 0.57
_OWN_TEAM_MATCH_SCORE: Final[float] = 0.72

_UI_STOP_WORDS: Final[tuple[str, ...]] = (
    "選出",
    "決定",
    "相手",
    "自分",
    "ポケモン",
    "バトル",
    "キャンセル",
    "準備",
    "対戦",
    "時間",
    "残り",
    "通信",
    "トレーナー",
    "チーム",
    "メンバー",
)

_RATIO_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?P<current>\d{1,3})\s*[/／]\s*(?P<maximum>\d{1,3})(?!\d)"
)
_PERCENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?P<percent>\d{1,3})\s*[%％]")
_DIGIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d")


class OcrScene(StrEnum):
    SELECTION = "SELECTION"
    TURN = "TURN"


@dataclass(frozen=True, slots=True)
class RealOcrCandidateContext(OcrCandidateContext):
    """Runtime context for selection-screen and turn-screen recognition."""

    scene: OcrScene = OcrScene.TURN
    self_team_candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OcrTextLine:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float

    @property
    def center_x(self) -> float:
        return self.left + self.width / 2

    @property
    def center_y(self) -> float:
        return self.top + self.height / 2


CommandRunner = Callable[[Sequence[str], bytes | None, float], subprocess.CompletedProcess[bytes]]


def _default_command_runner(
    arguments: Sequence[str], input_bytes: bytes | None, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - executable is resolved from a strict allowlist
        list(arguments),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.strip("-—–_・:：|｜[]【】()（）<>〈〉『』「」.,、。!?！？")


def _similarity(left: str, right: str) -> float:
    normalized_left = _normalized_text(left).casefold()
    normalized_right = _normalized_text(right).casefold()
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    ratio = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    if normalized_left in normalized_right or normalized_right in normalized_left:
        ratio = max(ratio, 0.88)
    return ratio


def _hp_bucket_for_ratio(ratio: float) -> str:
    percent = max(0.0, min(100.0, ratio * 100.0))
    if percent <= 0.0:
        return HpBucket.ZERO.value
    if percent >= 99.5:
        return HpBucket.FULL.value
    lower = int(percent)
    if lower <= 10:
        return HpBucket.ONE_TO_TEN.value
    if lower <= 20:
        return HpBucket.ELEVEN_TO_TWENTY.value
    if lower <= 30:
        return HpBucket.TWENTY_ONE_TO_THIRTY.value
    if lower <= 40:
        return HpBucket.THIRTY_ONE_TO_FORTY.value
    if lower <= 50:
        return HpBucket.FORTY_ONE_TO_FIFTY.value
    if lower <= 60:
        return HpBucket.FIFTY_ONE_TO_SIXTY.value
    if lower <= 70:
        return HpBucket.SIXTY_ONE_TO_SEVENTY.value
    if lower <= 80:
        return HpBucket.SEVENTY_ONE_TO_EIGHTY.value
    if lower <= 90:
        return HpBucket.EIGHTY_ONE_TO_NINETY.value
    return HpBucket.NINETY_ONE_TO_NINETY_NINE.value


def _candidate(
    *,
    field_key: str,
    value: str,
    raw_text: str,
    confidence: float,
    reason: str,
    frame_id: str,
    raw_estimate: float | None = None,
) -> OcrCandidate:
    return OcrCandidate(
        field_key=field_key,
        suggested_value=value,
        raw_text=raw_text,
        confidence=max(0.0, min(1.0, confidence)),
        rank=1,
        reason=reason,
        source_frame_id=frame_id,
        raw_estimate=raw_estimate,
    )


class TesseractCliOcrBackend:
    """Japanese/English Tesseract TSV parser with no canonical-side effects."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._runner = runner or _default_command_runner
        self._executable = executable or self._discover_executable()
        self._availability: bool | None = None
        self._language = "jpn+eng"
        self._recognition_count = 0
        self._failure_count = 0

    @staticmethod
    def _discover_executable() -> str | None:
        configured = os.environ.get("MAPLE_TESSERACT_EXE", "").strip()
        candidates: list[str] = []
        if configured:
            candidates.append(configured)
        discovered = shutil.which("tesseract")
        if discovered:
            candidates.append(discovered)
        for environment_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.environ.get(environment_key)
            if root:
                candidates.append(str(Path(root) / "Tesseract-OCR" / "tesseract.exe"))
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if path.is_file():
                return str(path)
        return None

    def is_available(self) -> bool:
        if self._availability is not None:
            return self._availability
        if self._executable is None:
            self._availability = False
            return False
        try:
            result = self._runner(
                (self._executable, "--list-langs"),
                None,
                _TESSERACT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            self._availability = False
            return False
        languages = {
            line.strip().casefold()
            for line in result.stdout.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        }
        self._availability = result.returncode == 0 and "jpn" in languages
        return self._availability

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[OcrCandidate, ...]:
        if not self.is_available() or not isinstance(frame.image, QImage):
            return ()
        runtime_context = (
            context
            if isinstance(context, RealOcrCandidateContext)
            else RealOcrCandidateContext(
                self_active_candidates=context.self_active_candidates,
                opponent_active_candidates=context.opponent_active_candidates,
            )
        )
        try:
            lines = self._recognize_lines(frame.image)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self._failure_count += 1
            return ()
        self._recognition_count += 1
        if runtime_context.scene is OcrScene.SELECTION:
            return self._selection_candidates(frame, lines, runtime_context)
        return self._turn_candidates(frame, lines, runtime_context)

    def metrics(self) -> dict[str, object]:
        return {
            "ocr_engine": "tesseract-cli",
            "ocr_available": bool(self._availability),
            "ocr_recognition_count": self._recognition_count,
            "ocr_failure_count": self._failure_count,
        }

    def _recognize_lines(self, image: QImage) -> tuple[OcrTextLine, ...]:
        png_bytes = self._image_png_bytes(image)
        if self._executable is None:
            return ()
        result = self._runner(
            (
                self._executable,
                "stdin",
                "stdout",
                "-l",
                self._language,
                "--psm",
                "11",
                "tsv",
            ),
            png_bytes,
            _TESSERACT_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise RuntimeError("tesseract failed")
        return self._parse_tsv(result.stdout.decode("utf-8", errors="replace"))

    @staticmethod
    def _image_png_bytes(image: QImage) -> bytes:
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            raise RuntimeError("image buffer unavailable")
        try:
            if not image.save(buffer, "PNG"):
                raise RuntimeError("image encoding failed")
        finally:
            buffer.close()
        return bytes(data)

    @staticmethod
    def _parse_tsv(tsv: str) -> tuple[OcrTextLine, ...]:
        grouped: dict[tuple[str, str, str, str], list[tuple[str, int, int, int, int, float]]] = {}
        reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
        for row in reader:
            if row.get("level") != "5":
                continue
            text = row.get("text", "").strip()
            if not text:
                continue
            try:
                confidence = float(row.get("conf", "-1"))
                left = int(row.get("left", "0"))
                top = int(row.get("top", "0"))
                width = int(row.get("width", "0"))
                height = int(row.get("height", "0"))
            except ValueError:
                continue
            if confidence < 0 or width <= 0 or height <= 0:
                continue
            key = (
                row.get("page_num", "0"),
                row.get("block_num", "0"),
                row.get("par_num", "0"),
                row.get("line_num", "0"),
            )
            grouped.setdefault(key, []).append(
                (text, left, top, width, height, confidence)
            )

        lines: list[OcrTextLine] = []
        for words in grouped.values():
            words.sort(key=lambda item: item[1])
            text = "".join(item[0] for item in words)
            left = min(item[1] for item in words)
            top = min(item[2] for item in words)
            right = max(item[1] + item[3] for item in words)
            bottom = max(item[2] + item[4] for item in words)
            total_width = sum(max(1, item[3]) for item in words)
            confidence = sum(item[5] * max(1, item[3]) for item in words) / total_width
            lines.append(
                OcrTextLine(
                    text=text,
                    left=left,
                    top=top,
                    width=right - left,
                    height=bottom - top,
                    confidence=confidence,
                )
            )
        return tuple(sorted(lines, key=lambda line: (line.top, line.left)))

    @staticmethod
    def _plausible_name_line(line: OcrTextLine) -> bool:
        normalized = _normalized_text(line.text)
        if line.confidence < _MIN_LINE_CONFIDENCE:
            return False
        if not 2 <= len(normalized) <= 18:
            return False
        if _DIGIT_PATTERN.search(normalized):
            return False
        return not any(stop_word in normalized for stop_word in _UI_STOP_WORDS)

    def _selection_candidates(
        self,
        frame: FramePacket,
        lines: tuple[OcrTextLine, ...],
        context: RealOcrCandidateContext,
    ) -> tuple[OcrCandidate, ...]:
        plausible = [line for line in lines if self._plausible_name_line(line)]
        if not plausible:
            return ()
        middle = frame.width / 2
        own_matches = {"left": 0, "right": 0}
        for line in plausible:
            own_score = max(
                (_similarity(line.text, name) for name in context.self_team_candidates),
                default=0.0,
            )
            if own_score >= _OWN_TEAM_MATCH_SCORE:
                own_matches["left" if line.center_x < middle else "right"] += 1

        if own_matches["left"] > own_matches["right"]:
            opponent_side = "right"
        elif own_matches["right"] > own_matches["left"]:
            opponent_side = "left"
        else:
            left_count = sum(line.center_x < middle for line in plausible)
            right_count = len(plausible) - left_count
            opponent_side = "right" if right_count >= left_count else "left"

        side_lines: list[OcrTextLine] = []
        for line in plausible:
            side = "left" if line.center_x < middle else "right"
            if side != opponent_side:
                continue
            own_score = max(
                (_similarity(line.text, name) for name in context.self_team_candidates),
                default=0.0,
            )
            if own_score >= 0.82:
                continue
            side_lines.append(line)

        # Collapse duplicate OCR fragments from the same visual row.
        row_best: list[OcrTextLine] = []
        for line in sorted(side_lines, key=lambda value: (value.center_y, -value.confidence)):
            existing_index = next(
                (
                    index
                    for index, existing in enumerate(row_best)
                    if abs(existing.center_y - line.center_y) <= max(existing.height, line.height)
                ),
                None,
            )
            if existing_index is None:
                row_best.append(line)
            elif line.confidence > row_best[existing_index].confidence:
                row_best[existing_index] = line

        if len(row_best) > 6:
            row_best = sorted(row_best, key=lambda line: line.confidence, reverse=True)[:6]
        row_best.sort(key=lambda line: line.center_y)

        return tuple(
            _candidate(
                field_key=field_key,
                value=_normalized_text(line.text),
                raw_text=line.text,
                confidence=line.confidence / 100.0,
                reason="tesseract_selection_opponent_line",
                frame_id=frame.frame_id,
            )
            for field_key, line in zip(
                SELECTION_OPPONENT_FIELD_KEYS, row_best, strict=False
            )
        )

    @staticmethod
    def _best_active_match(
        lines: tuple[OcrTextLine, ...],
        names: tuple[str, ...],
        *,
        prefer_top: bool,
        frame_height: int,
    ) -> tuple[str, OcrTextLine, float] | None:
        best: tuple[str, OcrTextLine, float] | None = None
        for line in lines:
            if line.confidence < _MIN_LINE_CONFIDENCE:
                continue
            for name in names:
                text_score = _similarity(line.text, name)
                position_match = line.center_y < frame_height / 2 if prefer_top else line.center_y >= frame_height / 2
                position_bonus = 0.07 if position_match else 0.0
                score = text_score * 0.88 + (line.confidence / 100.0) * 0.12 + position_bonus
                if best is None or score > best[2]:
                    best = (name, line, score)
        if best is None or best[2] < _MIN_ACTIVE_MATCH_SCORE:
            return None
        return best

    def _turn_candidates(
        self,
        frame: FramePacket,
        lines: tuple[OcrTextLine, ...],
        context: RealOcrCandidateContext,
    ) -> tuple[OcrCandidate, ...]:
        candidates: list[OcrCandidate] = []
        self_match = self._best_active_match(
            lines,
            context.self_active_candidates,
            prefer_top=False,
            frame_height=frame.height,
        )
        if self_match is not None:
            name, line, score = self_match
            candidates.append(
                _candidate(
                    field_key=OcrFieldKey.SELF_ACTIVE.value,
                    value=name,
                    raw_text=line.text,
                    confidence=score,
                    reason="tesseract_candidate_match_lower_half",
                    frame_id=frame.frame_id,
                )
            )
        opponent_match = self._best_active_match(
            lines,
            context.opponent_active_candidates,
            prefer_top=True,
            frame_height=frame.height,
        )
        if opponent_match is not None:
            name, line, score = opponent_match
            candidates.append(
                _candidate(
                    field_key=OcrFieldKey.OPPONENT_ACTIVE.value,
                    value=name,
                    raw_text=line.text,
                    confidence=score,
                    reason="tesseract_candidate_match_upper_half",
                    frame_id=frame.frame_id,
                )
            )

        hp_by_side: dict[str, tuple[OcrTextLine, float]] = {}
        for line in lines:
            ratio: float | None = None
            ratio_match = _RATIO_PATTERN.search(line.text)
            if ratio_match is not None:
                current = int(ratio_match.group("current"))
                maximum = int(ratio_match.group("maximum"))
                if maximum > 0 and current <= maximum:
                    ratio = current / maximum
            if ratio is None:
                percent_match = _PERCENT_PATTERN.search(line.text)
                if percent_match is not None:
                    percent = int(percent_match.group("percent"))
                    if 0 <= percent <= 100:
                        ratio = percent / 100.0
            if ratio is None:
                continue
            side = "opponent" if line.center_y < frame.height / 2 else "self"
            previous = hp_by_side.get(side)
            if previous is None or line.confidence > previous[0].confidence:
                hp_by_side[side] = (line, ratio)

        for side, field_key in (
            ("self", OcrFieldKey.SELF_HP.value),
            ("opponent", OcrFieldKey.OPPONENT_HP.value),
        ):
            hp_value = hp_by_side.get(side)
            if hp_value is None:
                continue
            line, ratio = hp_value
            candidates.append(
                _candidate(
                    field_key=field_key,
                    value=_hp_bucket_for_ratio(ratio),
                    raw_text=line.text,
                    confidence=line.confidence / 100.0,
                    reason="tesseract_explicit_hp_ratio",
                    frame_id=frame.frame_id,
                    raw_estimate=ratio,
                )
            )
        return tuple(candidates)


@dataclass(frozen=True, slots=True)
class _PendingRequest:
    frame: FramePacket
    context: OcrCandidateContext


class LatestOnlyAsyncOcrBackend:
    """One daemon worker, one replaceable pending request, no OCR backlog."""

    def __init__(self, delegate: TesseractCliOcrBackend) -> None:
        self._delegate = delegate
        self._condition = threading.Condition()
        self._pending: _PendingRequest | None = None
        self._in_flight = False
        self._closed = False
        self._latest_context: OcrCandidateContext | None = None
        self._latest_candidates: tuple[OcrCandidate, ...] = ()
        self._last_requested_frame_id: str | None = None
        self._submitted_count = 0
        self._completed_count = 0
        self._replaced_pending_count = 0
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="maple-next-ocr",
            daemon=True,
        )
        self._worker.start()

    def is_available(self) -> bool:
        return not self._closed and self._delegate.is_available()

    def generate_candidates(
        self, frame: FramePacket, context: OcrCandidateContext
    ) -> tuple[OcrCandidate, ...]:
        if self._closed or not self._delegate.is_available():
            return ()
        with self._condition:
            if frame.frame_id != self._last_requested_frame_id:
                image = frame.image.copy() if isinstance(frame.image, QImage) else frame.image
                detached_frame = replace(frame, image=image)
                if self._pending is not None:
                    self._replaced_pending_count += 1
                self._pending = _PendingRequest(detached_frame, context)
                self._last_requested_frame_id = frame.frame_id
                self._condition.notify()
            if self._latest_context == context:
                return self._latest_candidates
            return ()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify_all()
        self._worker.join(timeout=1.0)

    def metrics(self) -> dict[str, object]:
        delegate_metrics = self._delegate.metrics()
        with self._condition:
            return {
                **delegate_metrics,
                "ocr_async_in_flight": self._in_flight,
                "ocr_async_pending": self._pending is not None,
                "ocr_async_submitted_count": self._submitted_count,
                "ocr_async_completed_count": self._completed_count,
                "ocr_async_replaced_pending_count": self._replaced_pending_count,
            }

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                request = self._pending
                self._pending = None
                self._in_flight = True
                self._submitted_count += 1
            assert request is not None
            try:
                result = self._delegate.generate_candidates(request.frame, request.context)
            except Exception:  # noqa: BLE001 - worker must survive an OCR engine fault
                result = ()
            with self._condition:
                self._latest_context = request.context
                self._latest_candidates = result
                self._in_flight = False
                self._completed_count += 1


def build_default_ocr_backend() -> LatestOnlyAsyncOcrBackend:
    """Build the official fail-safe real OCR backend.

    Missing Tesseract or missing Japanese language data is represented through
    ``is_available() == False``; startup and manual entry remain available.
    """

    return LatestOnlyAsyncOcrBackend(TesseractCliOcrBackend())
