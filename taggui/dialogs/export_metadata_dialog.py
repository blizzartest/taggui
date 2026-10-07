"""Dialog for exporting tags to image metadata."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel,
                               QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout)

from utils.metadata import check_exiftool_available, export_tags_to_metadata_for_directory


class ExportMetadataDialog(QDialog):
    """Dialog for exporting tags to image metadata."""

    def __init__(self, directory_path: Path, parent=None):
        super().__init__(parent)
        self.directory_path = directory_path
        self.setWindowTitle('Export Tags to Metadata')
        self.setMinimumWidth(400)
        
        self.description_label = QLabel(
            'Export tags from .txt files to image metadata.\n'
            'This allows you to search for tags in file explorers and software like XnView.'
        )
        self.description_label.setWordWrap(True)
        
        self.method_label = QLabel()
        self._update_method_label()
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(True)
        
        self.export_button = QPushButton('Export Tags to Metadata')
        self.export_button.setDefault(True)
        
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        
        layout = QVBoxLayout()
        layout.addWidget(self.description_label)
        layout.addWidget(self.method_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.export_button)
        layout.addWidget(self.button_box)
        self.setLayout(layout)
        
        self.button_box.clicked.connect(self.close)
        self.export_button.clicked.connect(self._export_metadata)

    def _update_method_label(self):
        if check_exiftool_available():
            self.method_label.setText(
                'Using exiftool (IPTC and XMP metadata)'
            )
        else:
            self.method_label.setText(
                'Using Pillow/piexif (EXIF metadata only)'
            )

    def _export_metadata(self):
        if not self.directory_path:
            QMessageBox.warning(self, 'Error', 'No directory selected.')
            return
        
        reply = QMessageBox.question(
            self,
            'Confirm Export',
            f'Export tags to metadata for all images in:\n{self.directory_path}\n\n'
            'Tags will be appended to existing metadata in the images.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        self.export_button.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._perform_export)

    def _perform_export(self):
        try:
            successful, failed = export_tags_to_metadata_for_directory(
                self.directory_path, use_exiftool=True
            )
            
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.export_button.setEnabled(True)
            
            total = successful + failed
            if total == 0:
                QMessageBox.information(
                    self, 'Export Complete',
                    'No images with corresponding .txt files were found.'
                )
            else:
                message = f'Successfully exported tags to {successful} image(s).'
                if failed > 0:
                    message += f'\nFailed to export {failed} image(s).'
                QMessageBox.information(self, 'Export Complete', message)
        
        except Exception as e:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.export_button.setEnabled(True)
            QMessageBox.critical(self, 'Error', f'Error during export: {str(e)}')
