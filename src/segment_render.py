"""Frame-exact video pieces, and the lossless join that makes an edit out of them.

An edit is a list of pieces: short H.264 files on one canvas (size and frame rate), none longer
than CHUNK_S, all encoded the same way so they join without re-encoding. A piece is named by what
is in it, so changing an edit encodes only the pieces whose content changed. Audio is joined
sample-exact on the same frame grid and encoded once, which is quick.

Nothing here knows about jobs, captions or HTTP.
"""
import hashlib
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

import media_probe
from filenames import concat_entry

CHUNK_S = 6
MIN_TAIL_S = 1
SAMPLE_RATE = 48000
CHANNELS = 2
DECLICK_S = 0.003       # a ramp where two cut ends meet
SEEK_BEFORE = 0.1       # frames: seek just before the frame wanted, so rounding can't skip it

_encoders = ThreadPoolExecutor(max_workers=4, thread_name_prefix="encode")


@dataclass(frozen=True)
class Canvas:
    width: int
    height: int
    fps: Fraction

    def seconds(self, frames: int) -> float:
        return float(frames / self.fps)

    def frames(self, seconds: float) -> int:
        return round(seconds * float(self.fps))

    def samples(self, frames: int) -> int:
        return round(frames * SAMPLE_RATE / self.fps)


@dataclass(frozen=True)
class Piece:
    key: str
    path: str
    frames: int


def even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def frame_rate(rate: str) -> Fraction:
    """A constant rate to edit at. Phones' variable rates often claim nonsense such as 90000/1."""
    try:
        fps = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return Fraction(30)
    if 5 <= fps <= 60:
        return fps
    return Fraction(60) if 60 < fps <= 240 else Fraction(30)


def frame_ranges(keep, canvas: Canvas) -> list[tuple[int, int]]:
    """The kept ranges on the frame grid; one under a frame long comes out empty."""
    return [(canvas.frames(start), max(canvas.frames(start), canvas.frames(end))) for start, end in keep]


def split_range(first: int, end: int, canvas: Canvas) -> list[tuple[int, int]]:
    """Cut a range at fixed points of the original, so the same footage makes the same pieces."""
    chunk, shortest = canvas.frames(CHUNK_S), canvas.frames(MIN_TAIL_S)
    marks = [first]
    marks += [at for at in range((first // chunk + 1) * chunk, end, chunk) if end - at >= shortest]
    marks.append(end)
    return [(a, b) for a, b in zip(marks, marks[1:]) if b > a]


def content_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def run(command: list[str], what: str) -> None:
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"Could not {what}: {(done.stderr or '').strip()[-400:]}")


def video_codec(canvas: Canvas) -> list[str]:
    """How every piece is encoded. No B-frames: their reordering delay breaks lossless joins."""
    number = canvas.fps.numerator
    return ["-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-bf", "0", "-pix_fmt", "yuv420p",
            "-g", str(canvas.frames(2)), "-video_track_timescale", str(number * 512 if number < 1000 else number)]


def fit_filter(canvas: Canvas) -> str:
    return (f"scale=iw*sar:ih,scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=decrease:out_range=tv,"
            f"pad={canvas.width}:{canvas.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")


def encode_range(source: str, first: int, end: int, canvas: Canvas, look: list[str | None], output: str) -> None:
    """Frames [first, end) of ``source`` as one piece. Past the last frame, that frame is held."""
    at = (first - SEEK_BEFORE) / canvas.fps
    length = media_probe.video_duration(source)
    if length:
        at = min(at, Fraction(length).limit_denominator(1000) - Fraction(1) / canvas.fps)
    chain = [f"fps={canvas.fps}:start_time=0", "tpad=stop_mode=clone:stop=-1",
             *(part for part in look if part), fit_filter(canvas)]
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, float(at)):.6f}", "-i", source,
         "-map", "0:v:0", "-vf", ",".join(chain), "-frames:v", str(end - first), *video_codec(canvas), output],
        f"encode {canvas.seconds(first):.2f}-{canvas.seconds(end):.2f}s")
    if not os.path.exists(output) or not media_probe.duration(output):
        raise RuntimeError(f"Could not encode {canvas.seconds(first):.2f}-{canvas.seconds(end):.2f}s: no frames")


