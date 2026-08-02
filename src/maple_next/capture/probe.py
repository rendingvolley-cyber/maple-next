"""Bounded hardware probe CLI for UGREEN direct capture.

Usage:
    python -m maple_next.capture.probe --selector UGREEN --timeout-ms 5000

Prints one line of sanitized JSON and exits. Makes zero OBS/provider/game
calls. Never prints raw device IDs or exception text. Always releases the
camera lease before exiting, even on timeout or error.

Absence of UGREEN hardware on a dev machine is an expected, valid,
manual-safe result - not a failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence

from maple_next.capture.contracts import DEFAULT_DEVICE_SELECTOR
from maple_next.capture.qt_ugreen import QtMultimediaUgreenBackend
from maple_next.capture.service import CaptureService


def _ensure_qt_application() -> object | None:
    """Best-effort QGuiApplication for camera/event-loop support.

    Returns None (and lets the backend degrade to DEVICE_UNAVAILABLE) if Qt
    GUI infrastructure is not importable/usable in this environment.
    """

    try:
        from PySide6.QtGui import QGuiApplication

        existing = QGuiApplication.instance()
        if existing is not None:
            return existing
        return QGuiApplication([])
    except Exception:  # noqa: BLE001 - probe must never crash on env issues
        return None


def _run_probe(selector: str, timeout_ms: int) -> dict[str, object]:
    _ensure_qt_application()
    backend = QtMultimediaUgreenBackend()
    service = CaptureService(backend, selector=selector)
    try:
        status = service.start()
        device_found = status.status not in {"DEVICE_UNAVAILABLE"}
        deadline = time.monotonic() + (timeout_ms / 1000.0)

        frame_received = False
        fresh = False
        width: int | None = None
        height: int | None = None

        while time.monotonic() < deadline:
            status = service.latest_status()
            if status.frame_id is not None:
                frame_received = True
                fresh = bool(status.fresh)
                width = status.width
                height = status.height
                break
            time.sleep(0.05)

        result_status = status.status
        return {
            "status": result_status,
            "device_found": device_found,
            "frame_received": frame_received,
            "fresh": fresh,
            "width": width,
            "height": height,
            "manual_entry_allowed": True,
        }
    finally:
        service.stop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded UGREEN capture probe")
    parser.add_argument("--selector", default=DEFAULT_DEVICE_SELECTOR)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args(argv)

    timeout_ms = min(max(args.timeout_ms, 0), 5000)
    result = _run_probe(args.selector, timeout_ms)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
