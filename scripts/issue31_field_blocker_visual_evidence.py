"""Create deterministic Issue #31 evidence from isolated pre-seeded fixtures.

This harness deliberately performs no operator lifecycle command.  It opens
two already-persisted canonical states and renders them with capture disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication

from maple_next.application.match_service import MatchApplication
from maple_next.capture.contracts import DeviceOpenResult, SourceFramePacket
from maple_next.domain.enums import BattleState, HpBucket
from maple_next.domain.models import (
    AppliedSelectionSnapshot,
    BattleSession,
    BattleTurn,
    SelectionFacts,
    TurnFactsSnapshot,
)
from maple_next.domain.species_ability_catalog import canonical_species_ability_catalog
from maple_next.domain.turn_state import (
    ConfirmationMeta,
    ConfirmedTurnState,
    Known,
    OpponentEntryEvent,
    ProvenanceStep,
    SideState,
    TurnIdentity,
)
from maple_next.persistence.sqlite import SQLiteRepository
from maple_next.ui.battle_record_ui import BattleRecordUiWindow
from maple_next.ui.dev_advice import MockSelectionAdviceAdapter, MockTurnAdviceAdapter
from maple_next.ui.turn_state_flow import TurnStateFlowController

SELF_TEAM = ("Meowscarada", "Gholdengo", "Dragonite", "Dondozo", "Flutter Mane", "Urshifu")
OPPONENT_TEAM = ("Salamence", "Gholdengo", "Dragonite", "Flutter Mane", "Tyranitar", "Pelipper")
MATCH_ID = "fixture-match-field-blocker"
FIXED_TIME = "2026-08-09T00:00:00+00:00"
T = TypeVar("T")


class _NoHardwareCaptureBackend:
    def __init__(self) -> None:
        self.start_count = 0

    def start(
        self,
        selector: str,
        on_frame: Callable[[SourceFramePacket], None] | None = None,
    ) -> DeviceOpenResult:
        del selector, on_frame
        self.start_count += 1
        return DeviceOpenResult(False, False, None, "CAPTURE_FORBIDDEN_IN_EVIDENCE")

    def stop(self) -> None:
        return None

    def get_latest_frame(self) -> SourceFramePacket | None:
        return None

    def is_running(self) -> bool:
        return False


def _known(value: T) -> Known[T]:
    return Known.confirmed(value, provenance_chain=(ProvenanceStep.HUMAN_INPUT,))


def _side(active: str) -> SideState:
    return SideState(
        active=_known(active),
        hp_bucket=_known(HpBucket.FULL),
        status=_known("NONE"),
        attack_stage=_known(0),
        defense_stage=_known(0),
        special_attack_stage=_known(0),
        special_defense_stage=_known(0),
        speed_stage=_known(0),
        accuracy_stage=_known(0),
        evasion_stage=_known(0),
        side_effects=_known(()),
    )


def _seed_fixture(database_path: Path, opponent_species: str) -> SQLiteRepository:
    repository = SQLiteRepository(database_path)
    catalog = canonical_species_ability_catalog()
    species = catalog.resolve_species(opponent_species)
    session_id = f"fixture-session-{species.species_id}"
    turn_id = f"fixture-turn-{species.species_id}"
    state_id = f"fixture-state-{species.species_id}"
    event = OpponentEntryEvent(
        event_id=f"fixture-entry-{species.species_id}",
        session_id=session_id,
        match_id=MATCH_ID,
        generation=1,
        entry_ordinal=1,
        confirmed_state_id=state_id,
        turn_id=turn_id,
        turn_number=1,
        species_id=species.species_id,
        species_name=species.name,
        opponent_entity_id=f"opponent-species:{species.species_id}",
    )
    with repository.transaction():
        repository.insert_session(
            BattleSession(
                session_id=session_id,
                match_id=MATCH_ID,
                generation=1,
                state=BattleState.TURN_REVIEWED,
                battle_revision=4,
                current_reviewed_selection_id="fixture-selection-facts",
                current_selection_advice_id="fixture-selection-advice",
                current_applied_selection_id="fixture-applied-selection",
                current_turn_id=turn_id,
            )
        )
        repository.append_selection_facts(
            session_id,
            SelectionFacts("fixture-selection-facts", SELF_TEAM, OPPONENT_TEAM),
        )
        repository.append_selection_advice(
            "fixture-selection-advice",
            session_id,
            "fixture-selection-job",
            SELF_TEAM[:3],
            SELF_TEAM[0],
            SELF_TEAM[1:3],
        )
        repository.append_applied_selection(
            session_id,
            AppliedSelectionSnapshot(
                "fixture-applied-selection",
                SELF_TEAM[:3],
                SELF_TEAM[0],
                SELF_TEAM[1:3],
                "fixture-selection-advice",
            ),
        )
        repository.append_turn(session_id, BattleTurn(turn_id, 1))
        repository.append_turn_facts(
            session_id,
            TurnFactsSnapshot(
                "fixture-turn-facts",
                turn_id,
                1,
                SELF_TEAM[0],
                species.name,
                HpBucket.FULL,
                HpBucket.FULL,
                ("Flower Trick",),
                SELF_TEAM[1:3],
                "pre-seeded deterministic evidence",
            ),
        )
        repository.append_confirmed_turn_state(
            ConfirmedTurnState(
                confirmed_state_id=state_id,
                identity=TurnIdentity(session_id, MATCH_ID, 1, turn_id, 1, 4),
                previous_confirmed_state_id=None,
                self_side=_side(SELF_TEAM[0]),
                opponent_side=_side(species.name),
                weather=_known("NONE"),
                terrain=_known("NONE"),
                confirmation=ConfirmationMeta(True, FIXED_TIME, "fixture-preseed"),
            )
        )
        repository.append_opponent_entry_event(event)
    return repository


def _render_fixture(
    app: QApplication,
    output_directory: Path,
    opponent_species: str,
    image_name: str,
) -> tuple[dict[str, object], Path]:
    state_directory = output_directory / opponent_species.lower()
    state_directory.mkdir(exist_ok=True)
    repository = _seed_fixture(state_directory / "evidence.db", opponent_species)
    selection_adapter = MockSelectionAdviceAdapter()
    turn_adapter = MockTurnAdviceAdapter()
    capture_backend = _NoHardwareCaptureBackend()
    controller = TurnStateFlowController(
        MatchApplication(repository, state_directory / "exports"),
        repository,
        selection_adapter,
        turn_adapter,
    )
    window = BattleRecordUiWindow(
        controller,
        ocr_data_directory=state_directory / "ocr",
        capture_backend=capture_backend,
        auto_start_capture=False,
    )
    window.setFixedSize(1920, 1080)
    window.header_tabs.setCurrentIndex(1)
    window.show()
    view = controller.refresh()
    window.render_view(view)
    app.processEvents()
    summary = controller.turn_state_summary()
    canonical_entry_event_identity = window._active_ability_entry_event_id
    if canonical_entry_event_identity is not None:
        persisted_event = summary.pending_opponent_entry_event
        if (
            persisted_event is None
            or persisted_event.event_id != canonical_entry_event_identity
        ):
            raise RuntimeError("UI prompt event identity is not the persisted pending event")
    candidates = tuple(button.text() for button in window.parity_ability_buttons)
    image_path = output_directory / image_name
    image = window.grab()
    if (image.width(), image.height()) != (1920, 1080):
        raise RuntimeError(f"unexpected evidence size: {image.width()}x{image.height()}")
    if not image.save(str(image_path), "PNG"):
        raise RuntimeError(f"failed to save {image_path}")
    result = {
        "species_id": canonical_species_ability_catalog()
        .resolve_species(opponent_species)
        .species_id,
        "projection_match_id": view.projection.match_id,
        "canonical_entry_event_identity": canonical_entry_event_identity,
        "prompt_visible": not window.parity_ability_card.isHidden(),
        "candidates": list(candidates),
        "match_header": window.battle_context_label.text(),
        "selection_provider_calls": selection_adapter.network_call_count,
        "turn_provider_calls": turn_adapter.network_call_count,
        "capture_start": capture_backend.start_count,
        "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
    }
    window.close()
    repository.close()
    return result, image_path


def main(output_directory: Path) -> int:
    if output_directory.exists():
        raise RuntimeError("evidence destination must be a fresh directory")
    output_directory.mkdir(parents=True)
    app = cast(QApplication | None, QApplication.instance()) or QApplication([])
    dragonite, _ = _render_fixture(
        app, output_directory, "Dragonite", "01-dragonite-no-ability-prompt.png"
    )
    salamence, _ = _render_fixture(
        app, output_directory, "Salamence", "02-salamence-entry-candidates.png"
    )
    if dragonite["prompt_visible"]:
        raise RuntimeError("Dragonite unexpectedly showed an entry-ability prompt")
    if not salamence["prompt_visible"]:
        raise RuntimeError("Salamence did not show its persisted entry-event prompt")
    if salamence["candidates"] != ["いかく", "じしんかじょう", "不明"]:
        raise RuntimeError(f"unexpected Salamence candidates: {salamence['candidates']!r}")
    if "#demo" in str(salamence["match_header"]):
        raise RuntimeError("demo match label remains visible")
    if MATCH_ID not in str(salamence["match_header"]):
        raise RuntimeError("authoritative projection.match_id is not visible")
    candidate_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest = {
        "candidate_sha": candidate_sha,
        "resolution": "1920x1080",
        "fixture": "isolated pre-seeded canonical persistence",
        "operator_commands": {"new_match": 0, "apply": 0, "capture": 0},
        "real_provider_send": 0,
        "network_send": 0,
        "game_action": 0,
        "dragonite": dragonite,
        "salamence": salamence,
    }
    (output_directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    destination = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("C:/tmp/maple-issue31-field-blockers")
    )
    raise SystemExit(main(destination))
