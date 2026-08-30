"""Integration tests for Opponent INTEL v2 (Bundle C/D/E).

Covers: confirmed-facts precedence over population stats, OBSERVED marking
independent of usage percentage, observed-move isolation across two
different opponent species within one match, the source/freshness footer,
and that a stale snapshot only warns (never disables a Turn control).

Follows the plain-pytest / shared QApplication.instance() style already
used in tests/test_issue31_battle_record_v5.py -- no pytest-qt.
"""

from __future__ import annotations

import os
from pathlib import Path

from maple_next.domain.legal_switches import LegalSwitchStatus as _B2_LegalSwitchStatus

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel
from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.opponent_intel import OpponentMetaSnapshot, RankedUsage
from maple_next.ui.battle_record_ui import _ranked_chart_entries
from maple_next.ui.turn_state_flow import TurnStateFlowController


def _confirm_legal_switches_honestly(window) -> None:
    """Bundle 2 R1-F: an honest fixture default for tests that do not
    themselves exercise legal-switch behavior. Reviews the *real* derived
    candidates for the current binding and confirms exactly that set --
    never a fabricated CONFIRMED_NONE used only to clear the gate. When the
    team fixture genuinely has no legal switch candidates, this still
    produces CONFIRMED_NONE, but because it is actually empty."""

    controller = window._bundle_c_controller  # noqa: SLF001
    candidates = controller.derive_legal_switch_candidates()
    status = (
        _B2_LegalSwitchStatus.CONFIRMED_NONEMPTY
        if candidates
        else _B2_LegalSwitchStatus.CONFIRMED_NONE
    )
    controller._application.confirm_legal_switches(  # noqa: SLF001
        legal_switches=candidates, status=status, human_confirmed=True
    )


class _StaticMetaProvider:
    def __init__(self, snapshot: OpponentMetaSnapshot | None) -> None:
        self.snapshot = snapshot

    def get(self, species: str) -> OpponentMetaSnapshot | None:
        if self.snapshot is None or self.snapshot.species != species:
            return None
        return self.snapshot


def _advance_to_action_result_for_species(window, species: str) -> None:
    """Same flow as ``_advance_to_action_result`` but for a chosen species."""

    _fill_minimal_current_state(window)
    window.opponent_active_input.setText(species)
    window._on_confirm_turn_facts()  # noqa: SLF001
    _confirm_legal_switches_honestly(window)
    window.mock_turn_action_type_box.setCurrentText("MOVE")
    window.mock_turn_action_name_box.setCurrentText("Flower Trick")
    window.mock_turn_prediction_input.setText("manual test prediction")
    window.mock_turn_rationale_input.setText("fake transport only")
    window._on_trusted_send_turn_to_gemini()  # noqa: SLF001


