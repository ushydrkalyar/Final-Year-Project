"""Join finished clip edits, with a transition at each boundary.

Every clip is already cut on one canvas, so only the footage under a transition is encoded.
The rest of each clip is its own pieces, copied.
"""
from dataclasses import dataclass

import numpy as np

import segment_render as sr

# UI value -> ffmpeg xfade effect; these overlap the two clips
_XFADE = {
    "crossfade": "fade",
    "wipe_left": "wipeleft", "wipe_right": "wiperight", "wipe_up": "wipeup", "wipe_down": "wipedown",
    "slide_left": "slideleft", "slide_right": "slideright",
    "pixelize": "pixelize", "radial": "radial", "circle_open": "circleopen",
}
# these fade out then in, without overlapping
_FADE = {"fade": "black", "fade_white": "white"}
# what scene_detector calls the transitions it finds
_DETECTED = {"dissolve": "crossfade", "cross": "crossfade", "wipe": "wipe_left"}

TRANSITIONS = frozenset({"auto", "hard_cut", *_XFADE, *_FADE})
MAX_TRANSITION_S = 3.0


@dataclass(frozen=True)
class Joined:
    path: str
    pieces: list        # in playing order
    made: list          # the pieces this join owns: what lies under the transitions
    starts: list        # seconds at which each clip starts
    frames: int


def resolve_transition_type(style_profile: dict, fallback: str = "hard_cut") -> str:
    """The transition a reference's profile asks for, as one the UI knows."""
    found = str(style_profile.get("cv_transition_type", fallback)).lower()
    found = _DETECTED.get(found, found)
    return found if found in TRANSITIONS and found != "auto" else fallback


def _overlaps(clips, transitions, seconds, canvas) -> list[int]:
    """Frames under each boundary's transition; it can't use more than half of either clip."""
    wanted = canvas.frames(seconds)
    sizes = []
    for left, right, kind in zip(clips, clips[1:], transitions):
        size = min(wanted, left.frames // 2, right.frames // 2) if kind in _XFADE or kind in _FADE else 0
        sizes.append(size if size >= 2 else 0)
    return sizes


def _input(clip, first: int, canvas) -> list[str]:
    return ["-ss", f"{max(0.0, float((first - sr.SEEK_BEFORE) / canvas.fps)):.6f}", "-i", clip.path]


def _steady(canvas, frames: int) -> str:
    return f"fps={canvas.fps}:start_time=0,tpad=stop_mode=clone:stop=-1,trim=end_frame={frames}"


def _blend(left, right, kind: str, size: int, canvas, output: str) -> None:
    graph = (f"[0:v]{_steady(canvas, size)}[a];[1:v]{_steady(canvas, size)}[b];"
             f"[a][b]xfade=transition={_XFADE[kind]}:duration={canvas.seconds(size):.6f}:offset=0,format=yuv420p[v]")
    sr.run(["ffmpeg", "-y", "-v", "error", *_input(left, left.frames - size, canvas), *_input(right, 0, canvas),
            "-filter_complex", graph, "-map", "[v]", "-frames:v", str(size), *sr.video_codec(canvas), output],
           "render a transition")


def _faded(clip, first: int, size: int, direction: str, colour: str, canvas, output: str) -> None:
    look = f"{_steady(canvas, size)},fade=t={direction}:s=0:n={size}:color={colour}"
    sr.run(["ffmpeg", "-y", "-v", "error", *_input(clip, first, canvas), "-map", "0:v:0",
            "-vf", look, "-frames:v", str(size), *sr.video_codec(canvas), output], "render a fade")


def _video(clips, transitions, sizes, canvas) -> list[tuple]:
    """Every piece of the joined video as (key, frames, encode)."""
    wanted = []
    identity = [[piece.key for piece in clip.pieces] for clip in clips]
    for i, clip in enumerate(clips):
        head = sizes[i - 1] if i else 0
        body_end = clip.frames - (sizes[i] if i < len(sizes) else 0)
        if head and transitions[i - 1] in _FADE:
            colour = _FADE[transitions[i - 1]]
            wanted.append((sr.content_key("fade in", identity[i], head, colour), head,
                           lambda out, c=clip, n=head, col=colour: _faded(c, 0, n, "in", col, canvas, out)))
        at = 0
        for piece in clip.pieces:
            first, end = max(at, head), min(at + piece.frames, body_end)
            if end > first:
                if end - first == piece.frames:
                    wanted.append((piece.key, piece.frames, None))
                else:       # the part of it outside the transition
                    wanted.append((sr.content_key("part", piece.key, first - at, end - at), end - first,
                                   lambda out, c=clip, a=first, b=end: sr.encode_range(c.path, a, b, canvas, [], out)))
            at += piece.frames
        if i < len(sizes) and sizes[i]:
            kind, size = transitions[i], sizes[i]
            if kind in _XFADE:
                wanted.append((sr.content_key("blend", identity[i], identity[i + 1], kind, size), size,
                               lambda out, a=clip, b=clips[i + 1], k=kind, n=size: _blend(a, b, k, n, canvas, out)))
            else:
                wanted.append((sr.content_key("fade out", identity[i], size, _FADE[kind]), size,
                               lambda out, c=clip, n=size, col=_FADE[kind]: _faded(c, c.frames - n, n, "out", col, canvas, out)))
    return wanted


def _sound(clips, transitions, sizes, canvas, prefix):
    """The clips' sound, crossfaded or faded exactly where the picture is."""
    carry = None        # the previous clip's samples under an overlap
    for i, clip in enumerate(clips):
        with sr.Audio(clip.path, prefix) as audio:
            samples = audio.read(0, canvas.samples(clip.frames))
        if i and sizes[i - 1]:
            count = canvas.samples(sizes[i - 1])
            rise = np.linspace(0, 1, count, dtype=np.float32)[:, None]
            samples[:count] *= rise
            if carry is not None:
                samples[:count] += carry * (1 - rise)
        carry = None
        if i < len(sizes) and sizes[i]:
            count = canvas.samples(sizes[i])
            if transitions[i] in _XFADE:
                samples, carry = samples[:-count], samples[-count:].copy()
            else:
                samples[-count:] *= np.linspace(1, 0, count, dtype=np.float32)[:, None]
        yield samples


def join_clips(clips: list, transitions: list[str], seconds: float, prefix: str, output: str,
               known=(), on_progress=lambda done, total: None) -> Joined:
    """``clips`` are edit_render.Cut's on one canvas; ``transitions`` has one entry per boundary."""
    canvas = clips[0].canvas
    if any(clip.canvas != canvas for clip in clips):
        raise RuntimeError("These clips were edited at different sizes.")
    sizes = _overlaps(clips, transitions, seconds, canvas)
    shared = [piece for clip in clips for piece in clip.pieces]
    shared_paths = {piece.path for piece in shared}
    pieces = sr.build_pieces(_video(clips, transitions, sizes, canvas), prefix, [*known, *shared], on_progress)
    sr.join(pieces, canvas, _sound(clips, transitions, sizes, canvas, prefix), prefix, output)
    starts, at = [], 0
    for i, clip in enumerate(clips):
        starts.append(canvas.seconds(at))
        at += clip.frames
        if i < len(sizes) and transitions[i] in _XFADE:
            at -= sizes[i]
    return Joined(output, pieces, [piece for piece in pieces if piece.path not in shared_paths], starts, at)
