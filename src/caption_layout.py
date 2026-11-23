"""Where a caption track sits: moved as a block, and never off the frame.

The generators in subtitle_service lay every template out centred. This pass moves the result by the
user's offset and keeps it inside the picture, measured with the font files libass renders with. So
every template moves the same way (exactly as far as it was dragged), and the export can't clip.
A track that already fits and isn't moved comes back byte for byte.
Beside the track it says where each caption ended up, so a drag stops exactly where the export would.

Knows ASS, nothing about jobs or HTTP.
"""
import functools
import re
import unicodedata
from dataclasses import dataclass

from fontTools.ttLib import TTFont

import font_catalog

MARGIN = 0.015          # of the frame, at every side. live_captions.js clamps a drag to the same
HINTING = 1.02          # rendered text runs up to this much wider than its advances add up to
TOO_SMALL = 0.8         # rather than shrink a caption below this, give it another line
MAX_LINES = 3
UNKNOWN_GLYPH = 0.6     # em
WIDE_GLYPH = 1.0        # em: CJK and emoji

_TOKEN = re.compile(r"\{[^}]*\}|\\[Nn]|\\h|.", re.S)
_ANIMATION = re.compile(r"\\t\([^()]*(?:\([^()]*\)[^()]*)*\)")
_PLACE = re.compile(r"\\(pos|move)\(([^)]*)\)")


@dataclass
class _Line:
    width: float
    height: float
    spaces: list        # (index in the text, width before it)


@dataclass
class _Box:
    left: float
    top: float
    right: float
    bottom: float


@dataclass
class _Event:
    fields: list
    style: dict
    start: float
    end: float
    text: str
    box: _Box | None = None


# ---- fonts

@functools.lru_cache(maxsize=256)
def _face(family: str, bold: bool, italic: bool):
    """(advance in ems by code point, ems per unit of ASS font size) for what libass would pick."""
    paths = font_catalog.files(family) or font_catalog.files("Arial")
    best, least = None, None
    for path in paths:
        try:
            font = TTFont(path, lazy=True, fontNumber=0)
            weight = font["OS/2"].usWeightClass
            slanted = bool(font["OS/2"].fsSelection & 1)
        except Exception:       # a font that can't be read is one libass skips too
            continue
        distance = (slanted != italic) * 10000 + abs(weight - (700 if bold else 400))
        if least is None or distance < least:
            best, least = font, distance
    if best is None:
        return {}, 0.85
    unit = best["head"].unitsPerEm
    advances = {code: best["hmtx"][glyph][0] / unit for code, glyph in (best.getBestCmap() or {}).items()}
    os2 = best["OS/2"]
    tall = (os2.usWinAscent + os2.usWinDescent) or (best["hhea"].ascent - best["hhea"].descent)
    return advances, unit / tall        # libass sizes a font by ascent + descent, not the em


def _advance(char: str, advances: dict) -> float:
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0.0
    known = advances.get(ord(char))
    if known is not None:
        return known
    return WIDE_GLYPH if unicodedata.east_asian_width(char) in "WF" else UNKNOWN_GLYPH


# ---- reading a track

def _seconds(stamp: str) -> float:
    hours, minutes, seconds = stamp.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _track(ass: str):
    info, styles, events, columns, lines = {}, {}, {}, [], ass.split("\n")
    for at, line in enumerate(lines):
        key, colon, value = line.partition(":")
        if not colon:
            continue
        if key in ("PlayResX", "PlayResY", "WrapStyle"):
            info[key] = float(value)
        elif key == "Format" and "Fontname" in value:
            columns = [name.strip() for name in value.split(",")]
        elif key == "Style":
            style = dict(zip(columns, value.strip().split(",")))
            styles[style["Name"].strip()] = style
        elif key == "Dialogue":
            fields = line.split(",", 9)
            if len(fields) == 10:
                style = styles.get(fields[3].strip()) or next(iter(styles.values()), None)
                if style:
                    events[at] = _Event(fields, style, _seconds(fields[1]), _seconds(fields[2]), fields[9])
    return info, lines, events


def _tag(text: str, name: str, default: float) -> float:
    found = re.findall(rf"\\{name}(-?[\d.]+)", text)
    return float(found[-1]) if found else default


