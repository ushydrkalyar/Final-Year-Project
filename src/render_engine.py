import burned_captions
from decision_engine import compress_target
import subprocess
import os
import shutil
import pandas as pd
from dataclasses import dataclass





@dataclass
class SubtitleStyle:
    """
    Controls the visual appearance of burned-in subtitles.

    Colours use FFmpeg's &HAABBGGRR hex format (note: BGR not RGB).
    AA = alpha (00=opaque, FF=transparent)

    Alignment follows the numpad layout:
      7 8 9   (top row)
      4 5 6   (middle row)
      1 2 3   (bottom row)
    Default 2 = bottom-center, which is standard subtitle position.

    MarginV = vertical margin in pixels from the edge set by alignment.
    """
    font_name:       str   = "Arial"
    font_size:       int   = 28
    primary_colour:  str   = "&H00FFFFFF"   # white text
    outline_colour:  str   = "&H00000000"   # black outline
    back_colour:     str   = "&H80000000"   # semi-transparent black box
    bold:            bool  = False
    italic:          bool  = False
    outline:         int   = 2              # outline thickness in pixels
    shadow:          int   = 1              # drop shadow depth in pixels
    alignment:       int   = 2              # 2 = bottom-center
    margin_v:        int   = 30             # pixels from bottom edge
    margin_l:        int   = 20            # left margin
    margin_r:        int   = 20            # right margin
    border_style:    int   = 1              # 1=outline+shadow, 3=opaque box
    words_per_caption: int = 0              # 0 = automatic (Whisper phrases)
    caption_lines:   int   = 2              # 1 or 2 lines per caption
    emphasis:        bool  = False          # pop one keyword per caption (big, caps, neon)
    emph_colour:     str   = "&H0030EAB4"   # lime #B4EA30 (BGR)
    emph_font:       str   = "Arial Black"
    emph_scale:      float = 2.0            # keyword size relative to normal text
    emph_glow:       bool  = True
    emph_italic:     bool  = False
    emph_look:       str   = 'classic'   # 'lime' = Lime Pop look, 'classic' = original look
    emph_every:      int   = 2              # keyword in every Nth caption (1 = all)
    layout:          str   = "line"         # "line" or "scatter" (words placed around the keyword)
    pos_y:           float = 0.53           # vertical centre (0-1) for scatter layout
    pos_x:           float = 0.5            # horizontal centre (0-1) of the caption block; 0.5 = centred (the default look)
    exact_pos:       bool  = False          # True: place plain captions exactly at (pos_x, pos_y) as set in the Studio
    text_align:      str   = 'c'            # 'l' / 'c' / 'r' alignment of the caption block (exact_pos only)
    letter_spacing:  float = 0.0            # extra pixels between characters (ASS "Spacing"); 0 = font default
    line_spacing:    float = 1.0            # multiplier on the gap between stacked lines; 1.0 = current/default gap
    offset_y:        float = 0.0            # how far a fixed template was dragged up or down (fraction of height)


# Bundled fonts (Google Fonts, OFL) -> libass finds them via fontsdir
_FONTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts").replace("\\", "/").replace(":", "\\:")
FONTS_DIR_ARG = f"fontsdir='{_FONTS_PATH}'"

