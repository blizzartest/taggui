import re
from pathlib import Path

import imagesize
from PySide6.QtCore import QFile, Qt
from PySide6.QtWidgets import (QDialog, QGridLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from models.image_list_model import ImageListModel
from utils.settings_widgets import SettingsBigCheckBox
from utils.utils import ConfirmationDialog, pluralize
from utils.video import get_video_metadata, is_video_path

# Copy markers that browsers and operating systems append to file names,
# like 'image (1).png' or 'image - Copy.png'.
DUPLICATE_NAME_PATTERN = re.compile(
    r'[\s_-]+(copy(\s*\(\d+\))?|\(\d+\))$', re.IGNORECASE)


def get_base_stem(path: Path) -> str:
    """Return the file name without the directory, extension, and any
    trailing copy markers like ' (1)' or ' - Copy (2)'."""
    stem = path.stem
    while True:
        stripped_stem = DUPLICATE_NAME_PATTERN.sub('', stem)
        if stripped_stem == stem:
            break
        stem = stripped_stem
    return stem.lower()


def get_pixel_count(dimensions: tuple[int, int] | None) -> int:
    if not dimensions:
        return -1
    return dimensions[0] * dimensions[1]


def get_dimensions(path: Path) -> tuple[int, int] | None:
    """Read the dimensions of an image or video, or None if they could not
    be read (e.g. in tags-only mode where files are not opened)."""
    try:
        if is_video_path(path):
            return get_video_metadata(path)
        return imagesize.get(path)
    except Exception:
        return None


class FindDuplicateImagesDialog(QDialog):
    """Finds images/videos that share the same base file name and identical
    tags, and deletes all but the highest-resolution copy of each group."""

    def __init__(self, parent, image_list_model: ImageListModel,
                 directory_path: Path):
        super().__init__(parent)
        self.image_list_model = image_list_model
        self.directory_path = directory_path
        self.deleted_image_paths: list = []
        self.duplicate_groups: list[list] = []
        self.setWindowTitle('Find Duplicate Images')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)
        description_label = QLabel(
            'Finds images and videos that have the same tags (and the same '
            'base file name) and deletes all but the highest-resolution copy '
            'of each duplicate group. Deleted files are moved to the trash.')
        description_label.setWordWrap(True)
        layout.addWidget(description_label)
        grid_layout = QGridLayout()
        grid_layout.addWidget(QLabel('Match by'), 0, 0,
                              Qt.AlignmentFlag.AlignRight)
        self.match_tags_check_box = SettingsBigCheckBox(
            key='find_duplicates_match_tags', default=True, text='Same tags')
        self.match_name_check_box = SettingsBigCheckBox(
            key='find_duplicates_match_name', default=True,
            text='Same base file name')
        self.match_dimensions_check_box = SettingsBigCheckBox(
            key='find_duplicates_match_dimensions', default=False,
            text='Same dimensions')
        grid_layout.addWidget(self.match_tags_check_box, 0, 1,
                              Qt.AlignmentFlag.AlignLeft)
        grid_layout.addWidget(self.match_name_check_box, 1, 1,
                              Qt.AlignmentFlag.AlignLeft)
        grid_layout.addWidget(self.match_dimensions_check_box, 2, 1,
                              Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(grid_layout)
        self.find_button = QPushButton('Find Duplicates')
        self.delete_button = QPushButton(
            'Delete All But Highest Resolution Copy')
        self.delete_button.setEnabled(False)
        self.results_text_edit = QPlainTextEdit()
        self.results_text_edit.setReadOnly(True)
        layout.addWidget(self.find_button)
        layout.addWidget(self.results_text_edit)
        layout.addWidget(self.delete_button)
        self.find_button.clicked.connect(self.find_duplicates)
        self.delete_button.clicked.connect(self.delete_duplicates)
        self.find_duplicates()

    def find_duplicates(self):
        """Group the loaded images by the selected match criteria and show
        the groups that contain more than one image."""
        if self.image_list_model.load_mode == 'tags_only':
            images = []
            for image_path, tags in self.image_list_model.tag_index.items():
                images.append(type('Image', (), {
                    'path': image_path, 'tags': tags,
                    'dimensions': get_dimensions(image_path)})())
        else:
            images = self.image_list_model.images
        groups: dict[tuple, list] = {}
        for image in images:
            key_parts = []
            if self.match_tags_check_box.isChecked():
                key_parts.append(tuple(sorted(image.tags)))
            if self.match_name_check_box.isChecked():
                key_parts.append(get_base_stem(image.path))
            if self.match_dimensions_check_box.isChecked():
                key_parts.append(image.dimensions)
            if not key_parts:
                # Nothing to match by; use the full path so every group has
                # a single image.
                key_parts.append(str(image.path))
            key = tuple(key_parts)
            groups.setdefault(key, []).append(image)
        self.duplicate_groups = [
            group for group in groups.values() if len(group) > 1]
        self.duplicate_groups.sort(key=lambda group: str(group[0].path))
        if not self.duplicate_groups:
            self.results_text_edit.setPlainText(
                'No duplicate images were found.')
            self.delete_button.setEnabled(False)
            return
        result_lines = []
        duplicate_image_count = sum(
            len(group) for group in self.duplicate_groups)
        result_lines.append(
            f'{len(self.duplicate_groups)} duplicate '
            f'{pluralize("group", len(self.duplicate_groups))} with '
            f'{duplicate_image_count} '
            f'{pluralize("image", duplicate_image_count)} in total:')
        for group in self.duplicate_groups:
            kept_image = max(group, key=lambda image: get_pixel_count(
                image.dimensions))
            result_lines.append('')
            for image in group:
                if image.dimensions:
                    dimensions_text = (f'{image.dimensions[0]}'
                                       f'x{image.dimensions[1]}')
                else:
                    dimensions_text = 'unknown dimensions'
                marker = (' (kept)' if image is kept_image else ' (delete)')
                result_lines.append(
                    f'{image.path.name} [{dimensions_text}]{marker}')
        self.results_text_edit.setPlainText('\n'.join(result_lines))
        self.delete_button.setEnabled(True)

    def delete_duplicates(self):
        """Move all images except the highest-resolution copy of each
        duplicate group to the trash, along with their caption files."""
        duplicate_image_count = sum(
            len(group) for group in self.duplicate_groups)
        confirmation_dialog = ConfirmationDialog(
            title='Delete Duplicate Images',
            question=(f'Delete {duplicate_image_count} duplicate '
                      f'{pluralize("image", duplicate_image_count)} '
                      f'(all but the highest-resolution copy of each group)?'))
        if confirmation_dialog.exec() != QMessageBox.StandardButton.Yes:
            return
        deleted_image_paths = []
        for group in self.duplicate_groups:
            kept_image = max(group, key=lambda image: get_pixel_count(
                image.dimensions))
            for image in group:
                if image is kept_image:
                    continue
                image_file = QFile(image.path)
                if image_file.moveToTrash():
                    deleted_image_paths.append(image.path)
                else:
                    QMessageBox.critical(
                        self, 'Error', f'Failed to delete {image.path}.')
                caption_file_path = image.path.with_suffix('.txt')
                caption_file = QFile(caption_file_path)
                if caption_file.exists():
                    if not caption_file.moveToTrash():
                        QMessageBox.critical(
                            self, 'Error',
                            f'Failed to delete {caption_file_path}.')
        self.deleted_image_paths = deleted_image_paths
        message_box = QMessageBox(self)
        message_box.setWindowTitle('Delete Duplicate Images')
        message_box.setIcon(QMessageBox.Icon.Information)
        message_box.setText(
            f'Moved {len(deleted_image_paths)} '
            f'{pluralize("image", len(deleted_image_paths))} to the trash.')
        message_box.exec()
        self.accept()
