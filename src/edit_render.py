"""Render an edit: the caption-free cut, and the captions drawn on it.

Every edit, change and correction, the live caption track and the export go
through these functions, so the caption rules and render steps can't drift
apart between them.
"""
import dataclasses as dc
import math
import os
import re
import tempfile

import caption_layout
import media_probe
import reframe
import segment_render
import subtitle_service as ss
from color_grader import grade_filter
from render_engine import SUBTITLE_PRESETS, SubtitleStyle


def _int(f, key, default):
    try:
        return int(float(f.get(key, default)))
    except (TypeError, ValueError, OverflowError):
        return default


def _float(f, key, default):
    try:
        value = float(f.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


# These are written into the ASS file as they are, so only well-formed values may pass
_COLOUR = re.compile(r"&H[0-9A-Fa-f]{8}")
_FONT = re.compile(r"[\w .-]{1,64}")


def _matching(f, key, pattern, default):
    value = str(f.get(key, default))
    return value if pattern.fullmatch(value) else default


def build_style(f) -> tuple[SubtitleStyle, str]:
    """Caption style from the form fields every caption endpoint accepts.

    ``f`` is any mapping of field name -> value (strings or numbers).
    Returns ``(style, preset_name)``.
    """
    preset = f.get("subtitle_preset", "custom")
    if preset in SUBTITLE_PRESETS:
        # "animated" maps to None: its look lives in the ASS file, the base style is only a fallback
        style = SUBTITLE_PRESETS[preset] or SubtitleStyle()
    else:
        style = SubtitleStyle(
            font_name=_matching(f, "subtitle_font", _FONT, "Arial"),
            font_size=max(6, min(120, _int(f, "subtitle_size", 18))),
            primary_colour=_matching(f, "subtitle_colour", _COLOUR, "&H00FFFFFF"),
            alignment=_int(f, "subtitle_position", 2),
            margin_v=_int(f, "subtitle_margin_v", 30),
            bold=bool(_int(f, "sub_bold", 0)),
            italic=bool(_int(f, "sub_italic", 0)),
        )
    emphasis = bool(_int(f, "emphasis", 0))
    emph_look = f.get("emph_look", "classic")
    wpc = max(0, min(12, _int(f, "words_per_caption", 0)))
    align = f.get("sub_align")
    style = dc.replace(
        style,
        words_per_caption=(wpc or style.words_per_caption),
        caption_lines=1 if _int(f, "caption_lines", 2) == 1 else 2,
        emphasis=emphasis,
        emph_font=_matching(f, "emph_font", _FONT, "Arial Black"),
        emph_colour=_matching(f, "emph_colour", _COLOUR, "&H0030EAB4"),
        emph_scale=max(1.0, min(6.0, _float(f, "emph_scale", 2.0))),
        emph_glow=bool(_int(f, "emph_glow", 1)),
        emph_every=max(1, _int(f, "emph_every", 2)),
        emph_italic=bool(_int(f, "emph_italic", 0)),
        emph_look=(emph_look if emph_look in ("lime", "box", "soft") else "classic"),
        layout=("soft" if emph_look == "soft" else "karaoke" if emph_look == "box"
                else "scatter" if emphasis else "line"),
        pos_y=max(0.02, min(0.98, _float(f, "pos_y", 0.53))),
        pos_x=max(0.02, min(0.98, _float(f, "pos_x", 0.5))),
        offset_y=max(-1.0, min(1.0, _float(f, "offset_y", 0.0))),
        exact_pos=(preset == "custom"),
        text_align=(align if align in ("l", "c", "r") else "c"),
        letter_spacing=max(-2.0, min(20.0, _float(f, "letter_spacing", 0.0))),
        line_spacing=max(0.5, min(2.5, _float(f, "line_spacing", 1.0))),
    )
    return style, preset


def write_captions(transcript, keep, style, preset, srt_path, ass_path, video_path,
                   real_durations=None, parts=None, hidden=()):
    """Write the caption files for ``keep`` on the edited timeline.

    Writes ``srt_path`` (the caption list) and ``ass_path`` (the look), and
    returns where each caption was placed (see caption_layout.arrange). Every style
    renders from that one ASS file, both in the browser preview and in the burned
    export, so the two always match.
    ``video_path`` is only probed for its frame size.

    ``parts``: where each clip of a joined edit starts; no caption runs across two clips.
    ``hidden``: the clips (by index) that get no captions in the picture. They stay in the list.
    Every template is generated centred, then moved and kept in frame by caption_layout.
    """
    centred, down = dc.replace(style, pos_x=0.5, offset_y=0.0), style.offset_y
    if preset != "animated" and style.layout == "scatter":      # its own clamps would fight the move
        centred, down = dc.replace(centred, pos_y=0.5), down + style.pos_y - 0.5
    spans = _clip_spans(parts)
    if len(spans) == 1:
        _generate(transcript, keep, centred, preset, srt_path, ass_path, video_path, real_durations)
    else:
        _generate_by_clip(transcript, keep, centred, preset, srt_path, ass_path, video_path, spans)
    with open(ass_path, encoding="utf-8") as f:
        ass = f.read()
    if hidden:
        ass = _without_clips(ass, spans, set(hidden))
    ass, placed = caption_layout.arrange(ass, media_probe.frame_size(video_path) if video_path else (1080, 1920),
                                 style.pos_x - 0.5, down)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass)
    return placed


