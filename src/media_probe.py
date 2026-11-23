"""Everything we ask ffprobe, in one cached call with a timeout. Nothing else runs ffprobe."""
import functools
import json
import os
import subprocess
from dataclasses import dataclass

TIMEOUT_S = 30


@dataclass(frozen=True)
class MediaInfo:
    duration: float         # seconds; 0.0 when unknown
    width: int              # as stored; see display_size for how it is shown
    height: int
    fps: float
    time_base: str          # of the video stream, e.g. "1/12800"
    pix_fmt: str
    rotation: int           # degrees, from phone rotation metadata
    has_video: bool
    has_audio: bool
    codec: str
    audio_codec: str
    sample_rate: int
    channels: int
    size_bytes: int
    bit_rate: int
    rate: str = ""          # the exact frame rate, e.g. "30000/1001"
    video_seconds: float = 0.0  # the video stream alone: sound often runs a little longer
    comment: str = ""           # the container's comment tag

    @property
    def display_size(self) -> tuple[int, int]:
        """(width, height) as a player shows it, rotation applied."""
        return (self.height, self.width) if abs(self.rotation) % 180 == 90 else (self.width, self.height)


def _number(value, kind=float):
    try:
        return kind(float(value))
    except (TypeError, ValueError, OverflowError):
        return kind(0)


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            return round(_number(side["rotation"]))
    return _number((stream.get("tags") or {}).get("rotate"), int)


@functools.lru_cache(maxsize=512)
def _read(path: str, mtime_ns: int, size: int) -> MediaInfo:     # raises, so failures are never cached
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True, timeout=TIMEOUT_S, check=True).stdout
    data = json.loads(out)
    streams, fmt = data.get("streams", []), data.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    rate = video.get("r_frame_rate", "0/1")
    top, _, bottom = rate.partition("/")
    return MediaInfo(
        duration=_number(fmt.get("duration") or video.get("duration")),
        width=_number(video.get("width"), int), height=_number(video.get("height"), int),
        fps=round(_number(top) / (_number(bottom) or 1.0), 2), time_base=video.get("time_base", ""),
        pix_fmt=video.get("pix_fmt", ""), rotation=_rotation(video) if video else 0,
        has_video=bool(video), has_audio=bool(audio),
        codec=video.get("codec_name", "unknown"), audio_codec=audio.get("codec_name", "unknown"),
        sample_rate=_number(audio.get("sample_rate"), int), channels=_number(audio.get("channels"), int),
        size_bytes=size, bit_rate=_number(fmt.get("bit_rate"), int),
        rate=rate, video_seconds=_number(video.get("duration")),
        comment=str((fmt.get("tags") or {}).get("comment", "")),
    )


def info(path: str) -> MediaInfo | None:
    """What ``path`` is, or None when it can't be read as media within the timeout."""
    try:
        stat = os.stat(path)
        # keyed on the file's version: renders overwrite files under the same name
        return _read(os.path.abspath(path), stat.st_mtime_ns, stat.st_size)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def duration(path: str, fallback: float | None = 0.0) -> float | None:
    found = info(path)
    return found.duration if found and found.duration > 0 else fallback


def video_duration(path: str) -> float | None:
    """The video stream's own length, when the file records one."""
    found = info(path)
    return found.video_seconds if found and found.video_seconds > 0 else None


def comment(path: str) -> str:
    found = info(path)
    return found.comment if found else ""


def frame_size(path: str, fallback: tuple[int, int] = (1080, 1920)) -> tuple[int, int]:
    """(width, height) as stored."""
    found = info(path)
    return (found.width, found.height) if found and found.width and found.height else fallback


def upload_problem(path: str, name: str) -> str | None:
    """A user-facing reason this upload can't be edited, or None if it's fine."""
    found = info(path)
    if not found or not found.has_video or found.duration <= 0:
        return f"“{name}” isn't a video file CutSmart can read. Try an MP4, MOV, MKV or WebM."
    if not found.has_audio:
        return f"“{name}” has no audio track. CutSmart edits by speech, so the video needs sound."
    return None
