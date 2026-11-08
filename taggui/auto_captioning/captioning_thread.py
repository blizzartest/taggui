from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
from queue import Empty, Queue
import threading
from time import perf_counter
import traceback

from PIL import UnidentifiedImageError
from PIL.Image import DecompressionBombError
from PySide6.QtCore import QModelIndex, QThread, Qt, Signal

from auto_captioning.auto_captioning_model import AutoCaptioningModel
from auto_captioning.models_list import get_model_class
from models.image_list_model import ImageListModel
from utils.enums import CaptionPosition
from utils.image import Image
from utils.settings import get_tag_separator

# Number of worker threads that load and preprocess images ahead of the
# inference thread, the maximum number of prefetched images being processed
# or waiting to be consumed, and the size of the queue connecting the
# prefetch workers to the inference thread.
CPU_COUNT = os.cpu_count() or 1
PREFETCH_WORKER_COUNT = min(4, CPU_COUNT)
PREFETCH_QUEUE_SIZE = max(2, PREFETCH_WORKER_COUNT)
MAX_IN_FLIGHT_PREFETCHES = (PREFETCH_WORKER_COUNT
                            + PREFETCH_QUEUE_SIZE)


def add_caption_to_tags(tags: list[str], caption: str,
                        caption_position: CaptionPosition) -> list[str]:
    if caption_position == CaptionPosition.DO_NOT_ADD or not caption:
        return tags
    tag_separator = get_tag_separator()
    new_tags = caption.split(tag_separator)
    # Make a copy of the tags so that the tags in the image list model are not
    # modified.
    tags = tags.copy()
    if caption_position == CaptionPosition.BEFORE_FIRST_TAG:
        tags[:0] = new_tags
    elif caption_position == CaptionPosition.AFTER_LAST_TAG:
        tags.extend(new_tags)
    elif caption_position == CaptionPosition.OVERWRITE_FIRST_TAG:
        if tags:
            tags[:1] = new_tags
        else:
            tags = new_tags
    elif caption_position == CaptionPosition.OVERWRITE_ALL_TAGS:
        tags = new_tags
    return tags


def format_duration(seconds: float) -> str:
    seconds_per_minute = 60
    seconds_per_hour = 60 * seconds_per_minute
    seconds_per_day = 24 * seconds_per_hour
    if seconds < seconds_per_minute:
        return f'{seconds:.1f} seconds'
    if seconds < seconds_per_hour:
        minutes = seconds / seconds_per_minute
        return f'{minutes:.1f} minutes'
    if seconds < seconds_per_day:
        hours = seconds / seconds_per_hour
        return f'{hours:.1f} hours'
    days = seconds / seconds_per_day
    return f'{days:.1f} days'


