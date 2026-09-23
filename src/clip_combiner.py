"""
clip_combiner.py — Multi-Clip Combiner (Step 3) + Transition Renderer (Step 4)

Step 3: Accepts a list of already-edited video paths (outputs from the main
        pipeline) and stitches them into one combined video.

Step 4: Applies transitions between clips based on the dominant transition
        type detected from the reference video (or a style preset).

        Supported transition types:
          hard_cut   — direct concatenation, no effect (default)
          crossfade  — xfade filter blends N frames between clips
          fade       — each clip fades out to black then in from black

        The transition type comes from style_profile['cv_transition_type']
        which is populated by scene_detector._analyse_transitions() in app.py.

Usage (called from app.py after all individual clips are processed):
    from clip_combiner import combine_clips

    output_path = combine_clips(
        clip_paths        = ["edited_clip1.mp4", "edited_clip2.mp4", ...],
        transition_type   = "crossfade",   # hard_cut | crossfade | fade
        transition_duration = 0.5,         # seconds (ignored for hard_cut)
        output_filename   = "combined_final.mp4",
    )
"""

import subprocess
import os
import tempfile
import json
from typing import Literal


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

TransitionType = str   # any value from the UI dropdown

# Maps UI select values → FFmpeg xfade transition names (or special handlers)
# Full xfade list: https://ffmpeg.org/ffmpeg-filters.html#xfade
_XFADE_MAP = {
    "crossfade":    "fade",          # smooth dissolve
    "fade":         None,            # handled by _combine_fade (to black)
    "fade_white":   None,            # handled by _combine_fade_white
    "wipe_left":    "wipeleft",
    "wipe_right":   "wiperight",
    "wipe_up":      "wipeup",
    "wipe_down":    "wipedown",
    "slide_left":   "slideleft",
    "slide_right":  "slideright",
    "zoom_in":      "zoominrotate", # closest xfade approximation
    "zoom_out":     "fadeblack",    # fallback — no native zoom-out in xfade
    "blur":         "smoothup",     # closest visual approximation
    "pixelize":     "pixelize",
    "radial":       "radial",
    "circle_open":  "circleopen",
    "hard_cut":     None,            # handled by _combine_hard_cut
    "auto":         None,            # resolved before reaching combine_clips
}


