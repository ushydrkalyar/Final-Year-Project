import os
import shutil
import subprocess

# ---------------------------------------------------------------------------
# Feedback Service
# ---------------------------------------------------------------------------
# Handles the POST /feedback/ endpoint.
#
# When a user watches the edited video and finds a segment that was wrongly
# cut or should have been cut, they send a correction:
#   { job_id, timestamp_start, timestamp_end, correction: "CUT"|"KEEP" }
#
# This service:
#   1. Loads the original (pre-edit) video for that job from job_store
#   2. Extracts only the affected segment with the user's correction applied
#   3. Splices that new clip back into the already-edited video using FFmpeg
#      concat — so we never re-encode the whole video, only the changed part
#   4. Returns the updated video path
# ---------------------------------------------------------------------------


def apply_feedback(
    job_id:          str,
    timestamp_start: float,
    timestamp_end:   float,
    correction:      str,        # "CUT" or "KEEP"
    job_store:       dict,       # shared dict from app.py
) -> str:
    """
    Applies a single timestamp-based correction to an already-edited video.

    Returns the path to the corrected output video.
    Raises ValueError for bad inputs, RuntimeError if FFmpeg fails.
    """
    # ── Validate ──────────────────────────────────────────────────────────
    if job_id not in job_store:
        raise ValueError(f"Job '{job_id}' not found.")

    job = job_store[job_id]

    if job.get('status') != 'done':
        raise ValueError(f"Job '{job_id}' is not complete yet (status: {job.get('status')}).")

    correction = correction.upper()
    if correction not in ('CUT', 'KEEP'):
        raise ValueError(f"correction must be 'CUT' or 'KEEP', got '{correction}'.")

    if timestamp_end <= timestamp_start:
        raise ValueError("timestamp_end must be greater than timestamp_start.")

    original_video = job.get('input_video_path')
    edited_video   = job.get('output_path')

    if not original_video or not os.path.exists(original_video):
        raise ValueError("Original video for this job is no longer on disk.")
    if not edited_video or not os.path.exists(edited_video):
        raise ValueError("Edited video for this job is no longer on disk.")

    print(f"\n   🔧 Applying feedback for job {job_id}")
    print(f"      Segment: {timestamp_start:.2f}s – {timestamp_end:.2f}s")
    print(f"      Correction: {correction}")

    corrected_path = f"corrected_{job_id}.mp4"

    if correction == 'CUT':
        _splice_out(edited_video, timestamp_start, timestamp_end, corrected_path)
    else:  # KEEP
        _splice_in(original_video, edited_video, timestamp_start, timestamp_end, corrected_path)

    # Update job store so subsequent feedbacks use the corrected video
    job_store[job_id]['output_path'] = corrected_path
    print(f"   ✅ Corrected video saved: {corrected_path}")
    return corrected_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(cmd: list, label: str):
    """Run an FFmpeg command, raise RuntimeError on failure."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"{label} failed:\n{e.stderr[-400:]}")


def _splice_out(edited_video: str, cut_start: float, cut_end: float,
                output_path: str):
    """
    Removes a time range from the edited video by:
      1. Extracting the part BEFORE the cut
      2. Extracting the part AFTER the cut
      3. Concatenating them

    This implements the "I want this segment removed" feedback.
    """
    before_clip = "_fb_before.mp4"
    after_clip  = "_fb_after.mp4"
    list_file   = "_fb_concat.txt"

    try:
        # Part before the cut
        print(f"      Extracting before-segment (0 – {cut_start:.2f}s)...")
        _run([
            "ffmpeg", "-y",
            "-i", edited_video,
            "-t", str(cut_start),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            before_clip
        ], "Before-segment extraction")

        # Part after the cut
        print(f"      Extracting after-segment ({cut_end:.2f}s – end)...")
        _run([
            "ffmpeg", "-y",
            "-ss", str(cut_end),
            "-i", edited_video,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            after_clip
        ], "After-segment extraction")

        # Concatenate
        with open(list_file, "w") as f:
            f.write(f"file '{os.path.abspath(before_clip)}'\n")
            f.write(f"file '{os.path.abspath(after_clip)}'\n")

        print(f"      Stitching corrected video...")
        _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            output_path
        ], "Concat")

    finally:
        for f in [before_clip, after_clip, list_file]:
            if os.path.exists(f):
                os.remove(f)


def _splice_in(original_video: str, edited_video: str,
               keep_start: float, keep_end: float, output_path: str):
    """
    Inserts a segment from the ORIGINAL video back into the edited video at
    the corresponding position. This implements the "you cut too much here,
    put this back" feedback.

    The timestamp refers to the position in the EDITED video where the
    insertion should happen — we insert the original-video segment at that
    point in the edited timeline.
    """
    before_clip   = "_fb_before.mp4"
    restored_clip = "_fb_restored.mp4"
    after_clip    = "_fb_after.mp4"
    list_file     = "_fb_concat.txt"

    duration = keep_end - keep_start

    try:
        # Part of edited video before insertion point
        print(f"      Extracting before-segment (0 – {keep_start:.2f}s from edited)...")
        _run([
            "ffmpeg", "-y",
            "-i", edited_video,
            "-t", str(keep_start),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            before_clip
        ], "Before-segment")

        # The segment to restore from the original video
        print(f"      Restoring {duration:.2f}s from original at {keep_start:.2f}s...")
        _run([
            "ffmpeg", "-y",
            "-ss", str(keep_start),
            "-i", original_video,
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            restored_clip
        ], "Restore-segment")

        # Part of edited video after insertion point
        print(f"      Extracting after-segment ({keep_end:.2f}s – end from edited)...")
        _run([
            "ffmpeg", "-y",
            "-ss", str(keep_end),
            "-i", edited_video,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            after_clip
        ], "After-segment")

        # Concatenate all three parts
        with open(list_file, "w") as f:
            f.write(f"file '{os.path.abspath(before_clip)}'\n")
            f.write(f"file '{os.path.abspath(restored_clip)}'\n")
            f.write(f"file '{os.path.abspath(after_clip)}'\n")

        print(f"      Stitching corrected video...")
        _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            output_path
        ], "Concat")

    finally:
        for f in [before_clip, restored_clip, after_clip, list_file]:
            if os.path.exists(f):
                os.remove(f)