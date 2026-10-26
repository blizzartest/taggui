from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import (QMetaObject, QObject, Qt, QThread, QTimer,
                           Signal, Slot)
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QSizePolicy, QWidget)

from utils.video import VideoFrameReader

PLAYBACK_SPEEDS = (0.5, 1.0, 2.0, 4.0)


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{seconds:02d}'
    return f'{minutes}:{seconds:02d}'


class VideoDecodeWorker(QObject):
    """Decodes video frames off the GUI thread.

    The worker owns a VideoFrameReader. Playback is paced with a timer
    that ticks at the video's frame rate; the timer lives in the worker
    thread, so all playback state is only touched from that thread.
    Control requests from the GUI thread arrive as queued slot calls.
    """

    frame_ready = Signal(QImage, int, int, float)
    playing_changed = Signal(bool)
    error_occurred = Signal(str)

    def __init__(self, video_path: Path, reader: VideoFrameReader | None = None):
        super().__init__()
        self.video_path = video_path
        self._reader = reader
        self._playing = False
        self._speed = 1.0
        self._current_index = -1
        self._reader = None
        self._timer = None

    @Slot()
    def start(self):
        if self._reader is None:
            try:
                self._reader = VideoFrameReader(self.video_path)
            except Exception as exception:
                self.error_occurred.emit(str(exception))
                return
        if not self._reader.frame_count:
            self.error_occurred.emit('Could not read video metadata')
            self.close()
            return
        self._timer = QTimer()
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._next_frame)
        self.read_frame(0)

    @Slot(int)
    def read_frame(self, index: int):
        if self._reader is None:
            return
        frame = self._reader.read_frame(index)
        if frame is None:
            self.pause()
            return
        self._current_index = index
        image = QImage(frame.tobytes(), frame.width, frame.height,
                       frame.width * 3, QImage.Format.Format_RGB888)
        self.frame_ready.emit(image.copy(), index,
                              self._reader.frame_count, self._reader.fps)

    @Slot(bool)
    def set_playing(self, playing: bool):
        if playing:
            self._start_timer()
        else:
            self.pause()

    def pause(self):
        if self._playing:
            self._playing = False
            self.playing_changed.emit(False)
        if self._timer is not None:
            self._timer.stop()

    @Slot(float)
    def set_speed(self, speed: float):
        self._speed = speed
        if self._playing:
            self._start_timer()

    @Slot()
    def stop(self):
        self.pause()
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def _start_timer(self):
        if self._timer is None or self._reader is None:
            return
        if not self._playing:
            self._playing = True
            self.playing_changed.emit(True)
        fps = self._reader.fps or 24
        interval = max(10, int(1000 / (fps * self._speed)))
        self._timer.start(interval)

    def _next_frame(self):
        if self._reader is None:
            return
        index = self._current_index + 1
        if index >= self._reader.frame_count:
            self.pause()
            return
        self.read_frame(index)


