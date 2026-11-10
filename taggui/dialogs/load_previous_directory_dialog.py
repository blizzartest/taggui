from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QGroupBox, QLabel, QVBoxLayout)

from utils.settings import DEFAULT_SETTINGS


class LoadPreviousDirectoryDialog(QDialog):
    """Ask whether to load the previously opened directory and with which
    load mode. The check boxes are mutually exclusive; the currently active
    load mode is pre-selected."""

    LOAD_MODES = (('full', 'Full'), ('tags_only', 'Tags Only'),
                  ('untagged_only', 'Untagged Only'))

    def __init__(self, directory_path: Path, current_load_mode: str,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle('Load Previous Directory')
        if current_load_mode not in dict(self.LOAD_MODES):
            current_load_mode = DEFAULT_SETTINGS['load_mode']
        self.selected_load_mode = current_load_mode
        self.load_mode_check_boxes: dict[str, QCheckBox] = {}
        description_label = QLabel(
            f'Load the previously opened directory:\n{directory_path}\n\n'
            'This may take a while for large directories.')
        description_label.setWordWrap(True)
        load_mode_group_box = QGroupBox('Load Mode')
        load_mode_layout = QVBoxLayout(load_mode_group_box)
        for load_mode, label in self.LOAD_MODES:
            check_box = QCheckBox(label)
            check_box.setChecked(load_mode == current_load_mode)
            check_box.toggled.connect(
                lambda checked, mode=load_mode:
                self._on_load_mode_check_box_toggled(mode, checked))
            self.load_mode_check_boxes[load_mode] = check_box
            load_mode_layout.addWidget(check_box)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes
            | QDialogButtonBox.StandardButton.No)
        yes_button = self.button_box.button(QDialogButtonBox.StandardButton.Yes)
        yes_button.setDefault(True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(description_label)
        layout.addWidget(load_mode_group_box)
        layout.addWidget(self.button_box)

    def _on_load_mode_check_box_toggled(self, load_mode: str, checked: bool):
        if not checked:
            return
        self.selected_load_mode = load_mode
        for mode, check_box in self.load_mode_check_boxes.items():
            if mode != load_mode:
                check_box.setChecked(False)
