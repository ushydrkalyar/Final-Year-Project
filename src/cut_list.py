"""The keep list: which ``(start, end)`` ranges of the original video survive the edit.

Pure functions, no I/O. All times are seconds on the ORIGINAL timeline.
Used by the pipeline (edge silences), timestamp corrections, and the timeline
that explains every removed range.
"""
import math

Range = tuple[float, float]
_EPS = 1e-3
MIN_SEGMENT_S = 0.05    # the renderer cannot encode less


def normalize(ranges, duration=None, min_len=0.0) -> list[Range]:
    """Clamp to ``[0, duration]``, sort, merge overlapping or touching ranges,
    then drop pieces shorter than ``min_len``."""
    clean = []
    for start, end in ranges:
        start = max(0.0, float(start))
        end = float(end) if duration is None else min(float(duration), float(end))
        if end - start > _EPS:
            clean.append((start, end))
    merged: list[Range] = []
    for start, end in sorted(clean):
        if merged and start <= merged[-1][1] + _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [(round(s, 3), round(e, 3)) for s, e in merged if e - s >= min_len]


def subtract(keep, start, end) -> list[Range]:
    """``keep`` with ``[start, end)`` removed."""
    out = []
    for s, e in keep:
        if e <= start or s >= end:
            out.append((s, e))
            continue
        if s < start:
            out.append((s, start))
        if e > end:
            out.append((end, e))
    return normalize(out)


def apply_correction(keep, start, end, correction, duration) -> list[Range]:
    """A user's timestamp correction applied to the keep list.

    ``KEEP`` puts ``[start, end)`` back into the edit; ``CUT`` takes it out.
    Raises ValueError for invalid input or a correction that empties the video.
    """
    correction = str(correction).upper()
    if correction not in ("CUT", "KEEP"):
        raise ValueError("correction must be 'CUT' or 'KEEP'.")
    if not (math.isfinite(start) and math.isfinite(end)):
        raise ValueError("Timestamps must be numbers of seconds.")
    if end <= start:
        raise ValueError("timestamp_end must be greater than timestamp_start.")
    if start >= duration or end <= 0:
        raise ValueError("That range is outside the video.")
    if correction == "KEEP":
        result = normalize(list(keep) + [(start, end)], duration)
    else:
        result = subtract(keep, start, end)
    result = normalize(result, duration, min_len=MIN_SEGMENT_S)
    if not result:
        raise ValueError("That correction would remove the whole video.")
    return result


def edge_cuts(leading_end, trailing_start, duration, min_len=0.05) -> list[Range]:
    """Silence before the first word and after the last one, as cut ranges."""
    cuts = []
    if leading_end and leading_end > min_len:
        cuts.append((0.0, float(leading_end)))
    if trailing_start is not None and duration - trailing_start > min_len:
        cuts.append((float(trailing_start), float(duration)))
    return cuts


def removed(keep, duration) -> list[Range]:
    """The gaps between kept ranges: everything the edit takes out."""
    gaps, cursor = [], 0.0
    for s, e in normalize(keep, duration):
        if s - cursor > _EPS:
            gaps.append((cursor, s))
        cursor = e
    if duration - cursor > _EPS:
        gaps.append((cursor, duration))
    return gaps


def _overlap(a: Range, b: Range) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def label_timeline(keep, duration, reasons) -> list[dict]:
    """Original-timeline segments, each removed gap labelled with why it went.

    ``reasons`` is a list of ``{"start", "end", "kind", "reason"}``. A gap's type
    is the reason it overlaps most (later entries win ties, so a user's own
    correction outranks the AI's); its text lists every overlapping reason.
    A gap with no recorded reason is a pause cut.
    """
    segments = []
    for s, e in normalize(keep, duration):
        segments.append({"start": s, "end": e, "type": "speech_kept",
                         "reason": "Speech kept", "in_edit": True})
    for gap in removed(keep, duration):
        hits = [(_overlap(gap, (r["start"], r["end"])), i, r) for i, r in enumerate(reasons)]
        hits = sorted((h for h in hits if h[0] > 0), key=lambda h: (h[0], h[1]), reverse=True)
        texts = list(dict.fromkeys(h[2]["reason"] for h in hits))  # largest overlap first, no repeats
        segments.append({"start": round(gap[0], 3), "end": round(gap[1], 3),
                         "type": hits[0][2]["kind"] if hits else "pause_cut",
                         "reason": " + ".join(texts) if texts else "Pause removed",
                         "in_edit": False})
    segments.sort(key=lambda seg: seg["start"])
    for seg in segments:
        seg["duration"] = round(seg["end"] - seg["start"], 3)
    return segments
