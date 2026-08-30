"""Human-only editor for Pokemon Champions detailed team builds.

The dialog is intentionally a draft editor.  Confirming it returns an
immutable :class:`ChampionsTeamBuild` to the caller; it never opens files,
reads SQLite, contacts a provider, or writes canonical state by itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from maple_next.domain.team_build import (
    CHAMPIONS_BATTLE_FORMAT,
    CHAMPIONS_GAME,
    CHAMPIONS_SCHEMA_VERSION,
    CHAMPIONS_SCHEMA_VERSION_V2,
    CHAMPIONS_SCHEMA_VERSION_V3,
    CHAMPIONS_STAT_NAMES,
    CHAMPIONS_STAT_POINT_PER_STAT_MAX,
    CHAMPIONS_STAT_POINT_TOTAL_MAX,
    ChampionsPokemonBuild,
    ChampionsStatPoints,
    ChampionsTeamBuild,
    TeamSelectionProfile,
)


class _MemberPanel(QGroupBox):
    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(f"Pokemon {index + 1}", parent)
        self.pokemon_name_input = QLineEdit()
        self.pokemon_name = self.pokemon_name_input
        self.move_inputs = [QLineEdit() for _ in range(4)]
        self.moves = self.move_inputs
        self.held_item_input = QLineEdit()
        self.held_item = self.held_item_input
        self.ability_input = QLineEdit()
        self.ability = self.ability_input
        self.nature_input = QLineEdit()
        self.nature = self.nature_input
        self.stat_spinboxes: dict[str, QSpinBox] = {}
        self.stat_inputs = self.stat_spinboxes
        self.used_label = QLabel()
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)

        form = QFormLayout(self)
        form.addRow("Pokemon", self.pokemon_name_input)
        for index, field in enumerate(self.move_inputs, start=1):
            form.addRow(f"Move {index}", field)
        form.addRow("Held item", self.held_item_input)
        form.addRow("Ability", self.ability_input)
        form.addRow("Nature", self.nature_input)

        stats = QWidget()
        stats_layout = QGridLayout(stats)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        for column, field_name in enumerate(CHAMPIONS_STAT_NAMES):
            label = QLabel(field_name.replace("special_", "S."))
            spinbox = QSpinBox()
            spinbox.setRange(0, CHAMPIONS_STAT_POINT_PER_STAT_MAX)
            spinbox.setSingleStep(1)
            spinbox.valueChanged.connect(self._update_point_labels)
            self.stat_spinboxes[field_name] = spinbox
            stats_layout.addWidget(label, 0, column)
            stats_layout.addWidget(spinbox, 1, column)
        form.addRow("Stat points", stats)
        form.addRow("Points", self.used_label)
        form.addRow("Warning", self.warning_label)
        self._update_point_labels()

    def _update_point_labels(self, _value: int = 0) -> None:
        total = sum(spin.value() for spin in self.stat_spinboxes.values())
        remaining = CHAMPIONS_STAT_POINT_TOTAL_MAX - total
        self.used_label.setText(f"{total} / {CHAMPIONS_STAT_POINT_TOTAL_MAX}")
        if total > CHAMPIONS_STAT_POINT_TOTAL_MAX:
            self.warning_label.setText("Too many stat points")
        elif remaining:
            self.warning_label.setText(f"{remaining} stat points unspent")
        else:
            self.warning_label.setText("")

    def set_build(self, build: ChampionsPokemonBuild) -> None:
        self.pokemon_name_input.setText(build.pokemon_name)
        for field, value in zip(self.move_inputs, build.moves, strict=True):
            field.setText(value)
        for field in self.move_inputs[len(build.moves) :]:
            field.clear()
        self.held_item_input.setText(build.held_item or "")
        self.ability_input.setText(build.ability)
        self.nature_input.setText(build.nature)
        for name in CHAMPIONS_STAT_NAMES:
            self.stat_spinboxes[name].setValue(getattr(build.stat_points, name))

    def read_build(self) -> ChampionsPokemonBuild:
        moves = tuple(field.text() for field in self.move_inputs if field.text().strip())
        held_item = self.held_item_input.text().strip() or None
        stats = ChampionsStatPoints(
            **{name: self.stat_spinboxes[name].value() for name in CHAMPIONS_STAT_NAMES}
        )
        return ChampionsPokemonBuild(
            pokemon_name=self.pokemon_name_input.text(),
            moves=moves,
            held_item=held_item,
            ability=self.ability_input.text(),
            nature=self.nature_input.text(),
            stat_points=stats,
        )


class ChampionsTeamBuildEditor(QDialog):
    """A six-member draft editor with no persistence side effects."""

    def __init__(
        self,
        initial_build: ChampionsTeamBuild | None = None,
        *,
        pokemon_names: Sequence[str] | None = None,
        persistence_reads_allowed: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pokemon Champions team details")
        self.resize(1000, 800)
        self.persistence_reads_allowed = persistence_reads_allowed
        self._staged_build: ChampionsTeamBuild | None = None
        self._draft_schema_version = CHAMPIONS_SCHEMA_VERSION
        self._selection_profile: TeamSelectionProfile | None = None
        self._last_error: str | None = None
        self.member_panels: list[_MemberPanel] = []
        self.panels = self.member_panels
        self.member_editors = self.member_panels
        self.member_widgets = self.member_panels
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Enter six Pokemon and their confirmed details."))
        self.team_name_input = QLineEdit("Pokemon Champions team")
        self.team_name = self.team_name_input
        name_form = QFormLayout()
        name_form.addRow("Team name", self.team_name_input)
        root.addLayout(name_form)
        root.addWidget(self.status_label)
        root.addWidget(self.error_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        for index in range(6):
            panel = _MemberPanel(index)
            self.member_panels.append(panel)
            grid.addWidget(panel, index // 2, index % 2)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm_button = self.save_button
        if self.save_button is not None:
            self.save_button.setText("Confirm details")
            self.save_button.setEnabled(False)
        self.cancel_button = self.button_box.button(QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self._on_confirm)
        self.button_box.rejected.connect(self.reject)
        root.addWidget(self.button_box)

        for panel in self.member_panels:
            panel.pokemon_name_input.textChanged.connect(self._update_save_enabled)
            panel.ability_input.textChanged.connect(self._update_save_enabled)
            panel.nature_input.textChanged.connect(self._update_save_enabled)
            for field in panel.move_inputs:
                field.textChanged.connect(self._update_save_enabled)
            panel.held_item_input.textChanged.connect(self._update_save_enabled)
            for spinbox in panel.stat_spinboxes.values():
                spinbox.valueChanged.connect(self._update_save_enabled)
        self.team_name_input.textChanged.connect(self._update_save_enabled)
        if initial_build is not None:
            self.team_name_input.setText(initial_build.name)
            self.set_build(initial_build)
        elif pokemon_names is not None:
            self.set_pokemon_names(pokemon_names)
        self._update_save_enabled()

    @property
    def staged_build(self) -> ChampionsTeamBuild | None:
        return self._staged_build

    @property
    def team_build(self) -> ChampionsTeamBuild | None:
        return self._staged_build

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def set_pokemon_names(self, names: Sequence[str]) -> None:
        if len(names) != 6:
            raise ValueError("pokemon_names must contain exactly six entries")
        for panel, name in zip(self.member_panels, names, strict=True):
            panel.pokemon_name_input.setText(name)
        self._update_save_enabled()

    def set_build(self, build: ChampionsTeamBuild) -> None:
        if build.schema_version not in {
            CHAMPIONS_SCHEMA_VERSION_V2,
            CHAMPIONS_SCHEMA_VERSION_V3,
        }:
            raise ValueError("unsupported team build schema")
        self._draft_schema_version = build.schema_version
        self._selection_profile = build.selection_profile
        for panel, member in zip(self.member_panels, build.members, strict=True):
            panel.set_build(member)
        self._update_save_enabled()

    def build_from_inputs(self) -> ChampionsTeamBuild:
        members = tuple(panel.read_build() for panel in self.member_panels)
        name = self.team_name_input.text().strip() or "Pokemon Champions team"
        return ChampionsTeamBuild(
            schema_version=self._draft_schema_version,
            game=CHAMPIONS_GAME,
            name=name,
            battle_format=CHAMPIONS_BATTLE_FORMAT,
            members=members,
            selection_profile=self._selection_profile,
        )

    def collect_build(self) -> ChampionsTeamBuild:
        return self.build_from_inputs()

    def _update_save_enabled(self, _value: object = None) -> None:
        valid = True
        try:
            self.build_from_inputs()
        except (TypeError, ValueError):
            valid = False
        if self.save_button is not None:
            self.save_button.setEnabled(valid)
        for panel in self.member_panels:
            panel._update_point_labels()

    def _on_confirm(self) -> None:
        # This guard must be first: direct invocation in a persistence-fallback
        # render is a no-op and cannot reach a dialog, DB, filesystem, or provider.
        if not self.persistence_reads_allowed:
            return
        try:
            build = self.build_from_inputs()
        except (TypeError, ValueError):
            self._last_error = "team details are incomplete or invalid"
            self.error_label.setText(self._last_error)
            self.error_label.setVisible(True)
            return
        self._staged_build = build
        self._last_error = None
        self.error_label.clear()
        self.error_label.setVisible(False)
        self.status_label.setText(
            f"{build.sha256()} · {len(build.unspent_point_warnings())} member warning(s)"
        )
        self.accept()

    # Test- and controller-friendly names for the human confirmation slot.
    def confirm(self) -> ChampionsTeamBuild | None:
        self._on_confirm()
        return self._staged_build

    def accept_build(self) -> ChampionsTeamBuild | None:
        return self.confirm()


TeamBuildEditor = ChampionsTeamBuildEditor
