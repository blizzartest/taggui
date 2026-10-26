import sys
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent, QImage, QKeyEvent, QPixmap
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

from utils.video import transcode_video_for_playback

# Interval in seconds at which playback advances by one frame.
DEFAULT_PLAYBACK_FPS = 25.0


def format_timestamp(seconds: float) -> str:
    """Format a time in seconds as m:ss."""
    total_seconds = max(int(seconds), 0)
    minutes, seconds = divmod(total_seconds, 60)
    return f'{minutes}:{seconds:02d}'


class TranscodeThread(QThread):
    """Transcodes a video for playback in the background."""

    progress_updated = Signal(float)
    finished_successfully = Signal(Path)

    def __init__(self, parent: QObject, video_path: Path):
        super().__init__(parent)
        self.video_path = video_path
        self.is_canceled = False

    def run(self):
        playback_path = transcode_video_for_playback(
            self.video_path, progress_callback=self.progress_updated.emit,
            cancel_check=lambda: self.is_canceled)
        if playback_path is not None:
            self.finished_successfully.emit(playback_path)

    def cancel(self):
        self.is_canceled = True


class VideoDecodeThread(QThread):
    """Decodes frames of a (transcoded) video in the background.

    Frames are read sequentially, which is much faster than random-access
    reads, and emitted as QImages at the video's frame rate. Playback can be
    paused, resumed and seeked to any frame; seeking repositions the decoder
    and continues reading sequentially from there.
    """

    # The frame as a QImage, and its index in the video.
    frame_decoded = Signal(QImage, int)
    playback_finished = Signal()
    error_occurred = Signal(str)

    def __init__(self, parent: QObject, video_path: Path, fps: float):
        super().__init__(parent)
        self.video_path = video_path
        self.fps = fps
        self.is_playing = False
        self.is_stopped = False
        self.target_frame_index = 0
        self._reader = None
        # The frame index that the reader will decode next. The reader is
        # always positioned so that the next `get_next_data()` returns this
        # frame; the index of the last decoded frame is one less.
        self._next_frame_index = 0
        self._last_emitted_frame_index = -1

    def run(self):
        try:
            import imageio
            self._reader = imageio.get_reader(str(self.video_path))
        except Exception as exception:
            self.error_occurred.emit(
                f'Failed to open the video:\n{exception}')
            return
        frame_interval = 1.0 / self.fps
        try:
            while not self.is_stopped:
                if not self.is_playing:
                    QThread.msleep(20)
                    continue
                # A seek request is applied before decoding the next frame.
                # When resuming after a pause, no seek is needed because the
                # reader is still positioned at the next frame.
                self._seek_if_needed()
                frame_start_time = monotonic()
                try:
                    frame = self._reader.get_next_data()
                except (IndexError, StopIteration):
                    # The end of the video was reached.
                    self.is_playing = False
                    self.playback_finished.emit()
                    continue
                except Exception as exception:
                    self.error_occurred.emit(
                        f'Failed to decode a frame:\n{exception}')
                    return
                frame_index = self._next_frame_index
                self._next_frame_index += 1
                self._last_emitted_frame_index = frame_index
                # Keep the target in sync with normal playback so that the
                # seek check does not trigger on the next iteration.
                self.target_frame_index = self._next_frame_index
                image = QImage(frame.data, frame.shape[1], frame.shape[0],
                               frame.strides[0],
                               QImage.Format.Format_RGB888).copy()
                self.frame_decoded.emit(image, frame_index)
                # Pace the decoding to the video's frame rate.
                elapsed = monotonic() - frame_start_time
                remaining = frame_interval - elapsed
                if remaining > 0:
                    QThread.msleep(int(remaining * 1000))
        finally:
            self._reader.close()
            self._reader = None

    def _seek_if_needed(self):
        """Reposition the decoder if a seek was requested."""
        if self.target_frame_index == self._next_frame_index:
            return
        if self.target_frame_index > self._next_frame_index:
            # A forward seek is applied by decoding and discarding the frames
            # in between, which is fast because every frame of the
            # transcoded playback copy is a keyframe.
            while self._next_frame_index < self.target_frame_index:
                self._reader.get_next_data()
                self._next_frame_index += 1
            self._last_emitted_frame_index = self._next_frame_index - 1
            return
        # Every frame of the transcoded playback copy is a keyframe, so
        # seeking backward is fast and the reader continues sequentially
        # from the target frame. The target is cleared afterwards so that
        # normal playback continues sequentially instead of seeking back.
        self._reader.set_image_index(self.target_frame_index)
        self._next_frame_index = self.target_frame_index
        self._last_emitted_frame_index = self.target_frame_index - 1
        self.target_frame_index = self._next_frame_index

    def play(self):
        self.is_playing = True
        # Continue from the frame that was last decoded, because the GUI may
        # have seeked while the playback was paused.
        self.target_frame_index = self._next_frame_index

    def pause(self):
        self.is_playing = False

    def seek(self, frame_index: int):
        # The seek takes effect on the next decoded frame, or immediately
        # when the playback is paused (the next `play()` applies it).
        self.target_frame_index = max(frame_index, 0)

    def stop(self):
        self.is_stopped = True


