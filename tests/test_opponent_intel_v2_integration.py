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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_issue31_turn_state_ui_bundle_c import (
    OPPONENT_TEAM,
    _advance_to_turn_capture_pending,
    _fill_minimal_current_state,
    build_window,
)

from maple_next.domain.opponent_intel import OpponentMetaSnapshot, RankedUsage


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