def _measure(text: str, style: dict, stretch: float) -> list[_Line]:
    """Each written line's size, in script units."""
    def fresh():
        return (style["Fontname"], float(style["Fontsize"]), int(float(style["Bold"])) != 0,
                int(float(style["Italic"])) != 0, float(style["Spacing"]), 1.0)
    font, size, bold, italic, spacing, grown = fresh()
    advances, em = _face(font, bold, italic)
    lines, width, height, spaces, at = [], 0.0, 0.0, [], 0
    for token in _TOKEN.findall(text):
        if token[0] == "{":
            if re.search(r"\\r(?![a-z])", token):
                font, size, bold, italic, spacing, grown = fresh()
            still = _ANIMATION.sub("", token)
            named = re.findall(r"\\fn([^\\}]+)", still)
            font = named[-1] if named else font
            size = _tag(still, "fs", size)
            bold = _tag(still, "b", 1 if bold else 0) > 0
            italic = _tag(still, "i", 1 if italic else 0) > 0
            spacing = _tag(still, "fsp", spacing)
            scales = [float(v) / 100 for v in re.findall(r"\\fscx([\d.]+)", token)]
            grown = max([1.0, *scales]) if scales else grown    # as big as its animation makes it
            advances, em = _face(font, bold, italic)
        elif token in ("\\N", "\\n"):
            lines.append(_Line(width, height or size, spaces))
            width, height, spaces = 0.0, 0.0, []
        else:
            char = " " if token == "\\h" else token
            if token == " ":
                spaces.append((at, width))
            width += (_advance(char, advances) * em * size + spacing) * grown * stretch
            height = max(height, size * grown)
        at += len(token)
    lines.append(_Line(width, height or size, spaces))
    return lines


def _extent(lines: list[_Line], wrap_at: float | None) -> tuple[float, float]:
    """The block's size once libass has wrapped it (``wrap_at`` None: it doesn't wrap)."""
    width = height = 0.0
    for line in lines:
        rows, start = [], 0.0
        if wrap_at and line.width > wrap_at:
            last = None
            for _, before in [*line.spaces, (0, line.width)]:
                if before - start > wrap_at and last is not None:
                    rows.append(last - start)
                    start, last = last, None
                if before > start:
                    last = before
        rows.append(line.width - start)
        width = max(width, *rows)
        height += line.height * len(rows)
    return width, height


# ---- fitting

def _resized(event: _Event, scale: float) -> str:
    size = float(event.style["Fontsize"])

    def tags(block: re.Match) -> str:
        inner = re.sub(r"\\fs([\d.]+)", lambda m: f"\\fs{float(m[1]) * scale:.1f}", block[0])
        return re.sub(r"\\r(?![a-z])", lambda m: f"\\r\\fs{size * scale:.1f}", inner)

    body = re.sub(r"\{[^}]*\}", tags, event.text)
    return f"{{\\fs{size * scale:.1f}}}{body}"


def _fit(event: _Event, info: dict, stretch: float, room: tuple[float, float]) -> None:
    """Break or shrink one event until it fits, and work out where it sits."""
    style, width = event.style, info["PlayResX"]
    margins = [float(event.fields[i]) or float(style[key]) for i, key in ((5, "MarginL"), (6, "MarginR"), (7, "MarginV"))]
    left, right, vertical = margins
    wraps = "\\q2" not in event.text and info.get("WrapStyle", 0) != 2
    wrap_at = width - left - right if wraps else None

    outline = _tag(_ANIMATION.sub("", event.text), "bord", float(style["Outline"]))
    sides = 2 * outline * stretch + float(style["Shadow"]) * stretch
    ends = 2 * outline + float(style["Shadow"])
    space = (max(1.0, room[0] - sides), max(1.0, room[1] - ends))

    def scale_for(lines):
        w, h = _extent(lines, wrap_at)
        return min(space[0] / max(w * HINTING, 1e-6), space[1] / max(h, 1e-6), 1.0)

    lines = _measure(event.text, style, stretch)
    while scale_for(lines) < TOO_SMALL and len(lines) < MAX_LINES:
        widest = max(lines, key=lambda line: line.width)
        if not widest.spaces or widest.width <= space[0]:
            break
        at, _ = min(widest.spaces, key=lambda space_at: abs(space_at[1] - widest.width / 2))
        event.text = f"{event.text[:at]}\\N{event.text[at + 1:]}"
        lines = _measure(event.text, style, stretch)

    scale = scale_for(lines)
    if scale < 1:
        event.text = _resized(event, scale)
        lines = _measure(event.text, style, stretch)
    w, h = _extent(lines, wrap_at)
    w *= HINTING

    still = _ANIMATION.sub("", event.text)
    align = int(_tag(still, "an", float(style["Alignment"])))
    column, row = (align - 1) % 3, (align - 1) // 3      # row 0 = bottom
    placed = _PLACE.search(event.text)
    if placed:
        numbers = [float(n) for n in placed[2].split(",")]
        x, y = numbers[2:4] if placed[1] == "move" else numbers[:2]
    else:
        x = (left, (left + width - right) / 2, width - right)[column]
        y = (info["PlayResY"] - vertical, info["PlayResY"] / 2, vertical)[row]
        event.text = f"{{\\an{align}\\pos({x:.1f},{y:.1f})}}{event.text}"
    # the anchor is the text's own; outline, box and shadow grow out around it
    shadow = float(style["Shadow"])
    event.box = _Box(x - w * column / 2 - outline * stretch, y - h * (2 - row) / 2 - outline,
                     x + w * (2 - column) / 2 + (outline + shadow) * stretch, y + h * row / 2 + outline + shadow)


