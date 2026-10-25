from __future__ import annotations

import hashlib
import os
import queue
import sys
import tempfile
import threading
from pathlib import Path

from PIL import Image as PilImage


def _get_ffmpeg_exe() -> str:
    """Get the ffmpeg executable used for video decoding.

    In a PyInstaller build the ffmpeg binary is bundled into the
    `imageio_ffmpeg/binaries` directory (see taggui.spec), which is not the
    location the imageio_ffmpeg package finds on its own. Point the
    IMAGEIO_FFMPEG_EXE environment variable at it; imageio_ffmpeg checks that
    variable first. In a normal installation this is a no-op and the bundled
    binary of the imageio_ffmpeg package is used.
    """
    if 'IMAGEIO_FFMPEG_EXE' not in os.environ:
        bundled_directory = (Path(sys.executable).parent / '_taggui'
                             / 'imageio_ffmpeg' / 'binaries')
        if bundled_directory.is_dir():
            for binary in bundled_directory.iterdir():
                if binary.name.startswith('ffmpeg'):
                    os.environ['IMAGEIO_FFMPEG_EXE'] = str(binary)
                    break
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

# Video file formats that can be decoded with imageio-ffmpeg. Videos are
# treated like animated images: frames are extracted for thumbnails and
# multi-frame captioning.
VIDEO_SUFFIXES = {'.mp4', '.webm', '.mov', '.mkv', '.avi'}

# Fraction of the video duration at which a representative frame is taken
# for thumbnails and single-frame captioning, so that intros, fades and
# black frames at the start of a video are avoided.
REPRESENTATIVE_FRAME_POSITION = 0.25

# Default cap on the number of frames extracted from a single video, so
# that very long videos do not produce excessively large captioning
# batches. Can be changed with the `max_frames` parameter of
# `load_video_frames` (0 means no cap).
DEFAULT_MAX_VIDEO_FRAMES = 60