class VideoPlayerBar(QWidget):
    """Transport controls shown below a video in the image viewer."""

    play_requested = Signal(bool)
    position_changed = Signal(int)
    speed_changed = Signal(float)

    def __init__(self):
        super().__init__()
        self._suppress_slider_signals = False

        self.play_button = QPushButton('Play')
        self.play_button.setFixedWidth(64)
        self.play_button.clicked.connect(self._toggle_play)
        self.play_requested.connect(self.set_playing)

        self.step_back_button = QPushButton('⏮')
        self.step_back_button.setFixedWidth(36)
        self.step_back_button.clicked.connect(
            lambda: self.position_changed.emit(max(0, self.slider.value() - 1)))

        self.step_forward_button = QPushButton('⏭')
        self.step_forward_button.setFixedWidth(36)
        self.step_forward_button.clicked.connect(
            lambda: self.position_changed.emit(self.slider.value() + 1))

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
        self.slider.sliderPressed.connect(
            lambda: self._set_suppress_slider_signals(True))
        self.slider.sliderReleased.connect(
            lambda: self._set_suppress_slider_signals(False))
        self.slider.valueChanged.connect(self._slider_value_changed)

        self.time_label = QLabel('0:00 / 0:00')

        self.speed_combo_box = QComboBox()
        for speed in PLAYBACK_SPEEDS:
            self.speed_combo_box.addItem(f'{speed:g}x', speed)
        self.speed_combo_box.setCurrentIndex(1)
        self.speed_combo_box.currentIndexChanged.connect(
            lambda: self.speed_changed.emit(
                self.speed_combo_box.currentData()))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.step_back_button, self.play_button,
                       self.step_forward_button):
            layout.addWidget(widget)
        layout.addWidget(self.slider)
        layout.addWidget(self.time_label)
        layout.addWidget(self.speed_combo_box)

    def set_playing(self, playing: bool):
        self.play_button.setText('Pause' if playing else 'Play')

    def set_range(self, frame_count: int):
        self._suppress_slider_signals = True
        self.slider.setRange(0, max(0, frame_count - 1))
        self._suppress_slider_signals = False

    def set_position(self, frame_index: int, fps: float):
        self._suppress_slider_signals = True
        self.slider.setValue(frame_index)
        self._suppress_slider_signals = False
        position = frame_index / fps if fps else 0
        duration = self.slider.maximum() / fps if fps else 0
        self.time_label.setText(
            f'{format_time(position)} / {format_time(duration)}')

    def _toggle_play(self):
        self.play_requested.emit(self.play_button.text() == 'Play')

    def _slider_value_changed(self, value: int):
        if not self._suppress_slider_signals:
            self.position_changed.emit(value)

    def _set_suppress_slider_signals(self, suppress: bool):
        self._suppress_slider_signals = suppress
        if not suppress:
            self.position_changed.emit(self.slider.value())


class VideoPlayer(QWidget):
    """Owns the decode worker thread and the control bar for a video.

    Frames are displayed by the image viewer's image label; decoded
    frames are forwarded through the `frame_changed` signal.
    """

    frame_changed = Signal(QImage)
    error_occurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.control_bar = VideoPlayerBar()
        self.control_bar.hide()
        self._thread = None
        self._worker = None

    def load_video(self, video_path: Path):
        self.close_video()
        # Open the reader and set up the control bar on the GUI thread:
        # this also opens the ffmpeg decoder for the first time here,
        # safer than doing package discovery inside the worker thread
        # (a known source of trouble in frozen builds). Reading only the
        # metadata is fast, so this does not block the UI noticeably.
        try:
            reader = VideoFrameReader(video_path)
        except Exception as exception:
            self._on_error(str(exception))
            return
        if not reader.frame_count:
            reader.close()
            self._on_error('Could not read video metadata')
            return
        self.control_bar.set_range(reader.frame_count)
        self.control_bar.set_position(0, reader.fps)
        self.control_bar.show()
        thread = QThread()
        worker = VideoDecodeWorker(video_path, reader)
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        worker.frame_ready.connect(self._on_frame_ready)
        worker.playing_changed.connect(self.control_bar.set_playing)
        worker.error_occurred.connect(self._on_error)
        self.control_bar.play_requested.connect(worker.set_playing)
        self.control_bar.position_changed.connect(worker.read_frame)
        self.control_bar.speed_changed.connect(worker.set_speed)
        self._thread = thread
        self._worker = worker
        thread.start()

    def close_video(self):
        self.control_bar.hide()
        self.control_bar.set_playing(False)
        thread, worker = self._thread, self._worker
        self._thread = self._worker = None
        if thread is None:
            return
        if worker is not None:
            # Stop the worker in its own thread so that the reader is
            # closed where it is used and no decode is still running
            # when the thread quits.
            if thread.isRunning():
                QMetaObject.invokeMethod(
                    worker, 'stop', Qt.ConnectionType.BlockingQueuedConnection)
            else:
                worker.stop()
        thread.quit()
        thread.wait(2000)

    def _on_frame_ready(self, image: QImage, frame_index: int,
                        frame_count: int, fps: float):
        if self._worker is None:
            return
        self.control_bar.set_range(frame_count)
        self.control_bar.set_position(frame_index, fps)
        self.control_bar.show()
        self.frame_changed.emit(image)

    def _on_error(self, message: str):
        self.close_video()
        if sys.stderr is not None:
            print(f'Failed to play video: {message}', file=sys.stderr)
        self.error_occurred.emit(message)
