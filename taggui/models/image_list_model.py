import random
import re
import sys
import threading
import queue
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import exifread
import imagesize

from utils.image import Image
from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.tag_store import (get_tags_storage_mode, load_tags_from_database,
                             migrate_txt_tags_to_database as migrate_db,
                             write_tags_to_database)
from utils.utils import get_confirmation_dialog_reply, pluralize
from PySide6.QtCore import (QAbstractListModel, QModelIndex, QSize, Qt, Signal,
                            Slot, QTimer)
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import QMessageBox

UNDO_STACK_SIZE = 32
SEARCH_RESULT_BATCH_SIZE = 5
THUMBNAIL_WORKER_COUNT = 4
THUMBNAIL_QUEUE_CAPACITY = 192
THUMBNAIL_FLUSH_INTERVAL_MS = 180


def get_image_paths(directory_path: Path, image_suffixes: set[str]) -> set[Path]:
    """
    Recursively get all image file paths in a directory, including those in
    subdirectories. Filters for image files directly during traversal.
    """
    image_paths = set()
    for path in directory_path.iterdir():
        if path.is_file() and path.suffix.lower() in image_suffixes:
            image_paths.add(path)
        elif path.is_dir():
            image_paths.update(get_image_paths(path, image_suffixes))
    return image_paths


def get_text_file_paths(directory_path: Path, tags_subfolder: str = '') -> set[Path]:
    """
    Recursively get all .txt file paths in a directory, including those in
    subdirectories. Also checks the tags subfolder if specified.
    """
    text_paths = set()
    for path in directory_path.iterdir():
        if path.is_file() and path.suffix == '.txt':
            text_paths.add(path)
        elif path.is_dir():
            text_paths.update(get_text_file_paths(path, tags_subfolder))
    # Also check the tags subfolder for .txt files
    if tags_subfolder:
        tags_dir = directory_path / tags_subfolder
        if tags_dir.exists():
            for path in tags_dir.iterdir():
                if path.is_file() and path.suffix == '.txt':
                    text_paths.add(path)
                elif path.is_dir():
                    text_paths.update(get_text_file_paths(path, tags_subfolder))
    return text_paths


def load_image_metadata(image_path: Path, tag_separator: str,
                         text_file_paths: set[str],
                         tags_subfolder: str = '') -> tuple[Path, tuple[int, int] | None, list[str]]:
    """
    Load metadata for a single image in a thread-safe manner.
    Reads dimensions and EXIF orientation in a single pass.
    
    Returns: (path, dimensions, tags)
    """
    dimensions = None
    try:
        # Single-pass: get dimensions first
        dimensions = imagesize.get(image_path)
        
        # Check EXIF orientation and rotate dimensions if necessary
        # Use the file path directly - exifread handles opening efficiently
        try:
            with open(image_path, 'rb') as image_file:
                exif_tags = exifread.process_file(
                    image_file, details=False,
                    stop_tag='Image Orientation')
                if 'Image Orientation' in exif_tags:
                    orientations = exif_tags['Image Orientation'].values
                    if any(value in orientations for value in (5, 6, 7, 8)):
                        dimensions = (dimensions[1], dimensions[0])
        except Exception as exception:
            print(f'Failed to get Exif tags for {image_path}: '
                  f'{exception}', file=sys.stderr)
    except (ValueError, OSError) as exception:
        print(f'Failed to get dimensions for {image_path}: '
              f'{exception}', file=sys.stderr)
        dimensions = None
    
    # Load tags from .txt file
    tags = []
    # Check both the main directory and the tags subfolder
    possible_text_paths = []
    if tags_subfolder:
        tags_dir = image_path.parent / tags_subfolder
        possible_text_paths.append(tags_dir / image_path.with_suffix('.txt').name)
    possible_text_paths.append(image_path.with_suffix('.txt'))
    
    for text_file_path in possible_text_paths:
        if str(text_file_path) in text_file_paths:
            try:
                caption = text_file_path.read_text(encoding='utf-8', errors='replace')
                if caption:
                    tags = caption.split(tag_separator)
                    tags = [tag.strip() for tag in tags]
                    tags = [tag for tag in tags if tag]
                    break  # Found tags, stop looking
            except OSError as exception:
                print(f'Failed to read tags for {image_path}: '
                      f'{exception}', file=sys.stderr)
    
    return (image_path, dimensions, tags)