# Pre-built style presets the frontend can reference by name
SUBTITLE_PRESETS = {
    "default": SubtitleStyle(),   # white text, black outline, bottom-center

    "bold_white": SubtitleStyle(
        font_size=34, bold=True, outline=3, shadow=2
    ),

    "yellow": SubtitleStyle(
        primary_colour="&H0000FFFF",   # yellow (BGR: 00FFFF)
        outline_colour="&H00000000",
        font_size=31, bold=True
    ),

    "top": SubtitleStyle(
        alignment=6,       # top-center (force_style uses SSA numbering: 1-3 bottom, 5-7 top, 9-11 middle)
        margin_v=20,
    ),

    "box": SubtitleStyle(
        border_style=3,                # opaque background box
        back_colour="&H4D000000",      # semi-transparent black box
        outline_colour="&H4D000000",   # libass paints a box (border_style 3) in the outline colour
        primary_colour="&H00FFFFFF",
        outline=3, shadow=0,
        font_size=28,
    ),

    "minimal": SubtitleStyle(
        outline=1, shadow=0,
        font_size=25,
        back_colour="&HFF000000",      # fully transparent box
    ),


    # ── CutSmart caption templates (added) ─────────────────────────────────
    "impact_yellow": SubtitleStyle(
        font_name="Impact", font_size=40,
        primary_colour="&H0000F0FF",      # warm yellow
        outline=3, shadow=2,
    ),

    "outline_pop": SubtitleStyle(
        font_name="Arial Black", font_size=37, bold=True,
        primary_colour="&H00FFFFFF",
        outline=4, shadow=0,
    ),

    "cinema": SubtitleStyle(
        font_name="Georgia", font_size=29, italic=True,
        outline=1, shadow=2,
    ),

    "bubble": SubtitleStyle(
        font_name="Arial", font_size=31, bold=True,
        primary_colour="&H000B0B0B",      # near-black text
        back_colour="&H00B8F574",         # mint box (BGR of #74F5B8)
        outline_colour="&H00B8F574",      # what paints the box
        border_style=3, outline=3, shadow=0,
    ),

    "white_card": SubtitleStyle(
        font_name="Arial", font_size=29, bold=True,
        primary_colour="&H00111111",
        back_colour="&H00FFFFFF",         # white box
        outline_colour="&H00FFFFFF",      # what paints the box
        border_style=3, outline=3, shadow=0,
    ),

    # Sentinel: the animated look is written by create_remapped_ass and burned by burn_subtitles().
    # No SubtitleStyle object needed; all styling lives in the .ass file.
    "animated": None,
}


MIN_CUT_S = 0.05        # shorter "cuts" change nothing but still split the video
MIN_KEEP_S = 0.15       # kept slivers this short are stranded padding, not speech


def get_keep_segments(pause_df: pd.DataFrame, video_duration: float,
                      filler_cuts: list = None, style_profile: dict = None,
                      silence_cuts: list = None):
    """
    Converts the pause decision table into a list of (start, end) video
    segments that should be kept and stitched together.

    filler_cuts: optional list of (start, end) tuples from filler_remover.py.
    These are speech segments (not pauses) that should also be cut.
    They are merged with the pause CUT decisions into one unified cut list
    before calculating keep segments, so the render engine sees a single
    sorted list of regions to remove.

    style_profile: the same profile passed to decision_engine.classify_pauses().
    Used to derive how aggressively COMPRESS-flagged pauses should actually
    be shortened (see decision_engine.compress_target) — this is what makes the
    "Gap Removal" preset selected in the UI (Natural/Balanced/Aggressive) or
    a reference video's detected pacing actually take effect on COMPRESS
    pauses, not just on which pauses get flagged CUT.
    """
    print("   Calculating final video segments...")

    keep_len = compress_target(style_profile)
    print(f"   ⏱️  Compress target: {keep_len}s "
          f"(from compress_duration={  (style_profile or {}).get('compress_duration', 'default') })")

    # ── Build unified cut list from pause decisions + filler cuts ────────
    # Format: list of (start, end, label) sorted by start time
    all_cuts = []

    if not pause_df.empty:
        for _, row in pause_df.iterrows():
            decision  = row['decision']
            gap_start = row['start_time']
            gap_end   = row['end_time']

            if decision == 'CUT':
                all_cuts.append((gap_start, gap_end, 'PAUSE_CUT'))
            elif decision == 'COMPRESS':
                # COMPRESS: keep only the first `compress_target` seconds of
                # the pause, cut the rest. compress_target is derived from
                # the style profile so it always shortens the pause instead
                # of silently doing nothing (see decision_engine.compress_target).
                compressed_end = min(gap_start + keep_len, gap_end)
                all_cuts.append((compressed_end, gap_end, 'COMPRESS'))

    if filler_cuts:
        for (fs, fe) in filler_cuts:
            all_cuts.append((fs, fe, 'FILLER'))
    for (ss_, se_) in silence_cuts or []:
        all_cuts.append((ss_, se_, 'SILENCE'))
    all_cuts = [c for c in all_cuts if c[1] - c[0] >= MIN_CUT_S]

    # Sort all cuts by start time
    all_cuts.sort(key=lambda x: x[0])

    # ── Merge overlapping cut regions ────────────────────────────────────
    # A filler word might overlap with a pause boundary — merge them cleanly
    merged_cuts = []
    for start, end, label in all_cuts:
        if merged_cuts and start - merged_cuts[-1][1] < MIN_KEEP_S:  # overlapping, or a sliver apart
            # Overlapping or adjacent — extend the previous cut
            merged_cuts[-1] = (merged_cuts[-1][0], max(merged_cuts[-1][1], end), label)
        else:
            merged_cuts.append((start, end, label))

    # ── Build keep segments from the gaps between cuts ───────────────────
    if not merged_cuts:
        print("   ℹ️  No cuts to make — keeping entire video.")
        return [(0.0, video_duration)]

    keep_segments      = []
    current_keep_start = 0.0

    for cut_start, cut_end, label in merged_cuts:
        if cut_start - current_keep_start >= MIN_KEEP_S:
            keep_segments.append((current_keep_start, cut_start))

        if label == 'PAUSE_CUT':
            print(f"   ✂️  PAUSE CUT   {cut_start:.2f}s–{cut_end:.2f}s")
        elif label == 'COMPRESS':
            print(f"   ⏱️  COMPRESS    {cut_start:.2f}s–{cut_end:.2f}s (tail removed)")
        elif label == 'FILLER':
            print(f"   🗑️  FILLER CUT  {cut_start:.2f}s–{cut_end:.2f}s")
        elif label == 'SILENCE':
            print(f"   🔇 SILENCE CUT {cut_start:.2f}s–{cut_end:.2f}s")

        current_keep_start = cut_end

    # Final segment to end of video
    if video_duration - current_keep_start >= MIN_KEEP_S:
        keep_segments.append((current_keep_start, video_duration))


    print(f"\n   📊 Total segments to stitch: {len(keep_segments)}")
    for i, (s, e) in enumerate(keep_segments):
        print(f"      Segment {i+1}: {s:.2f}s – {e:.2f}s  ({e-s:.2f}s)")

    return keep_segments


