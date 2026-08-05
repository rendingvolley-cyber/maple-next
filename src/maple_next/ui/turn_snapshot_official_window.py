"""Official fail-closed wrapper for the Turn snapshot OCR window.

The base implementation owns the event-driven capture/OCR flow. This narrow
wrapper tightens operator-facing boundaries:

* every new capture or explicit retake immediately clears candidates and the
  previously frozen image, so stale controls cannot be used while the next
  immutable frame is being analysed;
* the inherited generic OCR-adopt handler is reached only when the current
  frozen bundle actually contains a candidate for that field;
* confirming Turn facts is one explicit human button action. The legacy
  duplicate checkbox is hidden and is not part of the official flow, while
  the separate Gemini send remains an independent trusted human action.
"""

from __future__ import annotations

from maple_next.capture.contracts import FramePacket
from maple_next.ocr.contracts import OcrFieldKey
from maple_next.ui.turn_snapshot_window import (
    TurnSnapshotMatchFlowWindow as _BaseTurnSnapshotMatchFlowWindow,
)


class TurnSnapshotMatchFlowWindow(_BaseTurnSnapshotMatchFlowWindow):
    """Official Turn screenshot window with stale-display protections."""

    def _build_turn_facts_group(self) -> None:
        super()._build_turn_facts_group()
        # The save button itself is the explicit human confirmation. Requiring
        # a checkbox immediately before the same action is redundant on every
        # Turn and does not add a distinct safety boundary.
        self.turn_facts_confirm_checkbox.setVisible(False)
        self.turn_facts_confirm_checkbox.setChecked(False)
        self.confirm_turn_facts_button.setText("Turn factsを確認して保存")

    def _clear_turn_snapshot_candidate_display(self, *, reset_origins: bool) -> None:
        self._latest_ocr_bundle = None
        for label in self._ocr_field_labels.values():
            label.setText("—")
        for button in self._ocr_adopt_buttons.values():
            button.setEnabled(False)
        self.turn_snapshot_image_label.clear()
        self.turn_snapshot_image_label.setText("固定画像なし")
        self.turn_snapshot_frame_label.setText("—")
        for label in self._turn_snapshot_crop_labels.values():
            label.clear()
            label.setText("—")
        if reset_origins:
            for field_key in self._turn_snapshot_field_locks:
                self._turn_snapshot_field_locks[field_key] = False
                self._turn_snapshot_origins[field_key] = "未入力"
        self._render_turn_snapshot_origins()

    def _submit_frozen_turn_frame(
        self,
        *,
        frame: FramePacket | None,
        status: str,
        message: str,
        reset_draft: bool,
    ) -> None:
        # Clear stale candidate controls before assigning the next identity or
        # displaying the next frame. A retake preserves human field locks and
        # typed values, while a genuinely new Turn resets origin/lock metadata.
        self._clear_turn_snapshot_candidate_display(reset_origins=reset_draft)
        super()._submit_frozen_turn_frame(
            frame=frame,
            status=status,
            message=message,
            reset_draft=False,
        )

    def _update_turn_fact_button(self, _checked: bool = False) -> None:
        self.turn_facts_confirm_checkbox.setVisible(False)
        self.confirm_turn_facts_button.setEnabled(
            self._persistence_reads_allowed
            and getattr(self, "_turn_facts_editable", False)
        )

    def _on_confirm_turn_facts(self, _checked: bool = False) -> None:
        if not self._mutation_slots_allowed():
            return
        moves = [field.text().strip() for field in self.move_inputs if field.text().strip()]
        switches = [
            checkbox.text() for checkbox in self.switch_checkboxes if checkbox.isChecked()
        ]
        view = self._controller.confirm_turn_facts(
            self_active=self.self_active_box.currentText(),
            opponent_active=self.opponent_active_input.text(),
            self_hp=self.self_hp_box.currentText(),
            opponent_hp=self.opponent_hp_box.currentText(),
            legal_moves=moves,
            legal_switches=switches,
            human_note=self.turn_note_input.text(),
            human_confirmed=True,
        )
        self.render_view(view)

    def _on_adopt_ocr_candidate(self, field_key: str) -> None:
        if not self._mutation_slots_allowed() or self._latest_ocr_bundle is None:
            return
        if field_key not in {
            OcrFieldKey.SELF_ACTIVE.value,
            OcrFieldKey.OPPONENT_ACTIVE.value,
            OcrFieldKey.SELF_HP.value,
            OcrFieldKey.OPPONENT_HP.value,
        }:
            return
        if not any(
            candidate.field_key == field_key
            for candidate in self._latest_ocr_bundle.candidates
        ):
            return
        super()._on_adopt_ocr_candidate(field_key)
