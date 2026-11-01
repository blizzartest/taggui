import logging
import os
import sys
import traceback
import warnings

import transformers
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication, QMessageBox

from utils.settings import get_settings
from utils.theme import apply_theme
from utils.version import VERSION
from widgets.main_window import MainWindow


def suppress_warnings():
    """Suppress all warnings when not in a development environment."""
    environment = os.getenv('TAGGUI_ENVIRONMENT')
    if environment == 'development':
        print('Running in development environment.')
        return
    logging.basicConfig(level=logging.ERROR)
    warnings.simplefilter('ignore')
    transformers.logging.set_verbosity_error()
    try:
        import auto_gptq
        auto_gptq_logger = logging.getLogger(auto_gptq.modeling._base.__name__)
        auto_gptq_logger.setLevel(logging.ERROR)
    except ImportError:
        pass


def run_gui():
    app = QApplication([])
    # The application name is shown in the taskbar.
    app.setApplicationName('TagGUI')
    # The application display name is shown in the title bar.
    app.setApplicationDisplayName('TagGUI')
    app.setApplicationVersion(VERSION)
    app.setStyle('Fusion')
    # Disable the allocation limit to allow loading large images.
    QImageReader.setAllocationLimit(0)
    settings = get_settings()
    theme = settings.value('theme', defaultValue='System', type=str)
    apply_theme(app, theme)
    main_window = MainWindow(app)
    main_window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    # Prevent PyTorch from opening multiple windows when running inside a
    # PyInstaller bundle.
    if len(sys.argv) > 1 and 'compile_worker' in sys.argv[1]:
        import runpy

        sys.argv = sys.argv[1:]
        runpy.run_path(sys.argv[0], run_name='__main__')
        sys.exit(0)
    suppress_warnings()
    try:
        run_gui()
    except Exception as exception:
        settings = get_settings()
        settings.clear()
        error_message_box = QMessageBox()
        error_message_box.setWindowTitle('Error')
        error_message_box.setIcon(QMessageBox.Icon.Critical)
        error_message_box.setText(str(exception))
        error_message_box.setDetailedText(traceback.format_exc())
        error_message_box.exec()
        raise exception
