import sys
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QSlider, QVBoxLayout, QWidget)

from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.video import VideoReader, get_video_thumbnail

# Width to which decoded frames are scaled before display. Scaling during
# decoding keeps the frame buffer small and the display smooth; the pixmap
# is scaled to the actual widget size afterwards.
FRAME_DISPLAY_WIDTH = 1280


def pil_to_pixmap(pil_image, device_pixel_ratio: float) -> QPixmap:
    """Convert an RGB PIL image to a QPixmap.

    QImage does not own the PIL buffer, so the data is copied. This matches
    the conversion used for video thumbnails in the image list model.
    """
    pixmap = QPixmap.fromImage(
        QImage(pil_image.tobytes(), pil_image.width, pil_image.height,
               pil_image.width * 3, QImage.Format.Format_RGB888).copy())
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    return pixmap


def format_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    seconds = int(seconds % 60)
    return f'{minutes}:{seconds:02d}'


class FrameDecodeThread(QThread):
    """Decodes a single frame off the GUI thread while scrubbing."""

    frame_decoded = Signal(object)

    def __init__(self, parent: QObject, reader: VideoReader, frame_index: int):
        super().__init__(parent)
        self.reader = reader
        self.frame_index = frame_index

    def run(self):
        try:
            image = self.reader.get_frame(self.frame_index)
        except Exception as exception:
            print(f'Failed to decode frame {self.frame_index} of '
                  f'{self.reader.video_path}: {exception}', file=sys.stderr)
            image = None
        self.frame_decoded.emit(image)