def is_video_path(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


def get_video_metadata(video_path: Path) -> tuple[int, int] | None:
    """Get the (width, height) of a video without decoding any frames.

    Returns None if the video cannot be read.
    """
    try:
        import imageio
        reader = imageio.get_reader(str(video_path))
        try:
            metadata = reader.get_meta_data()
        finally:
            reader.close()
        size = metadata.get('size')
        if size:
            return size[0], size[1]
    except Exception as exception:
        print(f'Failed to get video metadata for {video_path}: '
              f'{exception}', file=sys.stderr)
    return None


def load_video_frames(video_path: Path, frame_count: int,
                      position: float = 0.0,
                      frames_per_interval: int | None = None,
                      interval_seconds: float | None = None,
                      max_frames: int = DEFAULT_MAX_VIDEO_FRAMES
                      ) -> list[PilImage]:
    """Load evenly-spaced frames from a video.

    `frame_count` is the number of frames to load. If `frames_per_interval`
    and `interval_seconds` are given, `frame_count` is treated as the number
    of frames per `interval_seconds` of video and scaled with the actual
    duration, so that long videos are sampled more densely than short ones.
    The resulting count is clamped to the video's frame count and to
    `max_frames` (0 means no cap).

    `position` is used when only a single frame is requested (frame_count ==
    1) to select the frame as a fraction of the video duration, so that
    thumbnails and captions are not taken from the very beginning where
    intros, fades or black frames are common.

    Returns an empty list if the video cannot be decoded.
    """
    try:
        import imageio
        import numpy as np
        _get_ffmpeg_exe()
        reader = imageio.get_reader(str(video_path))
        try:
            metadata = reader.get_meta_data()
            fps = metadata.get('fps') or 0
            duration = metadata.get('duration') or 0
            if not fps or not duration:
                return []
            total_frames = int(fps * duration)
            if total_frames < 1:
                return []
            if frames_per_interval and interval_seconds:
                frame_count = int(round(
                    frames_per_interval * duration / interval_seconds))
                frame_count = min(frame_count, total_frames)
                if max_frames > 0:
                    frame_count = min(frame_count, max_frames)
            if frame_count <= 1:
                frame_indices = [min(int(total_frames * position),
                                     total_frames - 1)]
            else:
                frame_indices = np.linspace(0, total_frames - 1, frame_count,
                                            dtype=int)
            frames = []
            # Sort the indices so that seeking always moves forward; this is
            # significantly faster than random-access seeks because the
            # decoder does not have to restart from a keyframe for every
            # frame.
            for index in sorted(int(i) for i in frame_indices):
                try:
                    frame = reader.get_data(index)
                except IndexError:
                    # The frame count estimate can overshoot for videos with
                    # variable frame rates.
                    continue
                # imageio's ffmpeg reader returns RGB frames.
                frames.append(PilImage.fromarray(frame).convert('RGB'))
            return frames
        finally:
            reader.close()
    except Exception as exception:
        print(f'Failed to load frames from {video_path}: '
              f'{exception}', file=sys.stderr)
        return []


def get_thumbnail_cache_path(video_path: Path, width: int,
                             cache_directory: Path | None = None) -> Path:
    """Get the cache path for the thumbnail of a video.

    The file name is a hash of the resolved path, modification time, size
    and requested width of the video, so that a changed video or a different
    thumbnail width never reuses a stale thumbnail.
    """
    if cache_directory is None:
        cache_directory = Path(tempfile.gettempdir()) / 'taggui' / 'thumbnails'
    stat = video_path.stat()
    key = (f'{video_path.resolve()}_{stat.st_mtime_ns}_{stat.st_size}_{width}'
           .encode())
    return cache_directory / (hashlib.sha1(key).hexdigest() + '.jpg')


def get_video_thumbnail(
        video_path: Path, width: int,
        cache_directory: Path | None = None) -> PilImage | None:
    """Get a thumbnail image for a video.

    The thumbnail is a single frame taken partway into the video, scaled to
    `width` pixels. Thumbnails are cached on disk because seeking into a
    video is much slower than reading the first frame of a GIF. Returns None
    if the video cannot be decoded.
    """
    cache_path = get_thumbnail_cache_path(video_path, width, cache_directory)
    try:
        cached_thumbnail = PilImage.open(cache_path)
        # Force the image data to load so that a corrupt cache file is
        # detected here instead of when the caller uses the image.
        cached_thumbnail.load()
        return cached_thumbnail
    except Exception:
        pass
    frames = load_video_frames(video_path, 1,
                               position=REPRESENTATIVE_FRAME_POSITION)
    if not frames:
        return None
    thumbnail = frames[0]
    if thumbnail.width > width:
        thumbnail = thumbnail.resize(
            (width, int(width * thumbnail.height / thumbnail.width)),
            resample=PilImage.Resampling.BICUBIC)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file and rename it so that a crash or a
        # concurrent reader never sees a partially written thumbnail.
        temporary_path = cache_path.with_name(cache_path.name + '.tmp')
        thumbnail.save(temporary_path, format='JPEG', quality=85)
        os.replace(temporary_path, cache_path)
    except OSError as exception:
        print(f'Failed to cache the thumbnail of {video_path}: '
              f'{exception}', file=sys.stderr)
    return thumbnail


# Maximum number of decoded frames the playback decoder keeps buffered
# ahead of the display position. The frames are scaled to the display width,
# so the buffer stays small even for long videos.
PLAYBACK_FRAME_BUFFER_SIZE = 8

# Sentinel put into the frame queue when decoding has finished or failed.
_DECODE_END = object()


class VideoReader:
    """Sequential video reader for playback, backed by imageio-ffmpeg.

    Decoding happens on a background thread: the decoder reads frames one
    after another (which is much faster than random-access seeks) and puts
    them into a bounded queue, from which the GUI thread takes frames in
    display order. Frames are scaled to `display_width` while decoding so
    that the buffer stays small and the GUI thread never has to scale.

    The reader's public `get_data(index)` does the seeking: imageio's
    ffmpeg plugin seeks with `-ss` for long stretches and then decodes
    forward, so seeking backwards or by a large amount restarts the ffmpeg
    subprocess. Random-access frame reads are therefore only used for
    single frames while paused (e.g. scrubbing); during playback the
    decoder always reads strictly sequentially.
    """

    def __init__(self, video_path: Path, display_width: int):
        import imageio
        _get_ffmpeg_exe()
        self.video_path = video_path
        self.display_width = max(1, display_width)
        self.reader = imageio.get_reader(str(video_path))
        metadata = self.reader.get_meta_data()
        self.fps = metadata.get('fps') or 0
        duration = metadata.get('duration') or 0
        self.frame_count = max(1, int(self.fps * duration)) if self.fps else 0
        self.duration = duration
        self.width, self.height = metadata.get('size') or (0, 0)
        self._frame_queue: queue.Queue = queue.Queue(
            maxsize=PLAYBACK_FRAME_BUFFER_SIZE)
        self._decoder_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._next_frame_index = 0
        self._decoder_running = False
        self._closed = False

    def _scale_frame(self, pil_image: PilImage) -> PilImage:
        if pil_image.width > self.display_width:
            pil_image = pil_image.resize(
                (self.display_width,
                 int(self.display_width * pil_image.height / pil_image.width)),
                resample=PilImage.Resampling.BICUBIC)
        return pil_image

    def _put(self, item) -> bool:
        """Put an item into the frame queue, giving up when stopped.

        The queue is bounded so that the decoder never runs far ahead of
        the display; a blocking put would deadlock `stop`, which joins the
        decoder thread, so the put times out periodically to check the stop
        event.
        """
        while not self._stop_event.is_set():
            try:
                self._frame_queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _decode_frames(self, start_index: int):
        """Decode frames sequentially, starting at `start_index`."""
        try:
            for index in range(start_index, self.frame_count):
                if self._stop_event.is_set():
                    break
                try:
                    frame = self.reader.get_data(index)
                except (IndexError, RuntimeError):
                    # The frame count estimate can overshoot for videos
                    # with variable frame rates, and the ffmpeg pipe ends
                    # with a RuntimeError at the end of the stream.
                    break
                pil_image = self._scale_frame(
                    PilImage.fromarray(frame).convert('RGB'))
                if not self._put((index, pil_image)):
                    break
        except Exception as exception:
            print(f'Failed to decode frames from {self.video_path}: '
                  f'{exception}', file=sys.stderr)
        finally:
            self._put(_DECODE_END)

    def start(self):
        """Start the background decoder at the current position."""
        if self._decoder_running:
            return
        self._stop_event.clear()
        self._decoder_running = True
        self._decoder_thread = threading.Thread(
            target=self._decode_frames,
            args=(self._next_frame_index,), daemon=True)
        self._decoder_thread.start()

    def stop(self):
        """Stop the background decoder and discard buffered frames."""
        self._stop_event.set()
        if self._decoder_thread is not None:
            self._decoder_thread.join()
            self._decoder_thread = None
        self._decoder_running = False
        while True:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def poll_frame(self) -> tuple[int, PilImage] | None | bool:
        """Get the next frame for display without blocking.

        Returns a (frame_index, image) tuple, None when the end of the
        video has been reached or decoding failed, or False when no frame
        is ready yet (the caller should skip this display update).
        """
        try:
            item = self._frame_queue.get_nowait()
        except queue.Empty:
            return False
        if item is _DECODE_END:
            return None
        index, image = item
        self._next_frame_index = index + 1
        return index, image

    def seek(self, frame_index: int):
        """Set the position at which playback resumes and stop the decoder.

        The next `start` call resumes decoding at `frame_index`. Seeking is
        done by the imageio ffmpeg plugin on the next read: it seeks to the
        nearest keyframe with `-ss` and decodes forward to the exact frame.
        """
        frame_index = max(0, min(frame_index, self.frame_count - 1))
        self.stop()
        self._next_frame_index = frame_index

    def get_frame(self, frame_index: int) -> PilImage | None:
        """Decode a single frame, blocking until it is available.

        Used to show a frame while paused (e.g. when scrubbing) without
        keeping the background decoder running. The imageio ffmpeg plugin
        seeks to the nearest keyframe and decodes forward to the exact
        frame, so this is only fast enough for interactive use.
        """
        if self._closed:
            return None
        frame_index = max(0, min(frame_index, self.frame_count - 1))
        try:
            frame = self.reader.get_data(frame_index)
        except Exception:
            return None
        return self._scale_frame(PilImage.fromarray(frame).convert('RGB'))

    def close(self):
        """Stop the decoder and release the video file."""
        self.stop()
        self._closed = True
        try:
            self.reader.close()
        except Exception:
            pass