def _clip_spans(parts) -> list[tuple[float, float]]:
    starts = [0.0, *(parts or [])[1:]]
    return list(zip(starts, [*starts[1:], math.inf]))


def _cue_seconds(stamp: str) -> float:
    h, m, s = stamp.strip().replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _generate_by_clip(transcript, keep, style, preset, srt_path, ass_path, video_path, spans):
    """Each clip's words grouped on their own, in one track. ``transcript`` is on the joined timeline."""
    header, events, cues = "", [], []
    for index, (first, end) in enumerate(spans):
        segments = []
        for segment in transcript.get("segments", []):
            words = [w for w in segment.get("words", []) if first <= (w["start"] + w["end"]) / 2 < end]
            if words:
                segments.append({**segment, "start": words[0]["start"], "end": words[-1]["end"], "words": words,
                                 "text": " ".join(w["word"].strip() for w in words)})
        if not segments:
            continue
        srt, ass = f"{srt_path}.{index}.srt", f"{ass_path}.{index}.ass"
        try:
            _generate({**transcript, "segments": segments}, keep, style, preset, srt, ass, video_path)
            with open(ass, encoding="utf-8") as f:
                track = f.read()
            if not header:
                header = track.split("\nDialogue:", 1)[0].rstrip("\n") + "\n"
            for line in track.split("\n"):
                if line.startswith("Dialogue:"):
                    fields = line.split(",", 9)
                    if _cue_seconds(fields[2]) > end:       # gone before the next clip
                        fields[2] = ss._ass_time(end)
                    events.append(",".join(fields))
            with open(srt, encoding="utf-8") as f:
                for block in re.split(r"\n\s*\n", f.read().strip()):
                    if "-->" in block:
                        cues.append(block.split("\n", 1)[1])
        finally:
            for path in (srt, ass):
                if os.path.exists(path):
                    os.remove(path)
    if not events:
        _generate({**transcript, "segments": []}, keep, style, preset, srt_path, ass_path, video_path)
        return
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("".join(f"{number}\n{cue}\n\n" for number, cue in enumerate(cues, 1)))


def _without_clips(ass: str, spans, hidden: set) -> str:
    def shown(line: str) -> bool:
        if not line.startswith("Dialogue:"):
            return True
        fields = line.split(",", 3)
        middle = (_cue_seconds(fields[1]) + _cue_seconds(fields[2])) / 2
        return not any(first <= middle < end for i, (first, end) in enumerate(spans) if i in hidden)
    return "\n".join(line for line in ass.split("\n") if shown(line))


