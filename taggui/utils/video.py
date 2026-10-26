from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
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


def _log_error(message: str):
    """Print an error message, tolerating a missing stderr.

    In windowed (console-less) frozen builds, sys.stderr can be None;
    printing to it would raise inside Qt slots, where the exception is
    silently swallowed and aborts the rest of the slot.
    """
    if sys.stderr is not None:
        print(message, file=sys.stderr)


# Video file formats that can be decoded with imageio-ffmpeg. Videos are
# treated like animated images: frames are extracted for thumbnails and
# multi-frame captioning.
VIDEO_SUFFIXES = {'.mp4', '.m4v', '.webm', '.mov', '.mkv', '.avi', '.wmv',
                  '.flv', '.mpg', '.mpeg', '.3gp'}

# Fraction of the video duration at which a representative frame is taken
# for thumbnails and single-frame captioning, so that intros, fades and
# black frames at the start of a video are avoided.
REPRESENTATIVE_FRAME_POSITION = 0.25

# Default cap on the number of frames extracted from a single video, so
# that very long videos do not produce excessively large captioning
# batches. Can be changed with the `max_frames` parameter of
# `load_video_frames` (0 means no cap).
DEFAULT_MAX_VIDEO_FRAMES = 60

# Decoding is done directly with imageio_ffmpeg.read_frames() instead of
# the imageio package: imageio resolves its plugins through
# importlib.metadata package discovery at import time, which does not
# work in PyInstaller-frozen builds ("No package metadata was found for
# imageio"). read_frames() shells out to the bundled ffmpeg binary and
# has no such dependency.


def is_video_path(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


def _read_frames(video_path: Path, seek_seconds: float = 0.0):
    """Start an ffmpeg decode process for a video.

    Returns (metadata, frame_generator). The generator yields raw RGB
    bytes per frame; the caller must consume or close it. `seek_seconds`
    is applied as an ffmpeg input seek, which is fast even for large
    offsets.
    """
    import imageio_ffmpeg
    _get_ffmpeg_exe()
    input_params = []
    if seek_seconds > 0:
        input_params = ['-ss', f'{seek_seconds:.6f}']
    generator = imageio_ffmpeg.read_frames(
        str(video_path), pix_fmt='rgb24', input_params=input_params)
    metadata = next(generator)
    return metadata, generator


def _frame_to_pil_image(frame_bytes: bytes, size: tuple[int, int]) -> PilImage:
    width, height = size
    array = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
        height, width, 3)
    return PilImage.fromarray(array).convert('RGB')


def get_video_metadata(video_path: Path) -> tuple[int, int] | None:
    """Get the (width, height) of a video without decoding any frames.

    Returns None if the video cannot be read.
    """
    try:
        metadata, generator = _read_frames(video_path)
        generator.close()
        size = metadata.get('size')
        if size:
            return size[0], size[1]
    except Exception as exception:
        _log_error(f'Failed to get video metadata for {video_path}: '
                   f'{exception}')
    return None


class VideoFrameReader:
    """Sequential and seeking frame reader for a video.

    Frames are decoded with imageio_ffmpeg.read_frames(), running ffmpeg
    as a subprocess. Decoding is sequential: reading the next frames
    after the last decoded one is a fast forward read, while any seek
    (forward jump or backward) starts a new ffmpeg process positioned
    just before the target frame, which is fast because ffmpeg input
    seeking jumps to the nearest keyframe.
    """

    def __init__(self, video_path: Path):
        self.video_path = video_path
        self._generator = None
        self._next_index = 0
        self._frame_count = 0
        self.fps = 0.0
        self.duration = 0.0
        self._size = (0, 0)
        self._open(0.0, 0)

    def _open(self, seek_seconds: float, seek_index: int):
        self._close_generator()
        metadata, generator = _read_frames(self.video_path, seek_seconds)
        self._generator = generator
        # The new stream starts at the seek position; the next frame the
        # generator yields is the one at `seek_index`.
        self._next_index = seek_index
        self.fps = metadata.get('fps') or 0.0
        self.duration = metadata.get('duration') or 0.0
        size = metadata.get('size') or (0, 0)
        self._size = (size[0], size[1])
        if self.fps and self.duration:
            self._frame_count = max(1, int(self.fps * self.duration))

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def read_frame(self, index: int) -> PilImage | None:
        """Decode the frame at `index`, or None if it cannot be decoded.

        Sequential forward reads reuse the running ffmpeg process;
        reading any earlier index, or jumping forward, restarts the
        process with an input seek to just before the target frame.
        """
        if not self._frame_count:
            return None
        index = max(0, min(index, self._frame_count - 1))
        try:
            if index < self._next_index or index - self._next_index > 30:
                # Restart with an input seek just before the target
                # frame; ffmpeg seeks to the nearest keyframe, then
                # decodes forward to the exact position.
                self._open(index / self.fps if self.fps else 0.0, index)
            while self._next_index < index:
                next(self._generator)
                self._next_index += 1
            frame_bytes = next(self._generator)
            self._next_index += 1
            return _frame_to_pil_image(frame_bytes, self._size)
        except StopIteration:
            # The frame count estimate can overshoot for videos with
            # variable frame rates.
            self._frame_count = self._next_index
            return None
        except (OSError, ValueError) as exception:
            _log_error(f'Failed to read frame {index} from '
                       f'{self.video_path}: {exception}')
            return None

    def _close_generator(self):
        if self._generator is not None:
            try:
                self._generator.close()
            except Exception:
                pass
            self._generator = None

    def close(self):
        self._close_generator()

    def __del__(self):
        self.close()


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
        reader = VideoFrameReader(video_path)
        try:
            if not reader.frame_count:
                return []
            total_frames = reader.frame_count
            if frames_per_interval and interval_seconds:
                frame_count = int(round(
                    frames_per_interval * reader.duration / interval_seconds))
                frame_count = min(frame_count, total_frames)
                if max_frames > 0:
                    frame_count = min(frame_count, max_frames)
            if frame_count <= 1:
                frame_indices = [min(int(total_frames * position),
                                     total_frames - 1)]
            else:
                frame_indices = np.linspace(0, total_frames - 1,
                                            frame_count, dtype=int)
            frames = []
            # Sort the indices so that seeking always moves forward; this is
            # significantly faster than random-access seeks because the
            # decoder does not have to restart from a keyframe for every
            # frame.
            for index in sorted(int(i) for i in frame_indices):
                frame = reader.read_frame(index)
                if frame is None:
                    continue
                frames.append(frame)
            return frames
        finally:
            reader.close()
    except Exception as exception:
        _log_error(f'Failed to load frames from {video_path}: {exception}')
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
        _log_error(f'Failed to cache the thumbnail of {video_path}: '
                   f'{exception}')
    return thumbnail
