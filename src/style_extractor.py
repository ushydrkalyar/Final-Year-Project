import yt_dlp
import pandas as pd
import os



def download_youtube_audio(url: str, output_filename: str = "reference_audio.wav") -> str | None:
    """Downloads audio from a YouTube URL using yt-dlp."""
    import glob, shutil
    print(f"   Downloading reference audio from:\n   {url}")

    stem = output_filename.removesuffix('.wav')

    # Use %(ext)s so yt-dlp fills in the real extension before FFmpeg converts.
    # After FFmpegExtractAudio the output will be stem.wav regardless of
    # the source container — but we also glob for it in case yt-dlp appends
    # extra format tokens to the filename in certain versions.
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'wav',
            'preferredquality': '192',
        }],
        'outtmpl': stem + '.%(ext)s',
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Primary expected path
        expected_path = stem + '.wav'
        if os.path.exists(expected_path):
            print(f"   ✅ Reference audio saved: {expected_path}")
            return expected_path

        # Fallback: glob for any .wav file matching the stem
        # (some yt-dlp versions append codec/format tokens)
        matches = glob.glob(stem + '*.wav')
        if matches:
            best = matches[0]
            if best != expected_path:
                shutil.move(best, expected_path)
            print(f"   ✅ Reference audio saved (glob fallback): {expected_path}")
            return expected_path

        print(f"   ❌ File not found after download: {expected_path}")
        # List what IS in the directory for debugging
        parent = os.path.dirname(stem) or '.'
        nearby = [f for f in os.listdir(parent) if os.path.basename(stem)[:8] in f]
        if nearby:
            print(f"   ℹ️  Files with matching prefix: {nearby}")
        return None

    except Exception as e:
        print(f"   ❌ yt-dlp error: {e}")
        return None


