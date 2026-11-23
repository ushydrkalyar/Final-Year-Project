"""Timed cut requests typed as comments (Step 6: Feedback & Refinement), read with rules only.

    keep the pause at 0:12  |  cut 0:05 to 0:08  |  remove the silence at the start

This says what was asked. Studio resolves the times against the timeline it is showing.
"""
import re

_CUT = re.compile(r"^(?:cut|remove|delete|trim|take out|get rid of)\b")
_KEEP = re.compile(r"^(?:keep|restore|put back|bring back|don'?t (?:cut|remove)|do not (?:cut|remove))\b")
_POLITE = re.compile(r"^(?:(?:please|can you|could you|also)\s+)+")
# those are the caption parser's
_ABOUT_CAPTIONS = re.compile(r"\b(?:captions?|lines?|words?|spelling)\b|[\"“”‘]|(?:^|\s)'[^']+'")

_CLOCK = r"\d{1,3}:\d{2}(?::\d{2})?(?:\.\d+)?"
_UNIT = r"\s*(?:seconds?|secs?|s)\b"
_TIME = rf"({_CLOCK}|\d+(?:\.\d+)?(?:{_UNIT})?)"
_RANGE = re.compile(rf"(?:\bfrom\s+|\bbetween\s+)?{_TIME}\s*(?:to|until|till|through|and|-|–|—)\s*{_TIME}")
_POINT = re.compile(rf"\b(?:at|around|near)\s+({_CLOCK}|\d+(?:\.\d+)?{_UNIT})")
_THING = re.compile(r"\b(?:pause|silence|gap|dead air|cut|filler|section|part|breath)\b")
_EDGE = re.compile(r"\b(?:silence|pause|gap|dead air)\b.*\b(?:at|from|in) the (start|beginning|end)\b")


def seconds(token: str) -> float:
    number = re.sub(r"[^\d.:]", "", token)
    total = 0.0
    for part in number.split(":"):
        total = total * 60 + float(part)
    return total


def parse_cut(comment: str) -> dict | None:
    """The request, or None when the comment isn't about a cut."""
    text = re.sub(r"\s+", " ", (comment or "").strip().lower().replace("’", "'"))
    text = _POLITE.sub("", text)
    action = "KEEP" if _KEEP.search(text) else "CUT" if _CUT.search(text) else None
    if action is None or _ABOUT_CAPTIONS.search(text):
        return None

    found = _RANGE.search(text)
    if found:
        start, end = seconds(found.group(1)), seconds(found.group(2))
        if end <= start:
            return {"ok": False, "message": "The end time has to be after the start time.", "candidates": []}
        return {"ok": True, "action": action, "kind": "range", "start": start, "end": end}

    edge = _EDGE.search(text)
    if edge:
        return {"ok": True, "action": action, "kind": "edge", "edge": "end" if edge.group(1) == "end" else "start"}

    point = _POINT.search(text)
    if point and _THING.search(text):
        return {"ok": True, "action": action, "kind": "point", "at": seconds(point.group(1))}
    return None