class VideoDisplayLabel(QLabel):
    """Label that shows video frames and toggles play/pause on click."""

    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(QSize(1, 1))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class VideoPlayerWidget(QWidget):
    """Silent video player with play/pause and a seek slider.

    Frames are decoded in the background by a `VideoReader` and shown at
    the video's frame rate. Playback is silent; the player exists so that
    videos can be skimmed while tagging.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.video_path: Path | None = None
        self.reader: VideoReader | None = None
        self.is_playing = False
        self.current_frame_index = 0
        # Incremented whenever the shown frame becomes stale (seek or new
        # video), so that late decode results can be discarded.
        self._display_generation = 0
        self._scrub_thread: FrameDecodeThread | None = None
        self._slider_was_playing = False
        self._unscaled_pixmap: QPixmap | None = None

        self.display_label = VideoDisplayLabel()
        self.display_label.clicked.connect(self.toggle_play_pause)

        self.play_pause_button = QPushButton('Play')
        self.play_pause_button.setFixedWidth(70)
        self.play_pause_button.clicked.connect(self.toggle_play_pause)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderPressed.connect(self.handle_slider_pressed)
        self.position_slider.sliderReleased.connect(
            self.handle_slider_released)
        self.position_slider.valueChanged.connect(self.handle_slider_moved)

        self.time_label = QLabel('0:00 / 0:00')

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.play_pause_button)
        controls_layout.addWidget(self.position_slider, stretch=1)
        controls_layout.addWidget(self.time_label)

        layout = QVBoxLayout(self)
        layout.addWidget(self.display_label, stretch=1)
        layout.addLayout(controls_layout)

        self.frame_timer = QTimer(self)
        self.frame_timer.timeout.connect(self.show_next_frame)

    def load_video(self, video_path: Path) -> bool:
        """Load a video and show its poster frame, paused.

        Returns False if the video cannot be decoded, in which case the
        caller should fall back to showing a static frame.
        """
        self.unload_video()
        self.video_path = video_path
        try:
            self.reader = VideoReader(video_path, FRAME_DISPLAY_WIDTH)
        except Exception as exception:
            print(f'Failed to open video {video_path}: {exception}',
                  file=sys.stderr)
            self.reader = None
            return False
        if not self.reader.fps or not self.reader.frame_count:
            self.reader.close()
            self.reader = None
            return False
        self.current_frame_index = 0
        self.position_slider.setRange(0, self.reader.frame_count - 1)
        self._update_time_label()
        # Show the cached poster frame immediately so that there is no
        # decoding delay before the first frame.
        poster = get_video_thumbnail(video_path, FRAME_DISPLAY_WIDTH)
        if poster is not None:
            self.show_pixmap(pil_to_pixmap(poster.convert('RGB'),
                                           self.devicePixelRatio()))
        settings = get_settings()
        autoplay = settings.value(
            'video_autoplay', defaultValue=DEFAULT_SETTINGS['video_autoplay'],
            type=bool)
        if autoplay:
            self.play()
        return True

    def unload_video(self):
        """Stop playback and release the video."""
        self.pause()
        self._display_generation += 1
        if self._scrub_thread is not None:
            self._scrub_thread.frame_decoded.disconnect()
            self._scrub_thread.wait()
            self._scrub_thread = None
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.video_path = None
        self.current_frame_index = 0
        self.position_slider.setRange(0, 0)
        self.time_label.setText('0:00 / 0:00')
        self._unscaled_pixmap = None
        self.display_label.clear()

    def play(self):
        if self.reader is None or self.is_playing:
            return
        if self.current_frame_index >= self.reader.frame_count - 1:
            # Restart from the beginning when the video has ended.
            self.seek_to_frame(0, resume_playing=False)
        # A scrub decode must not run concurrently with the playback
        # decoder; both use the same underlying video reader.
        if self._scrub_thread is not None:
            self._scrub_thread.frame_decoded.disconnect()
            self._scrub_thread.wait()
            self._scrub_thread = None
        self.is_playing = True
        self.play_pause_button.setText('Pause')
        self.reader.start()
        interval_ms = max(1, round(1000 / self.reader.fps))
        self.frame_timer.start(interval_ms)

    def pause(self):
        if not self.is_playing:
            return
        self.is_playing = False
        self.frame_timer.stop()
        self.play_pause_button.setText('Play')
        if self.reader is not None:
            self.reader.stop()

    @Slot()
    def toggle_play_pause(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def seek_to_frame(self, frame_index: int, resume_playing: bool = True):
        """Seek to a frame; playback resumes there if it was playing."""
        if self.reader is None:
            return
        frame_index = max(0, min(frame_index, self.reader.frame_count - 1))
        was_playing = self.is_playing
        self.pause()
        self._display_generation += 1
        self.current_frame_index = frame_index
        self.position_slider.blockSignals(True)
        self.position_slider.setValue(frame_index)
        self.position_slider.blockSignals(False)
        self._update_time_label()
        self.reader.seek(frame_index)
        if resume_playing and was_playing:
            self.play()

    @Slot()
    def show_next_frame(self):
        if self.reader is None or not self.is_playing:
            return
        if not self.isVisible():
            # Do not decode while the player is hidden (e.g. the window is
            # minimized); the bounded frame buffer stops the decoder.
            return
        result = self.reader.poll_frame()
        if result is None:
            # End of the video (or a decoding error): stay on the last frame.
            self.pause()
            return
        if result is False:
            # The decoder has not caught up yet; skip this update.
            return
        frame_index, image = result
        self.current_frame_index = frame_index
        self.position_slider.blockSignals(True)
        self.position_slider.setValue(frame_index)
        self.position_slider.blockSignals(False)
        self._update_time_label()
        self.show_pixmap(pil_to_pixmap(image, self.devicePixelRatio()))

    def show_pixmap(self, pixmap: QPixmap):
        ratio = pixmap.devicePixelRatio()
        self._unscaled_pixmap = pixmap
        self.display_label.setPixmap(
            pixmap.scaled(self.display_label.size() * ratio,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event: QResizeEvent):
        """Rescale the current frame when the widget is resized."""
        pixmap = getattr(self, '_unscaled_pixmap', None)
        if pixmap is not None and not pixmap.isNull():
            self.show_pixmap(pixmap)
        super().resizeEvent(event)

    def handle_slider_pressed(self):
        self._slider_was_playing = self.is_playing
        self.pause()

    def handle_slider_released(self):
        self.seek_to_frame(self.position_slider.value(),
                           resume_playing=self._slider_was_playing)

    def handle_slider_moved(self, value: int):
        """Scrub: update the position and, while paused, decode the frame."""
        if self.reader is None:
            return
        self.current_frame_index = value
        self._update_time_label()
        if self.is_playing:
            return
        # Cancel a pending scrub decode; only the latest position matters.
        if self._scrub_thread is not None:
            self._scrub_thread.frame_decoded.disconnect()
            self._scrub_thread.wait()
        generation = self._display_generation
        self._scrub_thread = FrameDecodeThread(self, self.reader, value)
        self._scrub_thread.frame_decoded.connect(
            lambda image, generation=generation: self.handle_scrub_frame(
                image, generation))
        self._scrub_thread.start()

    def handle_scrub_frame(self, image, generation: int):
        if generation != self._display_generation or image is None:
            return
        thread = self.sender()
        if thread is not None:
            thread.wait()
        self._scrub_thread = None
        self.show_pixmap(pil_to_pixmap(image, self.devicePixelRatio()))

    def _update_time_label(self):
        if self.reader is None or not self.reader.fps:
            return
        current_seconds = self.current_frame_index / self.reader.fps
        total_seconds = (self.reader.frame_count - 1) / self.reader.fps
        self.time_label.setText(f'{format_timestamp(current_seconds)} / '
                                f'{format_timestamp(total_seconds)}')