def build_pieces(wanted: list[tuple], prefix: str, known: list[Piece], on_progress=lambda done, total: None) -> list[Piece]:
    """The pieces for ``wanted`` [(key, frames, encode(path))], in that order.

    A piece already in ``known`` is reused. On any failure nothing made here is left behind.
    """
    have = {piece.key: piece for piece in known if os.path.exists(piece.path)}
    todo = {}
    for key, frames, encode in wanted:
        if key not in have and key not in todo:
            todo[key] = (Piece(key, f"piece_{prefix}_{key}.mp4", frames), encode)
    made = {key: piece for key, (piece, _) in todo.items()}
    futures = [_encoders.submit(encode, piece.path) for piece, encode in todo.values()]
    try:
        for done, future in enumerate(as_completed(futures), 1):
            future.result()
            on_progress(done, len(futures))
    except BaseException:
        for future in futures:
            future.cancel()
        for future in futures:
            if not future.cancelled():
                future.exception()      # wait: a running encode would write its file again
        for piece in made.values():
            _remove(piece.path)
        raise
    return [have.get(key) or made[key] for key, _, _ in wanted]


def _remove(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


class Audio:
    """A file's sound as 48 kHz stereo samples, on disk while in use."""

    def __init__(self, source: str, prefix: str):
        self.source = source
        self._dir = tempfile.TemporaryDirectory(prefix=f"audio_{prefix}_")
        self._samples = None

    def __enter__(self):
        try:
            path = os.path.join(self._dir.name, "a.pcm")
            run(["ffmpeg", "-y", "-v", "error", "-i", self.source, "-map", "0:a:0",
                 "-af", f"aresample={SAMPLE_RATE}:async=1:first_pts=0", "-ac", str(CHANNELS), "-f", "s16le", path],
                "read the audio")
            self._samples = (np.memmap(path, dtype="<i2", mode="r").reshape(-1, CHANNELS) if os.path.getsize(path)
                             else np.zeros((0, CHANNELS), dtype="<i2"))
        except BaseException:
            self._dir.cleanup()
            raise
        return self

    def __exit__(self, *exc):
        self._samples = None
        self._dir.cleanup()

    def read(self, first: int, count: int) -> np.ndarray:
        """``count`` samples from ``first``, silence where the file has none."""
        out = np.zeros((count, CHANNELS), dtype=np.float32)
        have = self._samples[first:first + count]
        out[:len(have)] = have
        return out


def kept_audio(sound: Audio, ranges: list[tuple[int, int]], canvas: Canvas):
    """The kept ranges' samples, each exactly as long as its frames."""
    ramp = round(DECLICK_S * SAMPLE_RATE)
    ranges = [kept for kept in ranges if kept[1] > kept[0]]
    done = 0
    for index, (first, end) in enumerate(ranges):
        count = canvas.samples(done + end - first) - canvas.samples(done)
        done += end - first
        samples = sound.read(canvas.samples(first), count)
        edge = min(ramp, count // 2)
        if edge and index:
            samples[:edge] *= np.linspace(0, 1, edge, dtype=np.float32)[:, None]
        if edge and index < len(ranges) - 1:
            samples[-edge:] *= np.linspace(1, 0, edge, dtype=np.float32)[:, None]
        yield samples


def join(pieces: list[Piece], canvas: Canvas, audio, prefix: str, output: str) -> None:
    """The pieces copied end to end, under ``audio`` (an iterable of sample blocks) encoded once."""
    manifest = f"join_{prefix}.txt"
    with open(manifest, "w", encoding="utf-8") as listing:
        listing.writelines(concat_entry(piece.path) for piece in pieces)
    length = canvas.seconds(sum(piece.frames for piece in pieces))
    command = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", manifest,
               "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-t", f"{length:.6f}", "-movflags", "+faststart", output]
    ok = False
    try:
        with tempfile.TemporaryFile(mode="w+") as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                for block in audio:
                    process.stdin.write(np.clip(block, -32768, 32767).astype("<i2").tobytes())
                process.stdin.close()
            except BrokenPipeError:
                pass    # it has what it needs, or it failed and says why below
            except BaseException:
                process.kill()
                raise
            if process.wait():
                errors.seek(0)
                raise RuntimeError(f"Could not join the edit: {errors.read().strip()[-400:]}")
        ok = True
    finally:
        _remove(manifest)
        if not ok:
            _remove(output)
