"""Hardware-free screenshots of the redesigned Opponent INTEL v2 panel and the
move autocomplete popup, for reviewer evidence. No provider network, no game
action, no OBS -- pure offscreen Qt rendering of fixture data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget  # noqa: E402

from maple_next.domain.move_catalog import MoveMatcher  # noqa: E402
from maple_next.domain.opponent_intel import (  # noqa: E402
    MatchOpponentFacts,
    OpponentMetaSnapshot,
    RankedUsage,
    build_opponent_intel,
)
from maple_next.ui.battle_record_ui import _OpponentIntelWidget  # noqa: E402
from maple_next.ui.move_autocomplete import MoveAutocompletePopup  # noqa: E402


class _FixedMetaProvider:
    def __init__(self, snapshot: OpponentMetaSnapshot) -> None:
        self._snapshot = snapshot

    def get(self, species: str) -> OpponentMetaSnapshot | None:
        return self._snapshot


def _capture(app: QApplication, widget: QWidget, path: Path) -> None:
    widget.updateGeometry()
    for _ in range(3):
        app.processEvents()
    widget.repaint()
    app.processEvents()
    pixmap = widget.grab()
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"failed to save {path}")


def main(output_directory: Path) -> int:
    output_directory.mkdir(parents=True, exist_ok=True)
    app = cast(QApplication, QApplication.instance() or QApplication([]))

    snapshot = OpponentMetaSnapshot(
        species="ガチグマ(アカツキ)",
        regulation="M-5/Single",
        snapshot_date="2026-08-05",
        source="pokechamdb",
        moves=(
            RankedUsage("じしん", 99.0),
            RankedUsage("スケイルショット", 49.0),
            RankedUsage("ステルスロック", 46.0),
            RankedUsage("つるぎのまい", 37.0),
            RankedUsage("インファイト", 22.0),
        ),
        abilities=(RankedUsage("さめはだ", 61.0), RankedUsage("ヨガパワー", 39.0)),
        items=(RankedUsage("オボンのみ", 30.0), RankedUsage("こだわりハチマキ", 25.0)),
        natures=(RankedUsage("いじっぱり", 70.0),),
        partners=(RankedUsage("パオジアン", 18.0),),
        source_url="https://pokechamdb.com/pokemon/ursaluna-bloodmoon",
        source_updated_at="2026-08-05",
        fetched_at="2026-08-05T12:00:00Z",
        ranking=3.0,
    )
    facts = MatchOpponentFacts(ability="さめはだ", item=None, moves=("じしん", "スケイルショット"))
    view = build_opponent_intel(
        species=snapshot.species, match_facts=facts, provider=_FixedMetaProvider(snapshot)
    )

    intel = _OpponentIntelWidget()
    intel.resize(420, 640)
    intel.render_intel(view)
    intel.show()
    _capture(app, intel, output_directory / "01-opponent-intel-v2-panel.png")

    host = QWidget()
    host.resize(420, 80)
    layout = QVBoxLayout(host)
    field = QLineEdit()
    field.setPlaceholderText("相手の実際の行動（任意）")
    layout.addWidget(field)
    host.show()

    matcher = MoveMatcher(["じしん", "しんそく", "スケイルショット", "つるぎのまい", "インファイト"])
    popup = MoveAutocompletePopup(field, lambda: matcher)
    field.setText("じし")
    for _ in range(5):
        app.processEvents()
    _capture(app, host, output_directory / "02-autocomplete-field-before-popup.png")
    if popup.isVisible():
        popup.adjustSize()
        popup.updateGeometry()
        for _ in range(3):
            app.processEvents()
        popup.repaint()
        app.processEvents()
        popup_pixmap = popup.grab()
        popup_pixmap.save(str(output_directory / "03-autocomplete-popup-open.png"), "PNG")
        print(f"popup_size={popup.size().width()}x{popup.size().height()}")

    print(f"popup_visible={popup.isVisible()} field_text_len={len(field.text())}")
    print("game_action=0 provider_send=0 obs=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/tmp/maple-intel-v2-evidence")))