class VideoPlayerDialog(QDialog):
    """A dialog for skimming through a video without audio.

    The video is first transcoded into a seek-friendly playback copy (see
    `transcode_video_for_playback`), then played back with a seek bar,
    play/pause and keyboard controls (Space, Left/Right, Home/End).
    """

    def __init__(self, parent: QWidget, video_path: Path):
        super().__init__(parent)
        self.video_path = video_path
        self.setWindowTitle(video_path.name)
        self.resize(800, 600)
        layout = QVBoxLayout(self)
        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(320, 180)
        self.video_label.setStyleSheet('background-color: black;')
        layout.addWidget(self.video_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        controls_layout = QHBoxLayout()
        self.play_pause_button = QPushButton('Play')
        self.play_pause_button.setEnabled(False)
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        controls_layout.addWidget(self.play_pause_button)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderMoved.connect(self.handle_slider_moved)
        self.seek_slider.sliderReleased.connect(self.handle_slider_released)
        controls_layout.addWidget(self.seek_slider)
        self.timestamp_label = QLabel('0:00 / 0:00')
        controls_layout.addWidget(self.timestamp_label)
        layout.addLayout(controls_layout)
        self.transcode_thread = None
        self.decode_thread = None
        self.fps = DEFAULT_PLAYBACK_FPS
        self.duration = 0.0
        self.total_frames = 0
        self.current_frame_index = 0
        self.is_seeking = False
        self.is_closing = False
        self.start_transcoding()

    def start_transcoding(self):
        """Start transcoding the video into a seek-friendly playback copy."""
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat('Transcoding video... %p%')
        self.transcode_thread = TranscodeThread(self, self.video_path)
        self.transcode_thread.progress_updated.connect(
            self.handle_transcode_progress)
        self.transcode_thread.finished_successfully.connect(
            self.start_playback)
        self.transcode_thread.start()

    @Slot(float)
    def handle_transcode_progress(self, progress: float):
        self.progress_bar.setValue(int(progress * 1000))

    @Slot(Path)
    def start_playback(self, playback_path: Path):
        """Start playing back the transcoded video."""
        import imageio
        try:
            reader = imageio.get_reader(str(playback_path))
            try:
                metadata = reader.get_meta_data()
            finally:
                reader.close()
            self.fps = metadata.get('fps') or DEFAULT_PLAYBACK_FPS
            self.duration = metadata.get('duration') or 0.0
        except Exception as exception:
            print(f'Failed to read the metadata of {playback_path}: '
                  f'{exception}', file=sys.stderr)
            self.reject()
            return
        if not self.fps or not self.duration:
            self.reject()
            return
        self.total_frames = max(int(self.duration * self.fps), 1)
        self.seek_slider.setRange(0, self.total_frames - 1)
        self.seek_slider.setEnabled(True)
        self.play_pause_button.setEnabled(True)
        self.progress_bar.hide()
        self.update_timestamp_label()
        self.decode_thread = VideoDecodeThread(self, playback_path, self.fps)
        self.decode_thread.frame_decoded.connect(self.display_frame)
        self.decode_thread.playback_finished.connect(self.handle_playback_finished)
        self.decode_thread.error_occurred.connect(self.handle_decode_error)
        self.decode_thread.play()
        self.decode_thread.start()
        self.play_pause_button.setText('Pause')

    @Slot(QImage, int)
    def display_frame(self, image: QImage, frame_index: int):
        if self.is_closing:
            return
        # Ignore frames that were decoded before a newer frame; the queued
        # signal delivery can reorder frames when seeking rapidly.
        if frame_index < self.current_frame_index:
            return
        self.current_frame_index = frame_index
        pixmap = QPixmap.fromImage(image)
        pixmap = pixmap.scaled(
            self.video_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.video_label.setPixmap(pixmap)
        if not self.is_seeking:
            self.seek_slider.setValue(frame_index)
        self.update_timestamp_label()

    def update_timestamp_label(self):
        current = self.current_frame_index / self.fps
        self.timestamp_label.setText(
            f'{format_timestamp(current)} / {format_timestamp(self.duration)}')

    @Slot()
    def toggle_play_pause(self):
        if self.decode_thread is None:
            return
        if self.decode_thread.is_playing:
            self.decode_thread.pause()
            self.play_pause_button.setText('Play')
        else:
            self.decode_thread.play()
            self.play_pause_button.setText('Pause')

    @Slot()
    def handle_playback_finished(self):
        self.play_pause_button.setText('Play')

    @Slot(str)
    def handle_decode_error(self, message: str):
        print(f'Video playback error: {message}', file=sys.stderr)
        self.play_pause_button.setText('Play')

    @Slot(int)
    def handle_slider_moved(self, value: int):
        self.is_seeking = True
        self.current_frame_index = value
        self.update_timestamp_label()
        if self.decode_thread is not None:
            self.decode_thread.seek(value)

    @Slot()
    def handle_slider_released(self):
        self.is_seeking = False

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Space:
            self.toggle_play_pause()
            event.accept()
            return
        if self.decode_thread is None:
            super().keyPressEvent(event)
            return
        seek_amount = int(self.fps * 5)
        if event.key() == Qt.Key.Key_Left:
            target = max(self.current_frame_index - seek_amount, 0)
        elif event.key() == Qt.Key.Key_Right:
            target = min(self.current_frame_index + seek_amount,
                         self.total_frames - 1)
        elif event.key() == Qt.Key.Key_Home:
            target = 0
        elif event.key() == Qt.Key.Key_End:
            target = self.total_frames - 1
        else:
            super().keyPressEvent(event)
            return
        self.current_frame_index = target
        self.seek_slider.setValue(target)
        self.update_timestamp_label()
        self.decode_thread.seek(target)
        event.accept()

    def closeEvent(self, event: QCloseEvent):
        self.is_closing = True
        if self.transcode_thread is not None:
            self.transcode_thread.cancel()
            self.transcode_thread.wait(3000)
        if self.decode_thread is not None:
            self.decode_thread.stop()
            self.decode_thread.wait(3000)
        super().closeEvent(event)