def _run_ff(cmd, duration=None, cb=None):
    """Run ffmpeg; when cb is given, report 0-100 progress parsed from -progress output."""
    if cb is None or not duration:
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.returncode, (res.stderr or "")
    import tempfile
    errf = tempfile.TemporaryFile(mode="w+")
    p = subprocess.Popen([cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:],
                         stdout=subprocess.PIPE, stderr=errf, text=True)
    for line in p.stdout:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                cb(min(99, int(float(line.split("=", 1)[1]) / 1e6 / duration * 100)))
            except Exception:
                pass
    p.wait()
    errf.seek(0)
    return p.returncode, errf.read()


def burn_subtitles(intermediate, output_filename, ass_path, job_prefix="recap", progress_cb=None, duration=None):
    """Pass 2 only: burn captions onto an existing caption-less intermediate video.
    Returns True when captions were burned in, False when only a soft track could be attached."""
    safe_sub = f"subs_{job_prefix}_anim.ass"
    shutil.copy2(ass_path, safe_sub)
    vf = f"ass={safe_sub}:{FONTS_DIR_ARG}"
    last_err = ""
    try:
        cmd = ["ffmpeg", "-y", "-i", intermediate, "-vf", vf,
               "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "copy",
               "-metadata", f"comment={burned_captions.EXPORT_MARK}", output_filename]
        rc, err = _run_ff(cmd, duration, progress_cb)
        if rc == 0:
            return True
        last_err = err[-600:]
        cmd = ["ffmpeg", "-y", "-i", intermediate, "-i", safe_sub, "-c:v", "copy", "-c:a", "copy",
               "-c:s", "mov_text", output_filename]
        rc, err = _run_ff(cmd)
        if rc == 0:
            return False
        last_err += "\n" + err[-400:]
    finally:
        if os.path.exists(safe_sub):
            os.remove(safe_sub)
    tail = [l for l in last_err.strip().splitlines() if l.strip()][-1:] or [""]
    raise RuntimeError("Subtitle burn step failed - " + tail[0][:160])