class CaptioningThread(QThread):
    text_outputted = Signal(str)
    clear_console_text_edit_requested = Signal()
    # The image index, the caption, and the tags with the caption added. The
    # third parameter must be declared as `list` instead of `list[str]` for it
    # to work.
    caption_generated = Signal(QModelIndex, str, list)
    progress_bar_update_requested = Signal(int)

    def __init__(self, parent, image_list_model: ImageListModel,
                 selected_image_indices: list[QModelIndex],
                 caption_settings: dict, tag_separator: str,
                 models_directory_path: Path | None):
        super().__init__(parent)
        self.image_list_model = image_list_model
        self.selected_image_indices = selected_image_indices
        self.caption_settings = caption_settings
        self.tag_separator = tag_separator
        self.models_directory_path = models_directory_path
        self.is_error = False
        self.is_canceled = False
        self.is_prefetch_aborted = False

    def run_captioning(self):
        model_id = self.caption_settings['model_id']
        model_class = get_model_class(model_id)
        model: AutoCaptioningModel = model_class(
            captioning_thread_=self, caption_settings=self.caption_settings)
        error_message = model.get_error_message()
        if error_message:
            self.is_error = True
            self.clear_console_text_edit_requested.emit()
            print(error_message)
            return
        model.load_processor_and_model()
        model.monkey_patch_after_loading()
        if self.is_canceled:
            print('Canceled captioning.')
            return
        self.clear_console_text_edit_requested.emit()
        selected_image_count = len(self.selected_image_indices)
        are_multiple_images_selected = selected_image_count > 1
        captioning_start_datetime = datetime.now()
        captioning_message = model.get_captioning_message(
            are_multiple_images_selected, captioning_start_datetime)
        print(captioning_message)
        caption_position = self.caption_settings['caption_position']
        batch_size = max(1, getattr(model, 'batch_size', 1))
        supports_prefetch = getattr(model, 'supports_prefetch', False)
        if batch_size > 1 or supports_prefetch:
            captioned_count, skipped_count = self.run_parallel_captioning(
                model, caption_position, batch_size)
        else:
            captioned_count, skipped_count = self.run_sequential_captioning(
                model, caption_position)
        if are_multiple_images_selected:
            captioning_end_datetime = datetime.now()
            total_captioning_duration = ((captioning_end_datetime
                                          - captioning_start_datetime)
                                         .total_seconds())
            average_captioning_duration = (total_captioning_duration /
                                           max(captioned_count, 1))
            summary = (f'Finished captioning {captioned_count} of '
                       f'{selected_image_count} images in '
                       f'{format_duration(total_captioning_duration)} '
                       f'({average_captioning_duration:.1f} s/image) at '
                       f'{captioning_end_datetime.strftime("%Y-%m-%d %H:%M:%S")}.')
            if skipped_count:
                summary += f'\nSkipped {skipped_count} image(s) due to errors.'
            print(summary)

    def get_model_inputs_result(self, model,
                                image_index: QModelIndex) -> tuple:
        """Get the image, image prompt, model inputs, and loading error (if
        any) for an image. Safe to call from a worker thread."""
        image: Image = self.image_list_model.data(image_index,
                                                  Qt.ItemDataRole.UserRole)
        image_prompt = model.get_image_prompt(image)
        try:
            model_inputs = model.get_model_inputs(image_prompt, image)
            error = None
        except Exception as exception:
            model_inputs = None
            error = exception
        return image, image_index, image_prompt, model_inputs, error

    @staticmethod
    def report_image_error(image: Image, error: Exception) -> None:
        if isinstance(error, UnidentifiedImageError):
            print(f'Skipping {image.path.name} because its file format is '
                  'not supported or it is a corrupted image.')
        elif isinstance(error, DecompressionBombError):
            print(f'Skipping {image.path.name} because its pixel size '
                  'exceeds the decompression bomb limit.')
        elif isinstance(error, FileNotFoundError):
            print(f'Skipping {image.path.name} because it could not be '
                  'found.')
        else:
            error_traceback = ''.join(traceback.format_exception(error))
            print(f'Skipping {image.path.name} because an error occurred '
                  f'while processing it:\n{error_traceback}')

    def emit_caption(self, image: Image, image_index: QModelIndex,
                     caption: str, console_output_caption: str | None,
                     caption_position: CaptionPosition,
                     processed_index: int,
                     are_multiple_images_selected: bool,
                     duration: float) -> None:
        tags = add_caption_to_tags(image.tags, caption, caption_position)
        self.caption_generated.emit(image_index, caption, tags)
        if are_multiple_images_selected:
            self.progress_bar_update_requested.emit(processed_index + 1)
        if console_output_caption is None:
            console_output_caption = caption
        print(f'{image.path.name} ({duration:.1f} s):\n'
              f'{console_output_caption}')

    def process_image(self, model, image: Image, image_index: QModelIndex,
                      image_prompt: str, model_inputs, error: Exception | None,
                      caption_position: CaptionPosition, processed_index: int,
                      are_multiple_images_selected: bool
                      ) -> tuple[bool, bool]:
        """Generate a caption for a single image. Returns whether the image
        was captioned and whether it was skipped."""
        start_time = perf_counter()
        try:
            if error is not None:
                raise error
            caption, console_output_caption = model.generate_caption(
                model_inputs, image_prompt)
        except Exception as exception:
            self.report_image_error(image, exception)
            return False, True
        if processed_index == 0 and not are_multiple_images_selected:
            self.clear_console_text_edit_requested.emit()
        self.emit_caption(image, image_index, caption,
                          console_output_caption, caption_position,
                          processed_index, are_multiple_images_selected,
                          perf_counter() - start_time)
        return True, False

    def process_image_batch(self, model, batch_entries: list[tuple],
                            caption_position: CaptionPosition,
                            processed_start_index: int,
                            are_multiple_images_selected: bool
                            ) -> tuple[int, int]:
        """Generate captions for a batch of images in one inference run.
        Returns the number of captioned and skipped images."""
        captioned_count = 0
        skipped_count = 0
        valid_entries = []
        for entry in batch_entries:
            if entry[4] is None:
                valid_entries.append(entry)
            else:
                self.report_image_error(entry[0], entry[4])
                skipped_count += 1
        if not valid_entries:
            return captioned_count, skipped_count
        start_time = perf_counter()
        captions = model.generate_captions_batch(
            [entry[3] for entry in valid_entries])
        duration = perf_counter() - start_time
        for offset, (entry, caption_tuple) in enumerate(
                zip(valid_entries, captions)):
            image, image_index, _, _, _ = entry
            caption, console_output_caption = caption_tuple
            self.emit_caption(image, image_index, caption,
                              console_output_caption, caption_position,
                              processed_start_index + offset,
                              are_multiple_images_selected, duration)
            captioned_count += 1
        return captioned_count, skipped_count

    def run_sequential_captioning(self, model, caption_position
                                  ) -> tuple[int, int]:
        """Caption images one at a time. Returns the number of captioned
        and skipped images."""
        are_multiple_images_selected = len(self.selected_image_indices) > 1
        captioned_count = 0
        skipped_count = 0
        for i, image_index in enumerate(self.selected_image_indices):
            if self.is_canceled:
                print('Canceled captioning.')
                return captioned_count, skipped_count
            (image, image_index, image_prompt, model_inputs,
             error) = self.get_model_inputs_result(model, image_index)
            captioned, skipped = self.process_image(
                model, image, image_index, image_prompt, model_inputs, error,
                caption_position, i, are_multiple_images_selected)
            captioned_count += captioned
            skipped_count += skipped
        return captioned_count, skipped_count

    def put_queue_entry(self, queue: Queue, entry) -> bool:
        """Put an entry on the prefetch queue, aborting if captioning is
        canceled while the queue is full."""
        while True:
            try:
                queue.put(entry, timeout=0.1)
                return True
            except Empty:
                if self.is_canceled or self.is_prefetch_aborted:
                    return False

    def fill_prefetch_queue(self, model, queue: Queue) -> None:
        """Load and preprocess images in worker threads so the CPU work
        overlaps with GPU inference. Runs in a producer thread."""
        image_indices = self.selected_image_indices
        next_submission_index = 0
        futures = deque()
        with ThreadPoolExecutor(max_workers=PREFETCH_WORKER_COUNT) as pool:
            while (next_submission_index < len(image_indices)
                   and len(futures) < MAX_IN_FLIGHT_PREFETCHES):
                futures.append(pool.submit(self.get_model_inputs_result,
                                            model,
                                            image_indices[
                                                next_submission_index]))
                next_submission_index += 1
            while futures:
                entry = futures.popleft().result()
                if self.is_canceled or self.is_prefetch_aborted:
                    return
                if not self.put_queue_entry(queue, entry):
                    return
                if next_submission_index < len(image_indices):
                    futures.append(
                        pool.submit(self.get_model_inputs_result, model,
                                    image_indices[next_submission_index]))
                    next_submission_index += 1
        self.put_queue_entry(queue, None)

    def run_parallel_captioning(self, model, caption_position: CaptionPosition,
                                batch_size: int) -> tuple[int, int]:
        """Caption images using a prefetch pipeline that overlaps image
        loading and preprocessing (CPU) with inference (GPU), optionally
        running inference on batches of images. Returns the number of
        captioned and skipped images."""
        can_batch = (batch_size > 1
                     and hasattr(model, 'generate_captions_batch'))
        are_multiple_images_selected = len(self.selected_image_indices) > 1
        captioned_count = 0
        skipped_count = 0
        processed_count = 0
        batch_entries = []
        queue = Queue(maxsize=PREFETCH_QUEUE_SIZE)
        producer = threading.Thread(target=self.fill_prefetch_queue,
                                     args=(model, queue))
        producer.start()
        try:
            while True:
                # Poll with a timeout so captioning can be canceled even
                # when the producer has stopped supplying entries (which
                # it does as soon as it notices the cancellation).
                try:
                    entry = queue.get(timeout=0.1)
                except Empty:
                    if self.is_canceled:
                        print('Canceled captioning.')
                        return captioned_count, skipped_count
                    continue
                if entry is None:
                    break
                if self.is_canceled:
                    print('Canceled captioning.')
                    return captioned_count, skipped_count
                if can_batch:
                    batch_entries.append(entry)
                    if len(batch_entries) >= batch_size:
                        captioned, skipped = self.process_image_batch(
                            model, batch_entries, caption_position,
                            processed_count, are_multiple_images_selected)
                        captioned_count += captioned
                        skipped_count += skipped
                        processed_count += len(batch_entries)
                        batch_entries = []
                else:
                    image, image_index, image_prompt, model_inputs, error \
                        = entry
                    captioned, skipped = self.process_image(
                        model, image, image_index, image_prompt, model_inputs,
                        error, caption_position, processed_count,
                        are_multiple_images_selected)
                    captioned_count += captioned
                    skipped_count += skipped
                    processed_count += 1
        finally:
            # If the consumer exits early (canceled or an error), unblock
            # the producer so it can shut down.
            self.is_prefetch_aborted = True
            producer.join()
        if can_batch and batch_entries and not self.is_canceled:
            captioned, skipped = self.process_image_batch(
                model, batch_entries, caption_position, processed_count,
                are_multiple_images_selected)
            captioned_count += captioned
            skipped_count += skipped
        return captioned_count, skipped_count

    def run(self):
        try:
            self.run_captioning()
        except Exception as exception:
            self.is_error = True
            # Show the error message in the console text edit.
            raise exception

    def write(self, text: str):
        self.text_outputted.emit(text)
