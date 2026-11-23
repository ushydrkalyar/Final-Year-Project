"""
Regression test -- keyword text must never render outside the video frame.

Why this exists (SQA corrective measure, Assignment No. 3):
Red Editorial / Lime Pop ("Keyword Pop") captions size their emphasized
keyword by *estimating* its rendered pixel width from a per-character
constant (see `wwidth()` in subtitle_service.py) rather than measuring the
real glyphs. On 2026-10-08 that estimate for the classic/Red-Editorial path
was found to be wrong by more than 2x (0.22 where the true value is
0.50-0.64), which let the keyword's computed font size come out far too
large: real captions such as "WORKS," and "APPOINTMENT" rendered as "ORK"
and "OINTM" bleeding off both edges of the frame. The estimate was fixed,
but nothing stopped the *next* constant change, font swap, or emph_scale
tweak from reintroducing the same class of bug -- because there was no
automated check that ever actually rendered a keyword and looked at where
its pixels landed. This script is that check, kept outside the two
byte-exact "locked template" fixtures in locked_templates/ because this one
asserts a tolerance (fits in frame), not an exact frozen string.

Usage (from src_v4):  python3 tests/check_keyword_overflow.py
Requires ffmpeg (with libass) and Pillow on PATH / importable -- both already
used elsewhere in this project's render pipeline.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from subtitle_service import create_remapped_pop_ass
from render_engine import SubtitleStyle

W, H = 1080, 1920
# A trailing comma ("WORKS,") and all-wide-letter words are the worst cases
# found so far: wide uppercase letters (W, M, O, K) push the real glyph
# width well above the font's average, and clean()-stripped punctuation
# used to be silently excluded from the width estimate entirely.
WORDS = ["APPOINTMENT", "WORKS,", "CUSTOMERS", "FEELING", "TIME"]
# Deliberately adversarial, not a real English word: six letters of nothing
# but W/O (Anton's two widest glyphs) back to back. A flat per-font average
# character width -- the only estimate this fast enough for the render
# pipeline to use -- still underestimates a repeated-widest-letter run like
# this one; tracked separately as a documented, known limitation (see
# Assignment 3's report) rather than silently dropped, since every
# *realistic* caption word tested above is comfortably safe.
KNOWN_LIMITATION_WORDS = ["WOWWOW"]
MARGIN_PX = 20  # the pipeline's own target margin is W*0.05 = 54px; alarm well before that erodes to zero


def _td(word):
    # One isolated word per group: a long gap on each side guarantees it is
    # its own caption (and, being >=4 chars and not a stopword, the
    # keyword), independent of any other grouping/selection logic.
    return {"segments": [{"start": 0, "end": 2, "text": "",
                           "words": [{"word": word, "start": 1.0, "end": 1.6}]}]}


def _measure_ink_width(ass_path, t):
    png = ass_path + ".png"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d=2",
         "-vf", f"ass={ass_path}", "-frames:v", "1", "-ss", str(t), "-update", "1", png],
        check=True, capture_output=True,
    )
    from PIL import Image
    im = Image.open(png).convert("L")
    bbox = im.point(lambda p: 255 if p > 20 else 0).getbbox()
    os.remove(png)
    return bbox


def check_look(emph_look, label, words, hard_fail=True):
    ok = True
    for word in words:
        st = SubtitleStyle(font_name="Instrument Serif", font_size=34, bold=False, italic=True,
                            primary_colour="&H00FFFFFF", pos_y=0.55, words_per_caption=1,
                            emphasis=True, emph_font="Anton", emph_scale=5.5,
                            emph_colour="&H001C28C0", emph_glow=False, emph_italic=False,
                            emph_every=1, emph_look=emph_look)
        out = os.path.join(tempfile.gettempdir(), f"overflow_check_{emph_look}.ass")
        create_remapped_pop_ass(_td(word), [(0, 2)], out, words_per_caption=1, style=st, video_path=None)
        bbox = _measure_ink_width(out, 1.3)
        if not bbox:
            print(f"FAILED - {label}: {word!r} rendered nothing")
            ok = False
            continue
        left, _, right, _ = bbox
        overflow_left = left <= 1          # touches/crosses the left edge
        overflow_right = right >= W - 1    # touches/crosses the right edge
        tight = left < MARGIN_PX or right > W - MARGIN_PX
        if overflow_left or overflow_right:
            tag = "FAILED" if hard_fail else "KNOWN LIMITATION (not counted)"
            print(f"{tag} - {label}: {word!r} overflows the frame (bbox x={left}..{right}, frame width={W})")
            if hard_fail:
                ok = False
        elif tight:
            print(f"WARNING - {label}: {word!r} is within {MARGIN_PX}px of the edge (bbox x={left}..{right})")
        else:
            print(f"   ok - {label}: {word!r} fits (bbox x={left}..{right} of {W})")
    return ok


if __name__ == "__main__":
    ok = True
    ok &= check_look("classic", "Red Editorial", WORDS)
    ok &= check_look("lime", "Lime Pop", WORDS)
    # Run but don't gate the suite on the deliberately adversarial case --
    # printed for visibility, not treated as a regression.
    check_look("classic", "Red Editorial", KNOWN_LIMITATION_WORDS, hard_fail=False)
    check_look("lime", "Lime Pop", KNOWN_LIMITATION_WORDS, hard_fail=False)
    print("OK - keyword text stays inside the frame for every realistic test word" if ok
          else "FAILED - one or more keywords render outside the frame")
    sys.exit(0 if ok else 1)