@dataclass
class HistoryItem:
    action_name: str
    tags: list[list[str]]
    should_ask_for_confirmation: bool


class Scope(str, Enum):
    ALL_IMAGES = 'All images'
    FILTERED_IMAGES = 'Filtered images'
    SELECTED_IMAGES = 'Selected images'


def txt_to_image_path(text_path: Path, tags_subfolder: str,
                       image_suffixes: set[str] | None = None) -> Path | None:
    """Convert a .txt file path back to its corresponding image path."""
    # Remove .txt suffix
    base_name = text_path.stem
    parent_dir = text_path.parent
    
    # If the text file is in a tags subfolder, the image is in the parent
    if tags_subfolder and parent_dir.name == tags_subfolder:
        parent_dir = parent_dir.parent
    
    # Determine which suffixes to try
    if image_suffixes is None:
        # Default common image suffixes
        image_suffixes = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.webp'}
    
    # Find the corresponding image file
    for suffix in image_suffixes:
        image_path = parent_dir / (base_name + suffix)
        if image_path.exists():
            return image_path
    
    # Also check if the text file is named exactly like the image
    # (e.g., image.jpg.txt)
    if text_path.suffix == '.txt':
        # Try removing .txt and checking if the result is a valid image
        possible_image_path = parent_dir / text_path.stem
        if possible_image_path.exists() and possible_image_path.suffix.lower() in image_suffixes:
            return possible_image_path
        
        # Also handle double extensions like "image.jpg.txt"
        stem_stem = text_path.stem
        if '.' in stem_stem:
            # Try splitting at the last dot
            possible_image_path = parent_dir / stem_stem
            if possible_image_path.exists() and possible_image_path.suffix.lower() in image_suffixes:
                return possible_image_path
    
    return None