def _generate(transcript, keep, style, preset, srt_path, ass_path, video_path, real_durations=None):
    wpc = style.words_per_caption or 0
    common = dict(real_durations=real_durations)
    ss.create_remapped_srt(transcript, keep, srt_path, words_per_caption=wpc,
                           caption_lines=style.caption_lines, style=style, **common)
    if preset == "animated":
        ss.create_remapped_ass(transcript, keep, ass_path, chunk_size=wpc,
                               letter_spacing=style.letter_spacing, pos_x=style.pos_x, **common)
    elif style.layout == "soft":
        ss.create_remapped_soft_ass(transcript, keep, ass_path, words_per_caption=wpc or 5,
                                    style=style, video_path=video_path, **common)
    elif style.layout == "karaoke":
        ss.create_remapped_box_ass(transcript, keep, ass_path, words_per_caption=wpc or 5,
                                   style=style, video_path=video_path, **common)
    elif style.layout == "scatter":
        ss.create_remapped_pop_ass(transcript, keep, ass_path, words_per_caption=wpc or 3,
                                   style=style, video_path=video_path, **common)
    elif style.exact_pos:
        ss.create_remapped_custom_ass(srt_path, ass_path, style, video_path)
    else:
        ss.create_remapped_line_ass(srt_path, ass_path, style, video_path)
    return ass_path


def transcript_from_captions(captions, respect=False) -> dict:
    """A transcript built from the user's edited caption list (edited timeline).

    Each caption's words are spread across its [start, end] by word length, so
    word-level layouts still animate sensibly. ``respect`` keeps every caption
    as one unit instead of re-grouping its words.
    """
    segments = []
    for c in sorted((c for c in captions if str(c.get('t', '')).strip()), key=lambda c: float(c['s'])):
        s0, e0 = float(c['s']), max(float(c['e']), float(c['s']) + 0.15)
        words = str(c['t']).split()
        weights = [max(2, len(w)) for w in words]
        total, at, wl = float(sum(weights)), s0, []
        for w, wt in zip(words, weights):
            d = (e0 - s0) * wt / total
            wl.append({"word": w, "start": round(at, 3), "end": round(at + d, 3)})
            at += d
        segments.append({"start": s0, "end": e0, "text": " ".join(words), "words": wl})
    return {"segments": segments, "respect_segments": bool(respect)}


def edited_transcript(transcript, keep, real_durations=None, offset=0.0) -> dict:
    """``transcript`` with word times moved onto the edited timeline (+ ``offset``).

    Words inside cut ranges are dropped; the mapping matches the caption
    generators (including real per-segment encoded durations). Used to join
    several clips' transcripts into one for a combined multi-clip video.
    """
    def to_edited(t):
        at = 0.0
        for i, (a, b) in enumerate(keep):
            if a <= t <= b:
                return offset + at + (t - a)
            if t < a:
                return None
            at += ss._seg_duration(i, a, b, real_durations)
        return None

    segments = []
    for seg in transcript.get("segments", []):
        words = []
        for w in seg.get("words", []):
            s0, e0 = to_edited(float(w["start"])), to_edited(float(w["end"]))
            if s0 is not None and e0 is not None:
                words.append({**w, "start": round(s0, 3), "end": round(max(e0, s0 + 0.05), 3)})
        if words:
            segments.append({"start": words[0]["start"], "end": words[-1]["end"],
                             "text": " ".join(w["word"].strip() for w in words), "words": words})
    return {"segments": segments, "language": transcript.get("language")}


def caption_track(transcript, keep, style, preset, video_path, real_durations=None, parts=None, hidden=()) -> dict:
    """The caption files for a look, as text, and where they sit: ``{"ass", "srt", "boxes"}``.

    Pure Python (no ffmpeg), fast enough to call on every style change for the
    live browser preview; the export burns exactly this ASS.
    """
    with tempfile.TemporaryDirectory(prefix="cap_") as d:
        srt, ass = os.path.join(d, "c.srt"), os.path.join(d, "c.ass")
        boxes = write_captions(transcript, keep, style, preset, srt, ass, video_path, real_durations, parts, hidden)
        with open(ass, encoding="utf-8") as fa, open(srt, encoding="utf-8") as fs:
            return {"ass": fa.read(), "srt": fs.read(), "boxes": boxes}


_TRANSPOSE = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}


