from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
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


# Maximum width of the transcoded playback copies of videos, in pixels.
# Playback copies are decoded at a reduced resolution because seeking in the
# original videos is too slow for interactive playback (see
# `transcode_video_for_playback`).
PLAYBACK_VIDEO_WIDTH = 640

# JPEG quality of the transcoded playback copies of videos.
PLAYBACK_VIDEO_QUALITY = 5


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


def get_playback_cache_path(video_path: Path,
                            cache_directory: Path | None = None) -> Path:
    """Get the cache path for the transcoded playback copy of a video.

    The file name is a hash of the resolved path, modification time and size
    of the video, so that a changed video never reuses a stale playback copy.
    """
    if cache_directory is None:
        cache_directory = Path(tempfile.gettempdir()) / 'taggui' / 'playback'
    stat = video_path.stat()
    key = f'{video_path.resolve()}_{stat.st_mtime_ns}_{stat.st_size}'.encode()
    return cache_directory / (hashlib.sha1(key).hexdigest() + '.avi')


def transcode_video_for_playback(
        video_path: Path, progress_callback=None,
        cancel_check=None) -> Path | None:
    """Transcode a video into a seek-friendly playback copy.

    Videos are usually encoded with long GOPs (a keyframe only every few
    seconds), which makes random-access seeking slow: the decoder has to
    restart from the previous keyframe and decode forward for every seek.
    For interactive playback this is transcoded once into an MJPEG AVI with
    every frame as a keyframe, scaled down to `PLAYBACK_VIDEO_WIDTH` pixels
    wide. The copy is typically a fraction of the original size and can be
    seeked into in well under a second. Transcoding is done with the ffmpeg
    binary bundled by imageio-ffmpeg, so no extra dependency is needed.

    `progress_callback` is called with the transcoding progress as a fraction
    between 0 and 1 (estimated from the output duration), and `cancel_check`
    is called regularly; if it returns True, the transcoding is aborted.

    Returns the path of the playback copy (cached on disk, so subsequent
    calls for the same video are instant), or None if the transcoding failed
    or was canceled.
    """
    cache_path = get_playback_cache_path(video_path)
    try:
        if cache_path.exists():
            return cache_path
    except OSError:
        pass
    _get_ffmpeg_exe()
    ffmpeg_exe = os.environ.get('IMAGEIO_FFMPEG_EXE')
    if not ffmpeg_exe:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    metadata = get_video_metadata(video_path)
    if metadata is None:
        return None
    width, height = metadata
    scale_filter = (f'scale={PLAYBACK_VIDEO_WIDTH}:-2'
                    if width > PLAYBACK_VIDEO_WIDTH else 'null')
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file and rename it so that a crash or a
        # concurrent reader never sees a partially written playback copy.
        # The temporary file keeps the .avi extension because ffmpeg chooses
        # the output format from the extension.
        temporary_path = cache_path.with_name('transcoding-' + cache_path.name)
        command = [ffmpeg_exe, '-y', '-i', str(video_path),
                   '-vf', scale_filter, '-c:v', 'mjpeg',
                   '-q:v', str(PLAYBACK_VIDEO_QUALITY), '-an',
                   str(temporary_path)]
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        duration = None
        try:
            import imageio
            reader = imageio.get_reader(str(video_path))
            try:
                metadata = reader.get_meta_data()
                duration = metadata.get('duration') or 0
            finally:
                reader.close()
        except Exception:
            pass
        while process.poll() is None:
            if cancel_check is not None and cancel_check():
                process.terminate()
                process.wait()
                temporary_path.unlink(missing_ok=True)
                return None
            if progress_callback is not None and duration:
                progress = _get_transcode_progress(process, temporary_path,
                                                  duration)
                if progress is not None:
                    progress_callback(progress)
            time.sleep(0.1)
        stderr = process.stderr.read().decode(errors='replace') if process.stderr else ''
        if process.returncode != 0:
            print(f'Failed to transcode {video_path} for playback: '
                  f'{stderr.strip()}', file=sys.stderr)
            temporary_path.unlink(missing_ok=True)
            return None
        os.replace(temporary_path, cache_path)
        if progress_callback is not None:
            progress_callback(1.0)
        return cache_path
    except OSError as exception:
        print(f'Failed to transcode {video_path} for playback: '
              f'{exception}', file=sys.stderr)
        return None


def _get_transcode_progress(process: subprocess.Popen, output_path: Path,
                            duration: float) -> float | None:
    """Estimate the transcoding progress from the output file's duration.

    ffmpeg writes the MJPEG AVI progressively, so the duration of the
    partially written file grows linearly with the progress. Returns None if
    the progress cannot be determined.
    """
    try:
        import imageio
        reader = imageio.get_reader(str(output_path))
        try:
            metadata = reader.get_meta_data()
            output_duration = metadata.get('duration') or 0
        finally:
            reader.close()
        if output_duration > 0 and duration > 0:
            return min(output_duration / duration, 1.0)
    except Exception:
        pass
    return None