def _shifted(text: str, dx: float, dy: float) -> str:
    def move(found: re.Match) -> str:
        numbers = found[2].split(",")
        for i in range(0, 4 if found[1] == "move" else 2, 2):
            numbers[i] = f"{float(numbers[i]) + dx:.1f}"
            numbers[i + 1] = f"{float(numbers[i + 1]) + dy:.1f}"
        return f"\\{found[1]}({','.join(numbers)})"
    return _PLACE.sub(move, text, count=1)


def _together(events: list[_Event]) -> list[list[_Event]]:
    """What is on screen at once moves as one, so a keyword and its small words keep their shape.
    So does one caption that is several events in a row (a highlight moving through its words)."""
    groups, until = [], -1.0
    for event in sorted(events, key=lambda e: e.start):
        if not groups or (event.start >= until - 1e-6 and _words(event) != _words(groups[-1][-1])):
            groups.append([])
            until = event.end
        groups[-1].append(event)
        until = max(until, event.end)
    return groups


def _words(event: _Event) -> str:
    return re.sub(r"\{[^}]*\}|\\[Nn]|\s+", "", event.text)


def _inside(low: float, high: float, limit: float) -> float:
    edge = limit * MARGIN
    if high - low >= limit - 2 * edge:
        return limit / 2 - (low + high) / 2
    return max(edge - low, min(0.0, limit - edge - high))


def arrange(ass: str, frame: tuple[int, int], dx: float = 0.0, dy: float = 0.0) -> tuple[str, list[dict]]:
    """``ass`` moved by (dx, dy), fractions of ``frame`` (its pixel size), and kept inside it.

    Also returns, for each caption: when it shows (s, e), its box (l, t, r, b) and how far it was
    held back from where it was asked to be (hx, hy), in fractions of the frame.
    """
    info, lines, events = _track(ass)
    if not events or "PlayResX" not in info or "PlayResY" not in info:
        return ass, []
    width, height = info["PlayResX"], info["PlayResY"]
    # glyphs scale with the frame's height, positions with its width
    stretch = (frame[1] / height) / (frame[0] / width)
    room = (width * (1 - 2 * MARGIN), height * (1 - 2 * MARGIN))
    written = {at: event.text for at, event in events.items()}
    for event in events.values():
        _fit(event, info, stretch, room)

    moved, placed = False, []
    for group in _together(list(events.values())):
        left, right = min(e.box.left for e in group), max(e.box.right for e in group)
        top, bottom = min(e.box.top for e in group), max(e.box.bottom for e in group)
        x, y = dx * width, dy * height
        held_x = _inside(left + x, right + x, width)
        held_y = _inside(top + y, bottom + y, height)
        x, y = x + held_x, y + held_y
        placed.append({
            "s": group[0].start, "e": max(e.end for e in group),
            "l": round((left + x) / width, 4), "r": round((right + x) / width, 4),
            "t": round((top + y) / height, 4), "b": round((bottom + y) / height, 4),
            "hx": round(held_x / width, 4), "hy": round(held_y / height, 4),
        })
        if abs(x) < 0.05 and abs(y) < 0.05:
            continue
        moved = True
        for event in group:
            event.text = _shifted(event.text, x, y)

    if not moved and all(_unplaced(event.text) == _unplaced(written[at]) for at, event in events.items()):
        return ass, placed      # only positions were spelled out: nothing changed
    for at, event in events.items():
        lines[at] = ",".join([*event.fields[:9], event.text])
    return "\n".join(lines), placed


def _unplaced(text: str) -> str:
    return re.sub(r"^\{\\an\d\\pos\([^)]*\)\}", "", text)