def download_youtube_video(url: str, output_filename: str = "reference_video.mp4") -> str | None:
    """Download a compact MP4 reference video for visual-style analysis."""
    import glob

    stem, _ = os.path.splitext(output_filename)
    expected_path = stem + '.mp4'
    ydl_opts = {
        # 720p is sufficient for scene, transition, and colour analysis while
        # avoiding an unnecessarily large temporary reference download.
        'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
        'merge_output_format': 'mp4',
        'outtmpl': stem + '.%(ext)s',
        'quiet': True,
        'no_warnings': True,
    }

    try:
        print("   Downloading reference video for CV analysis...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(expected_path):
            print(f"   Reference video saved: {expected_path}")
            return expected_path

        matches = glob.glob(stem + '*.mp4')
        if matches:
            print(f"   Reference video saved: {matches[0]}")
            return matches[0]

        print("   Reference video was not produced by yt-dlp.")
        return None
    except Exception as exc:
        print(f"   Reference video download failed: {exc}")
        return None


def calculate_style_profile(pause_data: pd.DataFrame, transcript_data: dict = None) -> dict:
    """
    Analyses a reference video's pause data (and optionally its transcript)
    to build a full style profile for the Decision Engine.

    Extracts 4 metrics:

    1. PAUSE DURATION THRESHOLDS (from pause_data)
       The core metric. Uses median pause duration to set CUT/COMPRESS
       thresholds. Median is used instead of mean because one very long
       silence (e.g. intro card) would skew the mean badly.

    2. SPEAKING RATE  (from transcript_data words-per-second)
       Fast creators (>3.5 words/sec) edit tightly — we tighten thresholds.
       Slow creators (<2.0 words/sec) leave more breathing room.
       This is a multiplier applied on top of the duration thresholds.

    3. SENTENCE-BOUNDARY PAUSE RATIO  (from pause_data)
       What fraction of pauses occur at sentence boundaries (.?!)?
       High ratio → creator intentionally breathes between sentences → 
       we should be more conservative about cutting boundary pauses.
       Low ratio  → creator talks in a stream → boundary pauses are fair game.

    4. CUT FREQUENCY  (from pause_data relative to transcript length)
       How often does the reference video have edits per minute of speech?
       High cut frequency → aggressive editing style → tighten our thresholds.
       Low cut frequency  → relaxed style → loosen thresholds.

    All four metrics are combined into a single style_profile dict that
    decision_engine.py uses directly.
    """
    print("   Calculating full style profile from reference...")

    # ── Defaults if we have no data ────────────────────────────────────────
    if pause_data.empty:
        print("   ⚠️  No pauses in reference — using balanced defaults.")
        return _default_profile()

    # ══════════════════════════════════════════════════════════════════════
    # METRIC 1: Pause duration thresholds
    # ══════════════════════════════════════════════════════════════════════
    median_pause = pause_data['duration'].median()
    mean_pause   = pause_data['duration'].mean()

    # Raw thresholds from pause durations alone
    raw_max_duration      = median_pause * 2.0
    raw_compress_duration = median_pause * 1.2

    print("      [Metric 1] Pause durations")
    print(f"         median: {median_pause:.3f}s   mean: {mean_pause:.3f}s")
    print(f"         raw thresholds → CUT>{raw_max_duration:.2f}s  COMPRESS>{raw_compress_duration:.2f}s")

    # ══════════════════════════════════════════════════════════════════════
    # METRIC 2: Speaking rate (words per second)
    # ══════════════════════════════════════════════════════════════════════
    speaking_rate   = 2.5   # fallback: average English speaking rate
    rate_multiplier = 1.0   # 1.0 = no adjustment

    if transcript_data is not None:
        all_words = []
        for seg in transcript_data.get('segments', []):
            all_words.extend(seg.get('words', []))

        if len(all_words) >= 2:
            total_speech_time = all_words[-1]['end'] - all_words[0]['start']
            if total_speech_time > 0:
                speaking_rate = len(all_words) / total_speech_time

        # Fast talkers (>3.5 w/s): tighten thresholds by up to 25%
        # Slow talkers (<2.0 w/s): loosen thresholds by up to 20%
        # Middle ground (2.0–3.5): no adjustment
        if speaking_rate > 3.5:
            rate_multiplier = max(0.75, 1.0 - (speaking_rate - 3.5) * 0.08)
        elif speaking_rate < 2.0:
            rate_multiplier = min(1.20, 1.0 + (2.0 - speaking_rate) * 0.10)

    print("      [Metric 2] Speaking rate")
    print(f"         {speaking_rate:.2f} words/sec  →  threshold multiplier: {rate_multiplier:.2f}x")

    # Apply speaking-rate multiplier to duration thresholds
    adj_max_duration      = raw_max_duration      * rate_multiplier
    adj_compress_duration = raw_compress_duration * rate_multiplier

    # ══════════════════════════════════════════════════════════════════════
    # METRIC 3: Sentence-boundary pause ratio
    # ══════════════════════════════════════════════════════════════════════
    boundary_ratio        = 0.0
    boundary_keep_boost   = 0.0   # extra seconds added to CUT threshold for boundary pauses

    if 'is_sentence_boundary' in pause_data.columns and len(pause_data) > 0:
        boundary_ratio = pause_data['is_sentence_boundary'].mean()  # fraction 0.0–1.0

        # If >40% of pauses are sentence boundaries, the creator clearly
        # uses them intentionally — give boundary pauses more protection
        if boundary_ratio > 0.4:
            boundary_keep_boost = round(boundary_ratio * 0.5, 2)   # up to +0.5s

    print("      [Metric 3] Sentence-boundary pause ratio")
    print(f"         {boundary_ratio:.1%} of pauses are at sentence boundaries")
    print(f"         boundary keep boost: +{boundary_keep_boost:.2f}s on CUT threshold")

    # ══════════════════════════════════════════════════════════════════════
    # METRIC 4: Cut frequency (pauses per minute of speech)
    # ══════════════════════════════════════════════════════════════════════
    cuts_per_minute       = 0.0
    frequency_multiplier  = 1.0

    if transcript_data is not None and len(pause_data) > 0:
        segs = transcript_data.get('segments', [])
        if segs:
            total_duration_mins = (segs[-1]['end'] - segs[0]['start']) / 60.0
            if total_duration_mins > 0:
                cuts_per_minute = len(pause_data) / total_duration_mins

        # High cut frequency (>15 cuts/min) → aggressive style → tighten 10%
        # Low cut frequency  (<5  cuts/min) → relaxed style   → loosen 10%
        if cuts_per_minute > 15:
            frequency_multiplier = 0.90
        elif cuts_per_minute < 5:
            frequency_multiplier = 1.10

    print("      [Metric 4] Cut frequency")
    print(f"         {cuts_per_minute:.1f} pauses/min  →  frequency multiplier: {frequency_multiplier:.2f}x")

    # ══════════════════════════════════════════════════════════════════════
    # Combine all metrics into final thresholds
    # ══════════════════════════════════════════════════════════════════════
    final_max_duration      = adj_max_duration      * frequency_multiplier
    final_compress_duration = adj_compress_duration * frequency_multiplier

    # Clamp to prevent absurd values from extreme reference videos
    final_max_duration      = round(max(0.4,  min(final_max_duration,      4.0)), 2)
    final_compress_duration = round(max(0.15, min(final_compress_duration, 2.0)), 2)

    # Ensure compress is always strictly less than max
    if final_compress_duration >= final_max_duration:
        final_compress_duration = round(final_max_duration * 0.6, 2)

    profile = {
        # Core thresholds used by decision_engine
        "max_duration":           final_max_duration,
        "compress_duration":      final_compress_duration,

        # Extra context passed to decision_engine for smarter decisions
        "speaking_rate":          round(speaking_rate, 2),
        "boundary_ratio":         round(boundary_ratio, 3),
        "boundary_keep_boost":    boundary_keep_boost,
        "cuts_per_minute":        round(cuts_per_minute, 1),

        # Human-readable summary for the terminal log
        "_summary": (
            f"CUT>{final_max_duration}s | COMPRESS>{final_compress_duration}s | "
            f"rate={speaking_rate:.1f}w/s | boundary={boundary_ratio:.0%} | "
            f"freq={cuts_per_minute:.0f}/min"
        )
    }

    print("\n   ✅ Final style profile:")
    print(f"      {profile['_summary']}")

    return profile


def _default_profile() -> dict:
    return {
        "max_duration":        1.5,
        "compress_duration":   0.8,
        "speaking_rate":       2.5,
        "boundary_ratio":      0.3,
        "boundary_keep_boost": 0.0,
        "cuts_per_minute":     8.0,
        "_summary":            "default balanced profile"
    }