def get_video_duration(video_path: str) -> float:
    """
    Returns the duration of a video in seconds using ffprobe.
    Returns 0.0 on failure.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                video_path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0.0))
    except Exception as e:
        print(f"   ⚠️  ffprobe failed for {video_path}: {e}")
        return 0.0


def combine_clips(
    clip_paths:           list[str],
    transition_type:      TransitionType = "hard_cut",
    transition_duration:  float = 0.5,
    output_filename:      str   = "combined_output.mp4",
    job_prefix:           str   = "cmb",
) -> str:
    """
    Combines multiple edited video clips into a single output file,
    applying the specified transition between each clip.

    Args:
        clip_paths:           Ordered list of edited video file paths.
        transition_type:      'hard_cut' | 'crossfade' | 'fade'
        transition_duration:  Duration of the transition effect in seconds.
                              Ignored for hard_cut. Recommended: 0.3–1.0s.
        output_filename:      Output file path.

    Returns:
        output_filename on success.

    Raises:
        RuntimeError if combining fails.
    """
    if not clip_paths:
        raise ValueError("combine_clips: clip_paths is empty.")

    # Filter out missing files before we start
    valid_clips = [p for p in clip_paths if os.path.exists(p)]
    if not valid_clips:
        raise RuntimeError("combine_clips: None of the clip files exist on disk.")

    missing = [p for p in clip_paths if not os.path.exists(p)]
    if missing:
        print(f"   ⚠️  Skipping {len(missing)} missing clip(s): {missing}")

    if len(valid_clips) == 1:
        # Only one clip — just copy it to the output path
        print("   ℹ️  Only one clip provided — copying directly to output.")
        _run_ffmpeg(["ffmpeg", "-y", "-i", valid_clips[0],
                     "-c", "copy", output_filename])
        return output_filename

    print(f"\n   🎬 Combining {len(valid_clips)} clip(s) "
          f"with transition='{transition_type}' "
          f"(duration={transition_duration}s)")

    if transition_type == "fade":
        _combine_fade(valid_clips, transition_duration, output_filename, job_prefix, color="black")
    elif transition_type == "fade_white":
        _combine_fade(valid_clips, transition_duration, output_filename, job_prefix, color="white")
    elif transition_type == "hard_cut":
        _combine_hard_cut(valid_clips, output_filename, job_prefix)
    else:
        # All remaining types (crossfade, wipes, slides, effects) use xfade
        xfade_name = _XFADE_MAP.get(transition_type, "fade")
        if xfade_name is None:
            # Fallback to hard_cut if mapping is missing
            _combine_hard_cut(valid_clips, output_filename, job_prefix)
        else:
            _combine_crossfade(valid_clips, transition_duration, output_filename,
                               job_prefix, xfade_effect=xfade_name)

    if not os.path.exists(output_filename):
        raise RuntimeError(
            f"combine_clips: Output file not created: {output_filename}"
        )

    size_mb = os.path.getsize(output_filename) / 1024 / 1024
    print(f"   ✅ Combined video saved: {output_filename} ({size_mb:.1f}MB)")
    return output_filename


# ---------------------------------------------------------------------------
# Transition implementations
# ---------------------------------------------------------------------------

def _combine_hard_cut(clip_paths: list[str], output_filename: str,
                      job_prefix: str = "cmb"):
    """
    Simplest method: FFmpeg concat demuxer.
    No re-encoding of video stream (codec copy) — extremely fast.
    Only works reliably when all clips have identical codec, resolution, fps.
    Falls back to re-encode if copy fails.
    """
    print("   ✂️  Hard cut: using concat demuxer...")

    list_file = f"_concat_list_{job_prefix}.txt"
    list_file = _write_concat_list(clip_paths, list_file)
    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            output_filename,
        ]
        _run_ffmpeg(cmd)
    finally:
        _safe_remove(list_file)


def _get_common_resolution(clip_paths: list[str]) -> tuple[int, int]:
    """
    Finds the most common resolution among clips (by frequency).
    Used so crossfade normalisation scales everything to one consistent size,
    preventing xfade failures on mixed-resolution inputs (Bug 6 fix).
    Falls back to 1920×1080 if probing fails.
    """
    import json
    from collections import Counter
    resolutions = []
    for p in clip_paths:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", p],
                check=True, capture_output=True, text=True
            )
            streams = json.loads(result.stdout).get("streams", [])
            for s in streams:
                if s.get("codec_type") == "video":
                    resolutions.append((s.get("width", 1920), s.get("height", 1080)))
                    break
        except Exception:
            resolutions.append((1920, 1080))
    if not resolutions:
        return (1920, 1080)
    # Pick most common resolution; ties broken by largest area
    most_common = Counter(resolutions).most_common(1)[0][0]
    return most_common


def _combine_crossfade(clip_paths: list[str], duration: float,
                       output_filename: str, job_prefix: str = "cmb",
                       xfade_effect: str = "fade"):
    """
    Applies an xfade crossfade between every pair of consecutive clips.

    FFmpeg xfade filter blends the end of clip N with the start of clip N+1
    over `duration` seconds. The offset is computed as cumulative duration
    minus the overlap, so the timeline contracts by (N-1) * duration.

    Strategy for N clips:
      - Re-encode all clips to a normalised intermediate (same res/fps/codec)
        so xfade has consistent inputs. All clips are scaled to the most
        common resolution among the inputs (Bug 6 fix).
      - Chain xfade filters: [0][1]xfade[v01]; [v01][2]xfade[v12]; ...
      - Similarly chain acrossfade for audio.
    """
    print(f"   🌊 xfade '{xfade_effect}': {duration}s blend between each clip...")

    n = len(clip_paths)

    # Determine common resolution for all clips (Bug 6 fix)
    target_w, target_h = _get_common_resolution(clip_paths)
    print(f"   📐 Normalising all clips to {target_w}×{target_h}...")

    # Step 1: Normalise all clips to same codec/fps/resolution so xfade works
    norm_clips = []
    try:
        for i, src in enumerate(clip_paths):
            norm = f"_norm_{job_prefix}_{i:04d}.mp4"
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", src,
                "-vf", (
                    f"scale={target_w}:{target_h}:"
                    f"force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2"
                ),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                "-r", "30",           # normalise to 30fps
                norm,
            ])
            norm_clips.append(norm)
            print(f"      Normalised clip {i+1}/{n}")

        # Step 2: Get durations so we can compute xfade offsets
        durations = [get_video_duration(c) for c in norm_clips]

        # Step 3: Build the complex xfade filter chain
        # Offsets: after clip i ends minus one transition duration overlap
        offsets = []
        cumulative = 0.0
        for i in range(n - 1):
            cumulative += durations[i] - duration
            offsets.append(max(0.0, cumulative))

        # Build -filter_complex string
        # Video: [0:v][1:v]xfade=transition=fade:duration=D:offset=O[v01];
        #        [v01][2:v]xfade=...
        # Audio: [0:a][1:a]acrossfade=d=D[a01]; [a01][2:a]acrossfade=...
        inputs = []
        for c in norm_clips:
            inputs += ["-i", c]

        video_parts = []
        audio_parts = []

        prev_v = "0:v"
        prev_a = "0:a"

        for i in range(n - 1):
            next_v = f"{i+1}:v"
            next_a = f"{i+1}:a"
            out_v  = f"v{i:02d}{i+1:02d}"
            out_a  = f"a{i:02d}{i+1:02d}"

            video_parts.append(
                f"[{prev_v}][{next_v}]xfade="
                f"transition={xfade_effect}:"
                f"duration={duration}:"
                f"offset={offsets[i]:.4f}"
                f"[{out_v}]"
            )
            audio_parts.append(
                f"[{prev_a}][{next_a}]acrossfade="
                f"d={duration}"
                f"[{out_a}]"
            )

            prev_v = out_v
            prev_a = out_a

        filter_complex = ";".join(video_parts + audio_parts)
        final_v = prev_v
        final_a = prev_a

        cmd = (
            ["ffmpeg", "-y"]
            + inputs
            + [
                "-filter_complex", filter_complex,
                "-map", f"[{final_v}]",
                "-map", f"[{final_a}]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                output_filename,
            ]
        )
        _run_ffmpeg(cmd)

    finally:
        for f in norm_clips:
            _safe_remove(f)


def _combine_fade(clip_paths: list[str], duration: float,
                  output_filename: str, job_prefix: str = "cmb",
                  color: str = "black"):
    """
    Applies a fade (to black or white) at the end of each clip and
    a fade-from at the start of the next clip, then hard-cuts them together.
    """
    print(f"   🌑 Fade to {color}: {duration}s per clip edge...")

    n = len(clip_paths)
    faded_clips = []

    try:
        for i, src in enumerate(clip_paths):
            dur = get_video_duration(src)
            if dur <= 0:
                print(f"   ⚠️  Could not get duration for clip {i+1}, skipping fade")
                faded_clips.append(src)
                continue

            faded = f"_faded_{job_prefix}_{i:04d}.mp4"

            fade_out_start = max(0.0, dur - duration)

            # afade mirrors the video fade for audio
            vf = (
                f"fade=t=in:st=0:d={duration}:color={color},"
                f"fade=t=out:st={fade_out_start:.4f}:d={duration}:color={color}"
            )
            af = (
                f"afade=t=in:st=0:d={duration},"
                f"afade=t=out:st={fade_out_start:.4f}:d={duration}"
            )

            _run_ffmpeg([
                "ffmpeg", "-y", "-i", src,
                "-vf", vf,
                "-af", af,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "192k",
                faded,
            ])
            faded_clips.append(faded)
            print(f"      Faded clip {i+1}/{n}")

        # Now hard-cut the faded clips together
        _combine_hard_cut(faded_clips, output_filename, job_prefix)

    finally:
        for f in faded_clips:
            # Only remove temp faded clips, not the originals
            if "_faded_" in f and job_prefix in f:
                _safe_remove(f)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_concat_list(clip_paths: list[str],
                       list_file: str = "_combine_concat_list.txt") -> str:
    """Writes an FFmpeg concat demuxer manifest and returns its path."""
    with open(list_file, "w", encoding="utf-8") as f:
        for p in clip_paths:
            abs_path = os.path.abspath(p).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")
    return list_file


def _run_ffmpeg(cmd: list[str]):
    """Runs an FFmpeg command. Raises RuntimeError with stderr on failure."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"FFmpeg failed.\nCommand: {' '.join(cmd)}\n"
            f"Stderr: {e.stderr[-800:]}"
        )


def _safe_remove(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Transition type resolver (convenience helper for app.py)
# ---------------------------------------------------------------------------

def resolve_transition_type(style_profile: dict, fallback: str = "hard_cut") -> str:
    """
    Reads the transition type from a style profile dict (set when a reference
    video was provided). Maps raw CV output to a supported transition value.
    If the user picked an explicit transition (not 'auto'), it bypasses this.
    """
    raw = style_profile.get("cv_transition_type", fallback)

    # Map CV-detected names → UI/combine values
    mapping = {
        "hard_cut":   "hard_cut",
        "crossfade":  "crossfade",
        "dissolve":   "crossfade",
        "fade":       "fade",
        "cross":      "crossfade",
        "wipe":       "wipe_left",
    }
    # If it's already a known UI value (user-selected), pass it straight through
    known_ui_values = set(_XFADE_MAP.keys()) | {"fade", "fade_white", "hard_cut", "auto"}
    if raw in known_ui_values:
        return raw if raw != "auto" else fallback

    resolved = mapping.get(str(raw).lower(), fallback)
    print(f"   🎞️  Transition resolved from CV: '{raw}' → '{resolved}'")
    return resolved
