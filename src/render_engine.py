import subprocess
import os
import shutil
import pandas as pd
from dataclasses import dataclass, field

COMPRESS_TARGET_DURATION = 0.4   # fallback only — used when no style_profile is supplied


def _resolve_compress_target(style_profile: dict = None) -> float:
    """
    Determines how many seconds of a COMPRESS-flagged pause to keep
    (the rest of the pause is cut).

    This MUST scale with the style profile's own `compress_duration`
    threshold — the UI's "Gap Removal" preset (Natural / Balanced /
    Aggressive) and any reference-video-derived style both set that
    threshold, and the user expects gaps to actually shrink accordingly.

    Bug this fixes: previously this was a hardcoded 0.4s target. Any
    style with compress_duration < 0.4s (e.g. the "Aggressive" preset's
    0.3s, or a fast-paced reference video's threshold, which can go as
    low as 0.15s) produced pauses classified COMPRESS that were never
    actually shortened — a duration below 0.4s always clamped back to
    the pause's own end, i.e. a zero-length cut / no-op.

    The target is set to half of compress_duration, which guarantees it
    is always strictly smaller than any pause that was classified as
    COMPRESS (those pauses are, by definition, longer than
    compress_duration) — so a real cut always happens. It's bounded to
    a sane [0.05s, 0.6s] range so behaviour stays predictable at both
    extremes.
    """
    compress_duration = (style_profile or {}).get('compress_duration', 0.8)
    target = compress_duration * 0.5
    target = max(0.05, min(target, 0.6))
    # Extra safety margin: never let the target reach the threshold itself,
    # even if compress_duration is unusually small.
    target = min(target, max(0.05, compress_duration - 0.05))
    return round(target, 3)


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
    font_size:       int   = 18
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

    def to_force_style(self) -> str:
        """
        Converts the style config to an FFmpeg force_style string.
        All fields are passed so nothing falls back to a hardcoded default.
        """
        parts = [
            f"FontName={self.font_name}",
            f"FontSize={self.font_size}",
            f"PrimaryColour={self.primary_colour}",
            f"OutlineColour={self.outline_colour}",
            f"BackColour={self.back_colour}",
            f"Bold={1 if self.bold else 0}",
            f"Italic={1 if self.italic else 0}",
            f"Outline={self.outline}",
            f"Shadow={self.shadow}",
            f"Alignment={self.alignment}",
            f"MarginV={self.margin_v}",
            f"MarginL={self.margin_l}",
            f"MarginR={self.margin_r}",
            f"BorderStyle={self.border_style}",
        ]
        return ",".join(parts)


# Pre-built style presets the frontend can reference by name
SUBTITLE_PRESETS = {
    "default": SubtitleStyle(),   # white text, black outline, bottom-center

    "bold_white": SubtitleStyle(
        font_size=22, bold=True, outline=3, shadow=2
    ),

    "yellow": SubtitleStyle(
        primary_colour="&H0000FFFF",   # yellow (BGR: 00FFFF)
        outline_colour="&H00000000",
        font_size=20, bold=True
    ),

    "top": SubtitleStyle(
        alignment=8,       # top-center
        margin_v=20,
    ),

    "box": SubtitleStyle(
        border_style=3,                # opaque background box
        back_colour="&HAA000000",      # semi-transparent black box
        primary_colour="&H00FFFFFF",
        outline=0, shadow=0,
        font_size=18,
    ),

    "minimal": SubtitleStyle(
        outline=1, shadow=0,
        font_size=16,
        back_colour="&HFF000000",      # fully transparent box
    ),

    # Sentinel — signals the animated ASS code path in render_final_video().
    # No SubtitleStyle object needed; all styling lives in the .ass file.
    "animated": None,
}