class ImageListModel(QAbstractListModel):
    update_undo_and_redo_actions_requested = Signal()
    search_batch_loaded = Signal(list, int)

    def __init__(self, image_list_image_width: int, tag_separator: str):
        super().__init__()
        self.image_list_image_width = image_list_image_width
        self.tag_separator = tag_separator
        self.images: list[Image] = []
        self.tag_index: dict[Path, list[str]] = {}
        self.undo_stack = deque(maxlen=UNDO_STACK_SIZE)
        self.redo_stack = []
        self.proxy_image_list_model = None
        self.image_list_selection_model = None
        self.load_mode = 'full'
        self.directory_path = None
        self.image_suffixes = set()
        self.tags_storage_mode = get_tags_storage_mode()
        self.database_dirty = False
        self.database_save_timer = QTimer(self)
        self.database_save_timer.setSingleShot(True)
        self.database_save_timer.setInterval(500)
        self.database_save_timer.timeout.connect(self.save_tag_database)
        self.search_thread = None
        self.load_generation = 0
        self.pending_thumbnail_rows = set()
        self.failed_thumbnail_paths = set()
        self.thumbnail_task_lock = threading.Lock()
        self.thumbnail_task_queue = queue.PriorityQueue()
        self.completed_thumbnails = queue.Queue(maxsize=THUMBNAIL_QUEUE_CAPACITY)
        self.thumbnail_task_sequence = 0
        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(THUMBNAIL_FLUSH_INTERVAL_MS)
        self.flush_timer.timeout.connect(self.flush_completed_thumbnails)
        self.search_batch_loaded.connect(self.add_search_batch)
        for _ in range(THUMBNAIL_WORKER_COUNT):
            threading.Thread(target=self.thumbnail_worker_loop,
                            daemon=True).start()

    def rowCount(self, parent=None) -> int:
        return len(self.images)

    def data(self, index, role=None) -> Image | str | QIcon | QSize:
        row = index.row()
        image = self.images[row]
        if role == Qt.ItemDataRole.UserRole:
            return image
        if role == Qt.ItemDataRole.DisplayRole:
            # The text shown next to the thumbnail in the image list.
            text = image.path.name
            if image.tags:
                caption = self.tag_separator.join(image.tags)
                text += f'\n{caption}'
            return text
        if role == Qt.ItemDataRole.DecorationRole:
            # The thumbnail. If the image already has a thumbnail stored, use
            # it. Otherwise, generate a thumbnail and save it to the image.
            if image.thumbnail:
                return image.thumbnail
            if not image.is_fully_loaded:
                # In tags-only mode, generate the thumbnail in the
                # background so that the list stays responsive. Visible
                # images are prioritized over the preloading of the rest of
                # the folder.
                self.submit_thumbnail_task(image, row, to_front=True)
                if not self.flush_timer.isActive():
                    self.flush_timer.start()
                return None
            image_reader = QImageReader(str(image.path))
            # Rotate the image based on the orientation tag.
            image_reader.setAutoTransform(True)
            pixmap = QPixmap.fromImageReader(image_reader).scaledToWidth(
                self.image_list_image_width,
                Qt.TransformationMode.SmoothTransformation)
            thumbnail = QIcon(pixmap)
            image.thumbnail = thumbnail
            return thumbnail
        if role == Qt.ItemDataRole.SizeHintRole:
            if image.thumbnail:
                return image.thumbnail.availableSizes()[0]
            dimensions = image.dimensions
            if not dimensions:
                return QSize(self.image_list_image_width,
                             self.image_list_image_width)
            width, height = dimensions
            # Scale the dimensions to the image width.
            return QSize(self.image_list_image_width,
                         int(self.image_list_image_width * height / width))

    def load_thumbnail_for_image(self, image: Image) -> QImage | None:
        """Load and generate a thumbnail image for a single image."""
        image_reader = QImageReader(str(image.path))
        image_reader.setAutoTransform(True)
        qimage = image_reader.read()
        if qimage.isNull():
            return None
        return qimage.scaledToWidth(
            self.image_list_image_width,
            Qt.TransformationMode.SmoothTransformation)

    def submit_thumbnail_task(self, image: Image, row: int,
                              to_front: bool = False):
        """
        Queue an image for thumbnail generation in the background. Tasks for
        images that are currently visible are put at the front of the queue
        so that they are prioritized over the preloading of the rest of the
        folder. Tasks are deduplicated per row, so moving through the list
        does not queue duplicates.
        """
        if (image.thumbnail or row in self.pending_thumbnail_rows
                or image.path in self.failed_thumbnail_paths):
            return
        self.pending_thumbnail_rows.add(row)
        generation = self.load_generation
        # Visible images get priority 0 and are generated first; preloading
        # tasks get priority 1. The sequence number keeps the order stable
        # within each priority class.
        priority = 0 if to_front else 1
        with self.thumbnail_task_lock:
            self.thumbnail_task_sequence += 1
            sequence = self.thumbnail_task_sequence
        self.thumbnail_task_queue.put((priority, sequence, image, row,
                                       generation))

    def thumbnail_worker_loop(self):
        while True:
            priority, sequence, image, row, generation = (
                self.thumbnail_task_queue.get())
            if generation != self.load_generation:
                self.completed_thumbnails.put((image, None, row, generation))
                continue
            try:
                thumbnail = self.load_thumbnail_for_image(image)
            except Exception:
                thumbnail = None
            # The completed queue is bounded; block until the GUI thread has
            # flushed enough results. This throttles the workers so that the
            # GUI thread is never flooded with thumbnail updates.
            self.completed_thumbnails.put((image, thumbnail, row, generation))

    @Slot()
    def flush_completed_thumbnails(self):
        """
        Store all thumbnails that were completed in the background since the
        last flush and emit a single data-changed signal for them. This
        limits the repaint work on the GUI thread to one update per flush
        interval.
        """
        first_row = None
        last_row = None
        while True:
            try:
                image, thumbnail, row, generation = (
                    self.completed_thumbnails.get_nowait())
            except queue.Empty:
                break
            self.pending_thumbnail_rows.discard(row)
            if not (0 <= row < len(self.images)) or self.images[row] is not image:
                # The image list changed while the thumbnail was being
                # generated. Queue the image that is now at this row, if any.
                if 0 <= row < len(self.images):
                    self.submit_thumbnail_task(self.images[row], row,
                                               to_front=True)
                continue
            if generation != self.load_generation:
                continue
            if thumbnail is None:
                # Decoding failed; do not try again to avoid an endless
                # retry loop for broken images.
                self.failed_thumbnail_paths.add(image.path)
                continue
            image.thumbnail = QIcon(QPixmap.fromImage(thumbnail))
            image.is_fully_loaded = True
            if first_row is None or row < first_row:
                first_row = row
            if last_row is None or row > last_row:
                last_row = row
        if first_row is not None:
            self.dataChanged.emit(self.index(first_row),
                                  self.index(last_row))
        if (self.completed_thumbnails.empty()
                and self.thumbnail_task_queue.empty()
                and not self.pending_thumbnail_rows):
            # Nothing is queued, in flight or pending, so the flush timer can
            # stop until the next thumbnail task is submitted.
            self.flush_timer.stop()

    def load_directory(self, directory_path: Path):
        self.save_tag_database()
        self.database_save_timer.stop()
        self.cancel_background_loading()
        self.beginResetModel()
        self.images.clear()
        self.tag_index.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.update_undo_and_redo_actions_requested.emit()
        self.directory_path = directory_path
        
        settings = get_settings()
        image_suffixes_string = settings.value(
            'image_list_file_formats',
            defaultValue=DEFAULT_SETTINGS['image_list_file_formats'], type=str)
        image_suffixes = set()
        for suffix in image_suffixes_string.split(','):
            suffix = suffix.strip().lower()
            if not suffix.startswith('.'):
                suffix = '.' + suffix
            image_suffixes.add(suffix)
        
        tags_subfolder = settings.value(
            'tags_subfolder',
            defaultValue=DEFAULT_SETTINGS['tags_subfolder'], type=str)
        self.image_suffixes = image_suffixes
        
        self.tags_storage_mode = get_tags_storage_mode()
        
        # Always build tag index
        self.build_tag_index(directory_path, image_suffixes, tags_subfolder)
        
        if self.load_mode == 'full':
            # Load all images with full data (existing behavior)
            image_paths = get_image_paths(directory_path, image_suffixes)
            if self.tags_storage_mode == 'single_file':
                # Tags come from the tag index (which is backed by the tag
                # database), so no per-image caption files need to be read.
                text_file_path_strings = set()
            else:
                text_file_paths = get_text_file_paths(
                    directory_path, tags_subfolder)
                text_file_path_strings = {str(path)
                                          for path in text_file_paths}
            
            # Use ThreadPoolExecutor for parallel loading
            num_workers = min(4, len(image_paths)) if image_paths else 1
            
            loaded_images = []
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                # Submit all image loading tasks
                future_to_path = {
                    executor.submit(load_image_metadata, image_path, self.tag_separator,
                                   text_file_path_strings, tags_subfolder): image_path
                    for image_path in sorted(image_paths)
                }
                
                # Collect results as they complete
                for future in as_completed(future_to_path):
                    image_path, dimensions, tags = future.result()
                    if self.tags_storage_mode == 'single_file':
                        tags = self.tag_index.get(image_path, [])
                    image = Image(image_path, dimensions, tags, None, True)
                    loaded_images.append(image)
            
            # Sort by path (already sorted from input, but ensure consistency)
            loaded_images.sort(key=lambda image_: image_.path)
            
            self.images = loaded_images
        
        self.endResetModel()

    def build_tag_index(self, directory_path: Path, image_suffixes: set[str],
                        tags_subfolder: str):
        """Build an index of image paths to their tags.

        In single-file mode the index is loaded from the tag database
        (tags.jsonl); .txt files are only used as a fallback when no
        database exists yet.
        """
        image_paths = get_image_paths(directory_path, image_suffixes)
        
        if self.tags_storage_mode == 'single_file':
            self.tag_index = load_tags_from_database(
                directory_path, self.tag_separator)
            if self.tag_index:
                return
        
        self._build_tag_index_from_txt_files(directory_path, image_suffixes,
                                              tags_subfolder)

    def _build_tag_index_from_txt_files(self, directory_path: Path,
                                         image_suffixes: set[str],
                                         tags_subfolder: str):
        """Build the tag index from individual .txt caption files."""
        text_file_paths = get_text_file_paths(directory_path, tags_subfolder)
        image_paths = get_image_paths(directory_path, image_suffixes)

        # Build a set of all valid image paths for quick lookup
        valid_image_paths = {str(p) for p in image_paths}
        
        def load_tags_from_text_file(text_path: Path):
            image_path = txt_to_image_path(text_path, tags_subfolder,
                                           image_suffixes)
            if image_path is None or str(image_path) not in valid_image_paths:
                return None
            try:
                caption = text_path.read_text(encoding='utf-8',
                                              errors='replace')
            except OSError as exception:
                print(f'Failed to read tags for {text_path}: '
                      f'{exception}', file=sys.stderr)
                return None
            if not caption:
                return None
            tags = caption.split(self.tag_separator)
            return image_path, [tag.strip() for tag in tags if tag.strip()]

        # Read the text files in parallel; this is the slowest part of
        # building the tag index.
        num_workers = min(4, len(text_file_paths)) if text_file_paths else 1
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            for result in executor.map(load_tags_from_text_file,
                                        text_file_paths):
                if result is not None:
                    image_path, tags = result
                    self.tag_index[image_path] = tags

    def image_matches_filter(self, image_path: Path, tags: list[str],
                              filter_):
        """Check if an image matches the given filter expression."""
        # Create a temporary Image object for filtering
        temp_image = Image(image_path, None, tags)
        
        # Use the proxy model's filtering logic if available
        if self.proxy_image_list_model:
            return self.proxy_image_list_model.does_image_match_filter(
                temp_image, filter_)
        
        # Fallback: simple string matching
        if filter_ is None:
            return True
        
        if isinstance(filter_, str):
            caption = self.tag_separator.join(tags)
            return filter_.lower() in caption.lower()
        
        return True

    def load_matching_images(self, filter_):
        """Load full image data only for images matching the filter."""
        self.cancel_background_loading()
        self.beginResetModel()
        self.images.clear()
        self.endResetModel()

        if filter_ is None:
            return

        self.load_generation += 1
        generation = self.load_generation

        def worker():
            # Find matching paths from the tag index and append them in
            # batches. This is done in the background so that even very
            # large tag indexes do not block the interface. The tags are
            # already in the tag index and thumbnails are generated on
            # demand, so no file I/O is needed.
            matching_images = []
            for image_path, tags in self.tag_index.items():
                if generation != self.load_generation:
                    return
                if self.image_matches_filter(image_path, tags, filter_):
                    matching_images.append((image_path, tags))
            if not matching_images:
                return
            matching_images.sort(key=lambda path_and_tags: path_and_tags[0])
            batch = []
            for image_path, tags in matching_images:
                if generation != self.load_generation:
                    return
                batch.append(Image(image_path, None, tags.copy(), None,
                                  False))
                if len(batch) >= SEARCH_RESULT_BATCH_SIZE:
                    self.search_batch_loaded.emit(batch, generation)
                    batch = []
            if batch and generation == self.load_generation:
                self.search_batch_loaded.emit(batch, generation)

        self.search_thread = threading.Thread(target=worker, daemon=True)
        self.search_thread.start()

    @Slot(list, int)
    def add_search_batch(self, batch: list[Image], generation: int):
        """Append a batch of search results on the GUI thread."""
        if generation != self.load_generation:
            return
        first_row = len(self.images)
        self.beginInsertRows(QModelIndex(), first_row,
                             first_row + len(batch) - 1)
        self.images.extend(batch)
        self.endInsertRows()
        # Preload the thumbnails of the new images in the background.
        for row, image in enumerate(batch, start=first_row):
            self.submit_thumbnail_task(image, row)
        if not self.flush_timer.isActive():
            self.flush_timer.start()

    def cancel_background_loading(self):
        """Cancel any in-progress background search loading."""
        if self.search_thread is None:
            return
        self.load_generation += 1
        self.search_thread.join()
        self.search_thread = None

    def add_to_undo_stack(self, action_name: str,
                          should_ask_for_confirmation: bool):
        """Add the current state of the image tags to the undo stack."""
        tags = [image.tags.copy() for image in self.images]
        self.undo_stack.append(HistoryItem(action_name, tags,
                                           should_ask_for_confirmation))
        self.redo_stack.clear()
        self.update_undo_and_redo_actions_requested.emit()

    def write_image_tags_to_disk(self, image: Image):
        settings = get_settings()
        tags_subfolder = settings.value(
            'tags_subfolder',
            defaultValue=DEFAULT_SETTINGS['tags_subfolder'], type=str)
        
        # Keep the tag index up to date so that subsequent searches and
        # the All Tags list reflect the edited tags.
        self.tag_index[image.path] = image.tags.copy()
        
        if get_tags_storage_mode() == 'single_file':
            # The tag database is rewritten as a whole; batch frequent tag
            # edits with a debounce so that the file is not rewritten for
            # every single edit.
            self.database_dirty = True
            self.database_save_timer.start()
            return
        
        # Determine where to write the tags file
        if tags_subfolder:
            tags_dir = image.path.parent / tags_subfolder
            # Create the subfolder if it doesn't exist
            tags_dir.mkdir(parents=True, exist_ok=True)
            text_file_path = tags_dir / image.path.with_suffix('.txt').name
        else:
            text_file_path = image.path.with_suffix('.txt')
        
        try:
            text_file_path.write_text(
                self.tag_separator.join(image.tags), encoding='utf-8',
                errors='replace')
        except OSError:
            error_message_box = QMessageBox()
            error_message_box.setWindowTitle('Error')
            error_message_box.setIcon(QMessageBox.Icon.Critical)
            error_message_box.setText(f'Failed to save tags for {image.path}.')
            error_message_box.exec()

    @Slot()
    def migrate_txt_tags_to_database(self, delete_txt_files: bool = True):
        """Migrate the tags of the loaded directory from individual .txt
        caption files to the tag database."""
        if self.directory_path is None:
            QMessageBox.information(None, 'Migrate Tags',
                                    'Load a directory first.')
            return
        settings = get_settings()
        tags_subfolder = settings.value(
            'tags_subfolder',
            defaultValue=DEFAULT_SETTINGS['tags_subfolder'], type=str)
        self._build_tag_index_from_txt_files(self.directory_path,
                                              self.image_suffixes,
                                              tags_subfolder)
        try:
            migrated_count, deleted_txt_count = migrate_db(
                self.directory_path, self.tag_index, delete_txt_files)
        except OSError:
            QMessageBox.critical(None, 'Migrate Tags',
                                'Failed to write the tag database.')
            return
        self.database_dirty = False
        QMessageBox.information(
            None, 'Migrate Tags',
            f'Migrated tags for {migrated_count} '
            f'{pluralize("image", migrated_count)} to the tag database'
            + (f' and deleted {deleted_txt_count} '
               f'{pluralize("caption file", deleted_txt_count)}.'
               if delete_txt_files else '.'))

    @Slot()
    def save_tag_database(self):
        """Write the tag index to the tag database if there are unsaved
        changes."""
        if not self.database_dirty or self.directory_path is None:
            return
        try:
            write_tags_to_database(self.directory_path, self.tag_index)
            self.database_dirty = False
        except OSError:
            error_message_box = QMessageBox()
            error_message_box.setWindowTitle('Error')
            error_message_box.setIcon(QMessageBox.Icon.Critical)
            error_message_box.setText('Failed to save the tag database.')
            error_message_box.exec()

    def restore_history_tags(self, is_undo: bool):
        if is_undo:
            source_stack = self.undo_stack
            destination_stack = self.redo_stack
        else:
            # Redo.
            source_stack = self.redo_stack
            destination_stack = self.undo_stack
        if not source_stack:
            return
        history_item = source_stack[-1]
        if history_item.should_ask_for_confirmation:
            undo_or_redo_string = 'Undo' if is_undo else 'Redo'
            reply = get_confirmation_dialog_reply(
                title=undo_or_redo_string,
                question=f'{undo_or_redo_string} '
                         f'"{history_item.action_name}"?')
            if reply != QMessageBox.StandardButton.Yes:
                return
        source_stack.pop()
        tags = [image.tags for image in self.images]
        destination_stack.append(HistoryItem(
            history_item.action_name, tags,
            history_item.should_ask_for_confirmation))
        changed_image_indices = []
        for image_index, (image, history_image_tags) in enumerate(
                zip(self.images, history_item.tags)):
            if image.tags == history_image_tags:
                continue
            changed_image_indices.append(image_index)
            image.tags = history_image_tags
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))
        self.update_undo_and_redo_actions_requested.emit()

    @Slot()
    def undo(self):
        """Undo the last action."""
        self.restore_history_tags(is_undo=True)

    @Slot()
    def redo(self):
        """Redo the last undone action."""
        self.restore_history_tags(is_undo=False)

    def is_image_in_scope(self, scope: Scope | str, image_index: int,
                          image: Image) -> bool:
        if scope == Scope.ALL_IMAGES:
            return True
        if scope == Scope.FILTERED_IMAGES:
            return self.proxy_image_list_model.is_image_in_filtered_images(
                image)
        if scope == Scope.SELECTED_IMAGES:
            proxy_index = self.proxy_image_list_model.mapFromSource(
                self.index(image_index))
            return self.image_list_selection_model.isSelected(proxy_index)

    def get_text_match_count(self, text: str, scope: Scope | str,
                             whole_tags_only: bool, use_regex: bool) -> int:
        """Get the number of instances of a text in all captions."""
        match_count = 0
        for image_index, image in enumerate(self.images):
            if not self.is_image_in_scope(scope, image_index, image):
                continue
            if whole_tags_only:
                if use_regex:
                    match_count += len([
                        tag for tag in image.tags
                        if re.fullmatch(pattern=text, string=tag)
                    ])
                else:
                    match_count += image.tags.count(text)
            else:
                caption = self.tag_separator.join(image.tags)
                if use_regex:
                    match_count += len(re.findall(pattern=text,
                                                  string=caption))
                else:
                    match_count += caption.count(text)
        return match_count

    def find_and_replace(self, find_text: str, replace_text: str,
                         scope: Scope | str, use_regex: bool):
        """
        Find and replace arbitrary text in captions, within and across tag
        boundaries.
        """
        if not find_text:
            return
        self.add_to_undo_stack(action_name='Find and Replace',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if not self.is_image_in_scope(scope, image_index, image):
                continue
            caption = self.tag_separator.join(image.tags)
            if use_regex:
                if not re.search(pattern=find_text, string=caption):
                    continue
                caption = re.sub(pattern=find_text, repl=replace_text,
                                 string=caption)
            else:
                if find_text not in caption:
                    continue
                caption = caption.replace(find_text, replace_text)
            changed_image_indices.append(image_index)
            image.tags = caption.split(self.tag_separator)
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def sort_tags_alphabetically(self, do_not_reorder_first_tag: bool):
        """Sort the tags for each image in alphabetical order."""
        self.add_to_undo_stack(action_name='Sort Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            old_caption = self.tag_separator.join(image.tags)
            if do_not_reorder_first_tag:
                first_tag = image.tags[0]
                image.tags = [first_tag] + sorted(image.tags[1:])
            else:
                image.tags.sort()
            new_caption = self.tag_separator.join(image.tags)
            if new_caption != old_caption:
                changed_image_indices.append(image_index)
                self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def sort_tags_by_frequency(self, tag_counter: Counter,
                               do_not_reorder_first_tag: bool):
        """
        Sort the tags for each image by the total number of times a tag appears
        across all images.
        """
        self.add_to_undo_stack(action_name='Sort Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            old_caption = self.tag_separator.join(image.tags)
            if do_not_reorder_first_tag:
                first_tag = image.tags[0]
                image.tags = [first_tag] + sorted(
                    image.tags[1:], key=lambda tag: tag_counter[tag],
                    reverse=True)
            else:
                image.tags.sort(key=lambda tag: tag_counter[tag], reverse=True)
            new_caption = self.tag_separator.join(image.tags)
            if new_caption != old_caption:
                changed_image_indices.append(image_index)
                self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def reverse_tags_order(self, do_not_reorder_first_tag: bool):
        """Reverse the order of the tags for each image."""
        self.add_to_undo_stack(action_name='Reverse Order of Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            changed_image_indices.append(image_index)
            if do_not_reorder_first_tag:
                image.tags = [image.tags[0]] + list(reversed(image.tags[1:]))
            else:
                image.tags = list(reversed(image.tags))
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def shuffle_tags(self, do_not_reorder_first_tag: bool):
        """Shuffle the tags for each image randomly."""
        self.add_to_undo_stack(action_name='Shuffle Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            changed_image_indices.append(image_index)
            if do_not_reorder_first_tag:
                first_tag, *remaining_tags = image.tags
                random.shuffle(remaining_tags)
                image.tags = [first_tag] + remaining_tags
            else:
                random.shuffle(image.tags)
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def move_tags_to_front(self, tags_to_move: list[str]):
        """
        Move one or more tags to the front of the tags list for each image.
        """
        self.add_to_undo_stack(action_name='Move Tags to Front',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if not any(tag in image.tags for tag in tags_to_move):
                continue
            old_caption = self.tag_separator.join(image.tags)
            moved_tags = []
            for tag in tags_to_move:
                tag_count = image.tags.count(tag)
                moved_tags.extend([tag] * tag_count)
            unmoved_tags = [tag for tag in image.tags if tag not in moved_tags]
            image.tags = moved_tags + unmoved_tags
            new_caption = self.tag_separator.join(image.tags)
            if new_caption != old_caption:
                changed_image_indices.append(image_index)
                self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def remove_duplicate_tags(self) -> int:
        """
        Remove duplicate tags for each image. Return the number of removed
        tags.
        """
        self.add_to_undo_stack(action_name='Remove Duplicate Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        removed_tag_count = 0
        for image_index, image in enumerate(self.images):
            tag_count = len(image.tags)
            unique_tag_count = len(set(image.tags))
            if tag_count == unique_tag_count:
                continue
            changed_image_indices.append(image_index)
            removed_tag_count += tag_count - unique_tag_count
            # Use a dictionary instead of a set to preserve the order.
            image.tags = list(dict.fromkeys(image.tags))
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))
        return removed_tag_count

    def remove_empty_tags(self) -> int:
        """
        Remove empty tags (tags that are empty strings or only contain
        whitespace) for each image. Return the number of removed tags.
        """
        self.add_to_undo_stack(action_name='Remove Empty Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        removed_tag_count = 0
        for image_index, image in enumerate(self.images):
            old_tag_count = len(image.tags)
            image.tags = [tag for tag in image.tags if tag.strip()]
            new_tag_count = len(image.tags)
            if old_tag_count == new_tag_count:
                continue
            changed_image_indices.append(image_index)
            removed_tag_count += old_tag_count - new_tag_count
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))
        return removed_tag_count

    def update_image_tags(self, image_index: QModelIndex, tags: list[str]):
        image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
        if image.tags == tags:
            return
        image.tags = tags
        self.dataChanged.emit(image_index, image_index)
        self.write_image_tags_to_disk(image)

    @Slot(list, list)
    def add_tags(self, tags: list[str], image_indices: list[QModelIndex]):
        """Add one or more tags to one or more images."""
        if not image_indices:
            return
        action_name = f'Add {pluralize("Tag", len(tags))}'
        should_ask_for_confirmation = len(image_indices) > 1
        self.add_to_undo_stack(action_name, should_ask_for_confirmation)
        for image_index in image_indices:
            image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
            image.tags.extend(tags)
            self.write_image_tags_to_disk(image)
        min_image_index = min(image_indices, key=lambda index: index.row())
        max_image_index = max(image_indices, key=lambda index: index.row())
        self.dataChanged.emit(min_image_index, max_image_index)

    @Slot(list, str)
    def rename_tags(self, old_tags: list[str], new_tag: str,
                    scope: Scope | str = Scope.ALL_IMAGES,
                    use_regex: bool = False):
        self.add_to_undo_stack(
            action_name=f'Rename {pluralize("Tag", len(old_tags))}',
            should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if not self.is_image_in_scope(scope, image_index, image):
                continue
            if use_regex:
                pattern = old_tags[0]
                if not any(re.fullmatch(pattern=pattern, string=image_tag)
                           for image_tag in image.tags):
                    continue
                image.tags = [new_tag if re.fullmatch(pattern=pattern,
                                                      string=image_tag)
                              else image_tag for image_tag in image.tags]
            else:
                if not any(old_tag in image.tags for old_tag in old_tags):
                    continue
                image.tags = [new_tag if image_tag in old_tags else image_tag
                              for image_tag in image.tags]
            changed_image_indices.append(image_index)
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    @Slot(list)
    def delete_tags(self, tags: list[str],
                    scope: Scope | str = Scope.ALL_IMAGES,
                    use_regex: bool = False):
        self.add_to_undo_stack(
            action_name=f'Delete {pluralize("Tag", len(tags))}',
            should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if not self.is_image_in_scope(scope, image_index, image):
                continue
            if use_regex:
                pattern = tags[0]
                if not any(re.fullmatch(pattern=pattern, string=image_tag)
                           for image_tag in image.tags):
                    continue
                image.tags = [image_tag for image_tag in image.tags
                              if not re.fullmatch(pattern=pattern,
                                                  string=image_tag)]
            else:
                if not any(tag in image.tags for tag in tags):
                    continue
                image.tags = [image_tag for image_tag in image.tags
                              if image_tag not in tags]
            changed_image_indices.append(image_index)
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))