def test_confirmed_facts_render_above_population_stats(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window._opponent_meta_provider = _StaticMetaProvider(  # noqa: SLF001
        OpponentMetaSnapshot(
            species="Garchomp",
            regulation="M-5/single",
            snapshot_date="2026-08-01",
            source="pokechamdb",
            moves=(RankedUsage("Earthquake", 90.0),),
            abilities=(RankedUsage("Sand Veil", 55.0),),
            items=(RankedUsage("Choice Scarf", 20.0),),
        )
    )
    _advance_to_action_result_for_species(window, "Garchomp")
    window.self_action_move_buttons[0].click()
    window.opponent_action_tabs["MOVE"].click()
    window.parity_opponent_action_input.setText("じしん")
    window._on_record_action()  # noqa: SLF001

    widget = window.opponent_intel_widget
    assert widget._view is not None  # noqa: SLF001
    assert widget._view.observed_moves == ("じしん",)  # noqa: SLF001
    # The confirmed-fact chip row is laid out before the charts row in the
    # widget's top-to-bottom QVBoxLayout -- confirmed facts stay visually
    # above/ahead of the population-statistics section.
    layout = widget.layout()
    # QLayout.indexOf only works for widgets, not layouts directly added via
    # addLayout; assert ordering through the underlying items instead.
    item_widgets = [layout.itemAt(i) for i in range(layout.count())]
    fact_row_pos = next(
        i for i, item in enumerate(item_widgets) if item.layout() is widget.fact_chip_row
    )
    chart_pos = next(
        i for i, item in enumerate(item_widgets) if item.widget() is widget.chart_section
    )
    assert fact_row_pos < chart_pos
    assert "じしん" in widget.fact_chips["moves"].text()
    assert "✓" in widget.fact_chips["moves"].text()
    repository.close()


def test_observed_move_marked_regardless_of_population_percentage(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    # A 100% usage move is still only population statistic until observed;
    # a 1% move that WAS observed this match must still be marked OBSERVED.
    window._opponent_meta_provider = _StaticMetaProvider(  # noqa: SLF001
        OpponentMetaSnapshot(
            species="Garchomp",
            regulation="M-5/single",
            snapshot_date="2026-08-01",
            source="pokechamdb",
            moves=(RankedUsage("じしん", 1.0), RankedUsage("げきりん", 99.0)),
        )
    )
    _advance_to_action_result_for_species(window, "Garchomp")
    window.self_action_move_buttons[0].click()
    window.opponent_action_tabs["MOVE"].click()
    window.parity_opponent_action_input.setText("じしん")
    window._on_record_action()  # noqa: SLF001

    widget = window.opponent_intel_widget
    assert widget._view is not None  # noqa: SLF001
    assert widget._view.observed_moves == ("じしん",)  # noqa: SLF001
    assert "げきりん" not in widget._view.observed_moves  # noqa: SLF001
    repository.close()


def test_observed_move_never_leaks_across_opponent_species_in_same_match(
    tmp_path: Path,
) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()

    species_a = OPPONENT_TEAM[0]
    species_b = OPPONENT_TEAM[1]
    assert species_a != species_b

    _advance_to_action_result_for_species(window, species_a)
    window.self_action_move_buttons[0].click()
    window.opponent_action_tabs["MOVE"].click()
    window.parity_opponent_action_input.setText("じしん")
    window._on_record_action()  # noqa: SLF001

    assert controller.opponent_match_facts(species_a).moves == ("じしん",)
    assert controller.opponent_match_facts(species_b).moves == ()

    window._on_next_turn()  # noqa: SLF001
    _advance_to_action_result_for_species(window, species_b)

    # Species A's observed move must never surface for species B: neither
    # in the live INTEL view/fact chips nor in a fresh species-B facts query
    # (the latter is naturally empty too, since a species-A-scoped query no
    # longer applies once a different species is the confirmed active).
    widget = window.opponent_intel_widget
    assert widget._view is not None  # noqa: SLF001
    assert widget._view.species == species_b  # noqa: SLF001
    assert widget._view.observed_moves == ()  # noqa: SLF001
    assert "じしん" not in widget.fact_chips["moves"].text()
    assert controller.opponent_match_facts(species_b).moves == ()
    repository.close()


def test_footer_renders_source_regulation_and_freshness_fields(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window._opponent_meta_provider = _StaticMetaProvider(  # noqa: SLF001
        OpponentMetaSnapshot(
            species="Garchomp",
            regulation="M-5/single",
            snapshot_date="2026-08-01",
            source="pokechamdb",
            source_updated_at="2026-08-01T00:00:00+00:00",
            fetched_at="2026-08-05T00:00:00+00:00",
        )
    )
    _advance_to_action_result_for_species(window, "Garchomp")

    footer = window.opponent_intel_widget.footer_label.text()
    assert "M-5/single" in footer
    assert "pokechamdb" in footer
    assert "2026-08-01" in footer
    assert "2026-08-05" in footer
    repository.close()


def test_stale_snapshot_warns_but_never_disables_turn_controls(tmp_path: Path) -> None:
    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window._opponent_meta_provider = _StaticMetaProvider(  # noqa: SLF001
        OpponentMetaSnapshot(
            species="Garchomp",
            regulation="M-5/single",
            snapshot_date="2020-01-01",
            source="pokechamdb",
            source_updated_at="2020-01-01T00:00:00+00:00",
            fetched_at="2020-01-01T00:00:00+00:00",
        )
    )
    _advance_to_action_result_for_species(window, "Garchomp")
    window.self_action_move_buttons[0].click()

    footer = window.opponent_intel_widget.footer_label.text()
    assert "⚠" in footer
    # Staleness must never disable/block any Turn-flow control.
    assert window.actual_action_confirm_checkbox.isChecked()
    assert window.record_action_button.isEnabled()
    repository.close()


def test_catalog_only_ability_never_shows_observed_badge_end_to_end(tmp_path: Path) -> None:
    """R3R1 BUG 1, case 10: a species entered via population/catalog data
    alone (no human-confirmed ability memory) must never show the
    observed/confirmed badge in the real bar chart, even when the
    catalog/legal-possibility text happens to equal a usage-stats entry."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window._opponent_meta_provider = _StaticMetaProvider(  # noqa: SLF001
        OpponentMetaSnapshot(
            species="Garchomp",
            regulation="M-5/single",
            snapshot_date="2026-08-01",
            source="pokechamdb",
            abilities=(RankedUsage("Sand Veil", 100.0),),
        )
    )
    _advance_to_action_result_for_species(window, "Garchomp")

    widget = window.opponent_intel_widget
    view = widget._view  # noqa: SLF001
    assert view is not None
    assert view.ability_confirmed is False
    _moves, abilities, _items = _ranked_chart_entries(view)
    assert abilities  # the 100% entry is present...
    assert all(not is_observed for _name, _pct, is_observed in abilities)  # ...never badged
    repository.close()


def test_match_confirmed_ability_shows_observed_badge_end_to_end(tmp_path: Path) -> None:
    """R3R1 BUG 1, case 11: a genuinely human-confirmed ability (persisted
    via the real opponent-ability-memory path) DOES show the observed badge
    in the real bar chart."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    species = "Garchomp"
    entity_id = TurnStateFlowController.opponent_entity_id_for_species(species)
    session = repository.load_active_session()
    assert session is not None
    with repository.transaction():
        repository.set_opponent_ability_memory(
            session_id=session.session_id,
            match_id=session.match_id,
            generation=session.generation,
            opponent_entity_id=entity_id,
            species=species,
            ability="Sand Veil",
        )
    window._opponent_meta_provider = _StaticMetaProvider(  # noqa: SLF001
        OpponentMetaSnapshot(
            species=species,
            regulation="M-5/single",
            snapshot_date="2026-08-01",
            source="pokechamdb",
            abilities=(RankedUsage("Sand Veil", 100.0),),
        )
    )
    _advance_to_action_result_for_species(window, species)

    widget = window.opponent_intel_widget
    view = widget._view  # noqa: SLF001
    assert view is not None
    assert view.ability == "Sand Veil"
    assert view.ability_confirmed is True
    _moves, abilities, _items = _ranked_chart_entries(view)
    assert any(
        name == "Sand Veil" and is_observed for name, _pct, is_observed in abilities
    )
    repository.close()


def test_main_panel_top_n_limits_retained(tmp_path: Path) -> None:
    """R3R1 case 15: the main panel's intentional top-N limits (moves 5,
    abilities 3, items 5) are unaffected by this repair."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    window._opponent_meta_provider = _StaticMetaProvider(  # noqa: SLF001
        OpponentMetaSnapshot(
            species="Garchomp",
            regulation="M-5/single",
            snapshot_date="2026-08-01",
            source="pokechamdb",
            moves=tuple(RankedUsage(f"Move{i}", float(20 - i)) for i in range(8)),
            abilities=tuple(RankedUsage(f"Ability{i}", float(10 - i)) for i in range(6)),
            items=tuple(RankedUsage(f"Item{i}", float(20 - i)) for i in range(8)),
        )
    )
    _advance_to_action_result_for_species(window, "Garchomp")

    view = window.opponent_intel_widget._view  # noqa: SLF001
    assert view is not None
    moves, abilities, items = _ranked_chart_entries(view)
    assert len(moves) == 5
    assert len(abilities) == 3
    assert len(items) == 5
    # Sorting is truthful: highest percentage first.
    assert [name for name, _pct, _obs in moves] == ["Move0", "Move1", "Move2", "Move3", "Move4"]
    repository.close()


def test_intel_detail_dialog_shows_more_than_five_entries(tmp_path: Path) -> None:
    """R3R1 BUG 2 / case 16: the detail dialog exposes the FULL underlying
    ranked list, not the main panel's top-5 truncation, when the source
    data has more than 5 entries. Source data itself is unchanged."""

    repository, controller, window, _transport = build_window(tmp_path)
    _advance_to_turn_capture_pending(controller)
    window.render_view()
    move_count = 9
    window._opponent_meta_provider = _StaticMetaProvider(  # noqa: SLF001
        OpponentMetaSnapshot(
            species="Garchomp",
            regulation="M-5/single",
            snapshot_date="2026-08-01",
            source="pokechamdb",
            moves=tuple(RankedUsage(f"Move{i}", float(90 - i)) for i in range(move_count)),
        )
    )
    _advance_to_action_result_for_species(window, "Garchomp")

    widget = window.opponent_intel_widget
    view = widget._view  # noqa: SLF001
    assert view is not None
    assert view.meta is not None
    assert len(view.meta.moves) == move_count  # source data unchanged

    widget._open_detail()  # noqa: SLF001
    moves_section = widget._detail_sections["moves"]  # noqa: SLF001
    # Rank-row QLabels live inside a scroll area's inner container, not
    # directly under the section -- search the whole subtree.
    rank_labels = [
        label for label in moves_section.findChildren(QLabel) if label.property("rankRow")
    ]
    assert len(rank_labels) == move_count
    assert len(rank_labels) > 5
    widget._detail_dialog.close()  # noqa: SLF001
    repository.close()
