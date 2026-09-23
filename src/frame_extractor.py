import subprocess
import os
import base64
import json


# ---------------------------------------------------------------------------
# Frame Extractor
# ---------------------------------------------------------------------------
# Extracts evenly-spaced thumbnail frames from the original video using
# FFmpeg. These are used by the frontend to render the filmstrip layer
# behind the coloured segment bars in the dual-timeline UI.
#
# Design decisions:
#   - Fixed count (default 30) regardless of video length — avoids hundreds
#     of images for long videos while keeping the strip dense enough.
#   - 160×90px JPEG at quality 4 — sharp enough for thumbnails, ~8-12KB each.
#     30 frames × 10KB = ~300KB total per job, acceptable for in-memory storage.
#   - Frames stored as base64 strings in job_store so the frontend can receive
#     them in the /timeline/ response without a separate per-frame endpoint.
#   - FFmpeg's 'fps' filter with a calculated rate handles the even spacing.
# ---------------------------------------------------------------------------

THUMB_WIDTH   = 160
THUMB_HEIGHT  = 90
THUMB_QUALITY = 4     # FFmpeg JPEG quality: 1=best, 31=worst. 4 is sharp enough.
DEFAULT_COUNT = 28    # Number of thumbnails to extract


def extract_frames(
    video_path:    str,
    req_id:        str,
    video_duration: float,
    count:         int = DEFAULT_COUNT,
) -> list:
    """
    Extracts `count` evenly-spaced thumbnail frames from `video_path`.

    Returns a list of dicts:
    [
        {
            "timestamp":   4.2,          # seconds into original video
            "position_pct": 7.0,         # percentage along original timeline
            "data":        "base64...",  # JPEG image as base64 string
        },
        ...
    ]

    Returns an empty list (non-fatal) if FFmpeg fails — the timeline
    still works without thumbnails, just without the filmstrip layer.
    """
    if video_duration <= 0:
        print("   ⚠️  Frame extraction skipped — video duration unknown.")
        return []

    # Output directory for this job's frames
    frame_dir = f"frames_{req_id}"
    os.makedirs(frame_dir, exist_ok=True)

    # Calculate the frame rate needed to get exactly `count` frames
    # from the full video duration. E.g. 28 frames from 60s = 0.467 fps.
    fps_rate = count / video_duration

    print(f"   Extracting {count} thumbnail frames "
          f"({THUMB_WIDTH}×{THUMB_HEIGHT}px) "
          f"at {fps_rate:.3f} fps...")

    output_pattern = os.path.join(frame_dir, "frame_%04d.jpg")

    command = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", (
            f"fps={fps_rate:.6f},"
            f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={THUMB_WIDTH}:{THUMB_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
        ),
        "-q:v", str(THUMB_QUALITY),
        "-frames:v", str(count),
        output_pattern
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Frame extraction failed:\n      {e.stderr[-300:]}")
        _cleanup_dir(frame_dir)
        return []

    # Read all extracted frames, encode as base64, attach timestamps
    frames = []
    for i in range(1, count + 1):
        frame_path = os.path.join(frame_dir, f"frame_{i:04d}.jpg")
        if not os.path.exists(frame_path):
            continue

        # Timestamp of this frame in the original video
        # Frame i is taken at (i - 0.5) / count of the total duration
        # (centres each frame in its equal-width slice)
        timestamp = ((i - 0.5) / count) * video_duration

        try:
            with open(frame_path, 'rb') as f:
                data = base64.b64encode(f.read()).decode('utf-8')
            frames.append({
                "timestamp":    round(timestamp, 2),
                "position_pct": round((timestamp / video_duration) * 100, 2),
                "data":         data,
            })
        except Exception:
            pass   # skip unreadable frames silently

    _cleanup_dir(frame_dir)

    print(f"   ✅ {len(frames)} frames extracted "
          f"(~{sum(len(f['data']) * 3 // 4 for f in frames) // 1024}KB total)")
    return frames


def _cleanup_dir(path: str):
    """Removes the temporary frame directory and all its contents."""
    try:
        if os.path.exists(path):
            for f in os.listdir(path):
                try:
                    os.remove(os.path.join(path, f))
                except OSError:
                    pass
            os.rmdir(path)
    except OSError:
        pass


def get_segment_thumbnail(frames: list, timestamp: float) -> dict | None:
    """
    Returns the thumbnail frame closest to a given timestamp.
    Used by the frontend to show a preview frame when hovering a segment.

    frames:    the list returned by extract_frames()
    timestamp: seconds into the original video
    """
    if not frames:
        return None
    closest = min(frames, key=lambda f: abs(f['timestamp'] - timestamp))
    return closest