def rotate(src, degrees, dst) -> str:
    """Rotate a video clockwise by 90/180/270 degrees (sideways phone footage).

    Returns ``dst``, or ``src`` unchanged for 0 / unsupported angles. Done once on
    the upload, so transcription, thumbnails, framing and cuts all see it upright.
    """
    vf = _TRANSPOSE.get(int(degrees) % 360)
    if not vf:
        return src
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf, "-metadata:s:v", "rotate=0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "copy", dst], check=True)
    return dst


def _no_progress(label, pct):
    pass


@dc.dataclass(frozen=True)
class Cut:
    """A caption-free edit of one upload, and what a change to it can reuse."""
    path: str
    source: str
    canvas: segment_render.Canvas
    pieces: list
    real_durations: list        # seconds on screen of each kept range; 0 for one under a frame
    frame: dict | None = None
    plan: object = None         # the reframe plan made for (source, frame)

    @property
    def frames(self) -> int:
        return sum(piece.frames for piece in self.pieces)


def _frame_plan(input_path, frame, previous, on_progress):
    if not frame:
        return None
    if previous and previous.source == input_path and previous.frame == frame:
        return previous.plan    # the same footage and frame: following the speaker again would match
    on_progress("Framing the video", 73)
    try:
        plan = reframe.plan_reframe(
            input_path, frame,
            progress_cb=lambda f: on_progress("Following the speaker", 73 + int(f * 2)))
    except Exception as exc:
        print(f"   ⚠️  Speaker tracking failed ({exc}) — using a centred frame instead")
        try:
            plan = reframe.plan_reframe(input_path, dict(frame, mode="custom", cx=0.5, cy=0.5, zoom=1.0))
        except Exception as exc2:
            print(f"   ⚠️  Reframing skipped: {exc2}")
            return None
    if plan:
        print(f"   🎞️  Reframe: {plan.describe()}")
    return plan


def natural_canvas(input_path, frame=None) -> segment_render.Canvas:
    """The size and rate an upload is edited at when nothing else decides it."""
    found = media_probe.info(input_path)
    if not found or not found.width or not found.height:
        raise RuntimeError("Couldn't read this video.")
    size = found.display_size
    if frame and (frame["mode"] == "custom" or reframe.source_kind(*size) != reframe.NATURAL[frame["fmt"]]):
        size = reframe.OUT_SIZE[frame["fmt"]]
    width, height = size
    return segment_render.Canvas(segment_render.even(width), segment_render.even(height),
                                 segment_render.frame_rate(found.rate))


def cut(input_path, keep, prefix, frame=None, color_grade="none", grade_params=None,
        on_progress=_no_progress, previous: Cut | None = None, canvas=None) -> Cut:
    """Pass 1: framed, graded, caption-free video of the kept ranges.

    Grading here (before captions) keeps caption colours exact. With ``previous``, only the
    pieces whose content changed are encoded.
    """
    if not keep:
        raise RuntimeError("No segments to render — nothing was produced.")
    canvas = canvas or natural_canvas(input_path, frame)
    plan = _frame_plan(input_path, frame, previous, on_progress)
    look = grade_filter(color_grade, grade_params)
    ranges = segment_render.frame_ranges(keep, canvas)

    def piece(first, end):
        framing = plan.filter_for_clip(canvas.seconds(first), canvas.seconds(end)) if plan else None
        key = segment_render.content_key(input_path, first, end, canvas, framing, look)
        return key, end - first, lambda out: segment_render.encode_range(
            input_path, first, end, canvas, [framing, look], out)

    wanted = [piece(a, b) for kept in ranges for a, b in segment_render.split_range(*kept, canvas)]
    if not wanted:
        raise RuntimeError("Every kept segment is too short to encode.")
    on_progress("Encoding changed segments" if previous else "Encoding segments", 75)
    pieces = segment_render.build_pieces(
        wanted, prefix, previous.pieces if previous else [],
        lambda done, total: on_progress(f"Encoding segments ({done}/{total})", 75 + int(7 * done / total)))
    output = f"_intermediate_{prefix}_edit.mp4"
    with segment_render.Audio(input_path, prefix) as sound:
        segment_render.join(pieces, canvas, segment_render.kept_audio(sound, ranges, canvas), prefix, output)
    return Cut(output, input_path, canvas, pieces, [canvas.seconds(b - a) for a, b in ranges], frame, plan)
