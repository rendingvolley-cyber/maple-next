"""Widget-level tests for maple_next.ui.move_autocomplete.MoveAutocompletePopup.

No pytest-qt: constructs/introspects widgets directly against the
session-wide QApplication.instance() singleton, matching the style used
throughout tests/test_issue31_battle_record_v5.py.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QLineEdit

from maple_next.domain.move_catalog import MoveMatcher
from maple_next.ui.move_autocomplete import MoveAutocompletePopup

# A QWidget must never be constructed before a QApplication exists -- ensure
# one is up before any test in this module builds a QLineEdit/popup.
QApplication.instance() or QApplication([])


def _press(field: QLineEdit, popup: MoveAutocompletePopup, key: Qt.Key) -> None:
    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    popup.eventFilter(field, event)


def test_typing_populates_candidates_without_mutating_field_text() -> None:
    QApplication.instance() or QApplication([])
    field = QLineEdit()
    matcher = MoveMatcher(["じしん", "じならし", "げきりん"])
    popup = MoveAutocompletePopup(field, matcher)

    field.setText("じし")
    assert field.text() == "じし"
    assert popup._list.count() >= 1  # noqa: SLF001
    field.deleteLater()
    popup.deleteLater()


def test_empty_candidates_hides_popup() -> None:
    QApplication.instance() or QApplication([])
    field = QLineEdit()
    matcher = MoveMatcher(["じしん"])
    popup = MoveAutocompletePopup(field, matcher)

    field.setText("じしん")
    assert popup.isVisible()
    field.setText("ｚｚｚｚｚｚｚｚ存在しない技名")
    assert not popup.isVisible()
    field.deleteLater()
    popup.deleteLater()


def test_click_commits_selected_candidate() -> None:
    QApplication.instance() or QApplication([])
    field = QLineEdit()
    matcher = MoveMatcher(["じしん", "じならし"])
    popup = MoveAutocompletePopup(field, matcher)
    committed: list[str] = []
    popup.committed.connect(committed.append)

    field.setText("じ")
    assert popup._list.count() >= 1  # noqa: SLF001
    item = popup._list.item(0)  # noqa: SLF001
    expected_name = item.text()
    popup._commit_item(item)  # noqa: SLF001

    assert field.text() == expected_name
    assert committed == [expected_name]
    assert not popup.isVisible()
    field.deleteLater()
    popup.deleteLater()


def test_enter_on_highlighted_row_commits() -> None:
    QApplication.instance() or QApplication([])
    field = QLineEdit()
    matcher = MoveMatcher(["じしん", "じならし"])
    popup = MoveAutocompletePopup(field, matcher)
    committed: list[str] = []
    popup.committed.connect(committed.append)

    field.setText("じ")
    _press(field, popup, Qt.Key.Key_Down)
    _press(field, popup, Qt.Key.Key_Return)

    assert committed
    assert field.text() == committed[0]
    assert not popup.isVisible()
    field.deleteLater()
    popup.deleteLater()


def test_escape_closes_without_commit() -> None:
    QApplication.instance() or QApplication([])
    field = QLineEdit()
    matcher = MoveMatcher(["じしん"])
    popup = MoveAutocompletePopup(field, matcher)
    committed: list[str] = []
    popup.committed.connect(committed.append)

    field.setText("じ")
    assert popup.isVisible()
    _press(field, popup, Qt.Key.Key_Escape)

    assert not popup.isVisible()
    assert committed == []
    assert field.text() == "じ"
    field.deleteLater()
    popup.deleteLater()


def test_typing_alone_never_mutates_draft_even_with_visible_popup() -> None:
    QApplication.instance() or QApplication([])
    field = QLineEdit()
    matcher = MoveMatcher(["じしん", "じならし", "じわれ"])
    popup = MoveAutocompletePopup(field, matcher)

    for partial in ("じ", "じし", "じしん"):
        field.setText(partial)
        assert field.text() == partial
    field.deleteLater()
    popup.deleteLater()


def test_missing_or_corrupt_catalog_never_raises_and_shows_nothing() -> None:
    QApplication.instance() or QApplication([])
    field = QLineEdit()

    def broken_matcher_source() -> MoveMatcher:
        raise RuntimeError("move_catalog.json is corrupt")

    popup = MoveAutocompletePopup(field, broken_matcher_source)
    field.setText("じしん")  # must not raise
    assert not popup.isVisible()
    field.deleteLater()
    popup.deleteLater()


def test_set_boosts_updates_ranking_without_rebuilding_matcher() -> None:
    QApplication.instance() or QApplication([])
    field = QLineEdit()
    matcher = MoveMatcher(["じしんA", "じしんB"])
    popup = MoveAutocompletePopup(field, matcher)

    popup.set_boosts({"じしんB": 50.0})
    field.setText("じしん")
    assert popup._list.item(0).text() == "じしんB"  # noqa: SLF001
    field.deleteLater()
    popup.deleteLater()