def get_keep_segments(pause_df: pd.DataFrame, video_duration: float,
                      filler_cuts: list = None, style_profile: dict = None):
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
    be shortened (see _resolve_compress_target) — this is what makes the
    "Gap Removal" preset selected in the UI (Natural/Balanced/Aggressive) or
    a reference video's detected pacing actually take effect on COMPRESS
    pauses, not just on which pauses get flagged CUT.
    """
    print("   Calculating final video segments...")

    compress_target = _resolve_compress_target(style_profile)
    print(f"   ⏱️  Compress target: {compress_target}s "
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
                # of silently doing nothing (see _resolve_compress_target).
                compressed_end = min(gap_start + compress_target, gap_end)
                all_cuts.append((compressed_end, gap_end, 'COMPRESS'))

    if filler_cuts:
        for (fs, fe) in filler_cuts:
            all_cuts.append((fs, fe, 'FILLER'))

    # Sort all cuts by start time
    all_cuts.sort(key=lambda x: x[0])

    # ── Merge overlapping cut regions ────────────────────────────────────
    # A filler word might overlap with a pause boundary — merge them cleanly
    merged_cuts = []
    for start, end, label in all_cuts:
        if merged_cuts and start <= merged_cuts[-1][1]:
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
        if current_keep_start < cut_start:
            keep_segments.append((current_keep_start, cut_start))

        if label == 'PAUSE_CUT':
            print(f"   ✂️  PAUSE CUT   {cut_start:.2f}s–{cut_end:.2f}s")
        elif label == 'COMPRESS':
            print(f"   ⏱️  COMPRESS    {cut_start:.2f}s–{cut_end:.2f}s (tail removed)")
        elif label == 'FILLER':
            print(f"   🗑️  FILLER CUT  {cut_start:.2f}s–{cut_end:.2f}s")

        current_keep_start = cut_end

    # Final segment to end of video
    if current_keep_start < video_duration:
        keep_segments.append((current_keep_start, video_duration))

    print(f"\n   📊 Total segments to stitch: {len(keep_segments)}")
    for i, (s, e) in enumerate(keep_segments):
        print(f"      Segment {i+1}: {s:.2f}s – {e:.2f}s  ({e-s:.2f}s)")

    return keep_segments


def _make_safe_srt(srt_path: str, job_prefix: str = "job") -> str:
    """
    Copy the SRT into cwd under a plain short name.
    FFmpeg subtitle filter on Windows breaks on any path containing
    drive letters, backslashes, or spaces.
    job_prefix keeps parallel clip renders from overwriting each other's copy.
    """
    safe_local = f"subs_{job_prefix}_working_copy.srt"
    shutil.copy2(srt_path, safe_local)
    return safe_local


def render_final_video(
    input_video:     str,
    segments:        list,
    srt_path:        str,
    output_filename: str = "final_edited_video.mp4",
    subtitle_style:  "SubtitleStyle | None" = None,
    job_prefix:      str = "job",    # unique per clip — prevents temp file collisions
    ass_path:        str = None,     # if set, use animated ASS burn path instead of SRT
):
    """
    Two-pass render:

    Pass 1 — Extract each kept segment WITHOUT subtitles and concatenate
             into a clean intermediate file. Each clip's clock runs from
             its own t=0, so burning subtitles here would misalign them.

    Pass 2 — Burn the remapped SRT onto the fully-stitched intermediate
             with optional subtitle styling via FFmpeg force_style.
             At this point there is one continuous timeline that matches
             the remapped SRT exactly, so subtitles land on the right words.
    """
    print(f"\n   Starting render — {len(segments)} segment(s) to process...")

    if not segments:
        raise RuntimeError("No segments to render — nothing was produced.")

    intermediate = f"_intermediate_{job_prefix}_{os.path.basename(output_filename)}"
    clip_files   = []
    failed_clips = []

    # ------------------------------------------------------------------
    # Pass 1: Extract clips WITHOUT subtitles
    # ------------------------------------------------------------------
    print("\n   Pass 1: Extracting segments (no subtitles)...")
    for i, (start, end) in enumerate(segments):
        duration = end - start

        if duration < 0.05:
            print(f"   ⚠️  Skipping segment {i+1} — too short ({duration:.3f}s)")
            continue

        clip_name = f"temp_clip_{job_prefix}_{i:04d}.mp4"
        print(f"   🎬 Clip {i+1}/{len(segments)}: {start:.2f}s → {end:.2f}s ({duration:.2f}s)")

        command = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", input_video,
            "-t", str(duration),
            # NO subtitle filter here — just video + audio
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            clip_name
        ]

        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            clip_files.append(clip_name)
            print(f"      ✅ Clip {i+1} done")
        except subprocess.CalledProcessError as e:
            failed_clips.append(i + 1)
            print(f"   ❌ Clip {i+1} failed:\n      {e.stderr[-400:]}")

    if not clip_files:
        raise RuntimeError("All clips failed to extract. Check FFmpeg installation.")

    # Write concat manifest
    list_file = f"concat_list_{job_prefix}.txt"
    with open(list_file, "w") as f:
        for clip in clip_files:
            abs_path = os.path.abspath(clip).replace('\\', '/')
            f.write(f"file '{abs_path}'\n")

    # Stitch clips into intermediate (no subtitles yet)
    print("\n   🧵 Stitching clips into intermediate...")
    concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "192k",
        intermediate
    ]

    concat_ok = False
    try:
        subprocess.run(concat_cmd, check=True, capture_output=True, text=True)
        concat_ok = True
        print(f"   ✅ Intermediate saved: {intermediate}")
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Concat failed:\n{e.stderr[-400:]}")
    finally:
        for clip in clip_files:
            if os.path.exists(clip):
                os.remove(clip)
        if os.path.exists(list_file):
            os.remove(list_file)

    if not concat_ok:
        raise RuntimeError("Concat step failed — intermediate video not produced.")

    # NOTE: intermediate is kept on disk after this function returns.
    # app.py stores its path in job_store['intermediate_path'] so that
    # POST /restyle/{job_id} can re-burn subtitles without re-encoding.
    # It is cleaned up when the job is removed from job_store (on download).

    # ------------------------------------------------------------------
    # Pass 2: Burn remapped subtitles onto the stitched intermediate
    # ------------------------------------------------------------------
    print("\n   Pass 2: Burning subtitles onto stitched video...")

    if ass_path and os.path.exists(ass_path):
        # ── Animated ASS path ──────────────────────────────────────────
        # Copy to a safe local name (avoids Windows path issues in FFmpeg)
        safe_sub = f"subs_{job_prefix}_anim.ass"
        shutil.copy2(ass_path, safe_sub)
        safe_sub_fwd = safe_sub.replace('\\', '/')
        sub_filter = f"ass={safe_sub_fwd}"
        print(f"   🎨 Animated subtitle mode — ASS filter: {safe_sub}")
    else:
        # ── Standard SRT path (all other presets) ──────────────────────
        safe_sub = _make_safe_srt(srt_path, job_prefix)
        style    = subtitle_style or SubtitleStyle()
        force    = style.to_force_style()
        sub_filter = f"subtitles={safe_sub}:force_style='{force}'"
        print(f"   🎨 Subtitle style: {force}")

    burn_cmd = [
        "ffmpeg", "-y",
        "-i", intermediate,
        "-vf", sub_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "copy",
        output_filename
    ]

    burn_ok = False
    try:
        subprocess.run(burn_cmd, check=True, capture_output=True, text=True)
        burn_ok = True
        print(f"   ✅ Final video with subtitles: {output_filename}")
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Subtitle burn failed:\n{e.stderr[-400:]}")
    finally:
        if os.path.exists(safe_sub):
            os.remove(safe_sub)
        # NOTE: intermediate is intentionally NOT deleted here.
        # It is kept so POST /restyle/{job_id} can re-burn with a different
        # subtitle style without re-running the full pipeline.
        # app.py is responsible for cleaning it up on job removal.

    if not burn_ok:
        # Clean up intermediate on failure since it's useless without output
        if os.path.exists(intermediate):
            os.remove(intermediate)
        raise RuntimeError("Subtitle burn step failed — final video not produced.")

    # Return intermediate path so app.py can store it in job_store
    return intermediate