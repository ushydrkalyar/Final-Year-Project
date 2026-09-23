import subprocess
import json
import math
import pandas as pd
from filler_remover import FILLER_WORDS


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------
# Produces a structured quality report after every editing job.
# Every metric is computed from data already available in the pipeline —
# no extra model loads, no extra FFmpeg passes.
#
# The report is stored in job_store under 'evaluation' and returned by
# GET /status/{job_id} so the frontend can display it immediately.
# ---------------------------------------------------------------------------


def _ffprobe_duration(video_path: str) -> float:
    """Uses FFprobe to get the exact duration of a video file in seconds."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        video_path
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        data   = json.loads(result.stdout)
        return float(data.get('format', {}).get('duration', 0.0))
    except Exception:
        return 0.0


def _ffprobe_metadata(video_path: str) -> dict:
    """
    Extracts full video metadata: duration, resolution, fps, codec, bitrate.
    Returned in the evaluation report so the frontend can show file info.
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        video_path
    ]
    try:
        result  = subprocess.run(cmd, check=True, capture_output=True, text=True)
        data    = json.loads(result.stdout)
        fmt     = data.get('format', {})
        streams = data.get('streams', [])

        video_stream = next((s for s in streams if s.get('codec_type') == 'video'), {})
        audio_stream = next((s for s in streams if s.get('codec_type') == 'audio'), {})

        # Parse fps from r_frame_rate string like "30000/1001" or "25/1"
        fps = 0.0
        raw_fps = video_stream.get('r_frame_rate', '0/1')
        try:
            num, den = raw_fps.split('/')
            fps = round(int(num) / int(den), 2) if int(den) > 0 else 0.0
        except Exception:
            pass

        return {
            'duration_s':  round(float(fmt.get('duration', 0)), 2),
            'size_mb':     round(int(fmt.get('size', 0)) / 1024 / 1024, 2),
            'bitrate_kbps':round(int(fmt.get('bit_rate', 0)) / 1000, 1),
            'width':       video_stream.get('width',  0),
            'height':      video_stream.get('height', 0),
            'fps':         fps,
            'video_codec': video_stream.get('codec_name', 'unknown'),
            'audio_codec': audio_stream.get('codec_name', 'unknown'),
        }
    except Exception:
        return {}


def evaluate(
    input_video_path:  str,
    output_video_path: str,
    transcript_data:   dict,
    decisions_df:      pd.DataFrame,
    filler_cuts:       list,
    keep_segments:     list,
    style_profile:     dict,
    processing_time_s: float,
    active_filler_words: set = None,   # Bug 5 fix: language-specific filler set
) -> dict:
    """
    Computes a full quality report for a completed editing job.

    Parameters
    ----------
    input_video_path  : path to the original uploaded video
    output_video_path : path to the rendered output video
    transcript_data   : Whisper output dict (segments + words)
    decisions_df      : DataFrame from classify_pauses() with 'decision' column
    filler_cuts       : list of (start, end) tuples from detect_fillers()
    keep_segments     : list of (start, end) tuples from get_keep_segments()
    style_profile     : dict from calculate_style_profile() or preset
    processing_time_s : wall-clock seconds the pipeline took

    Returns
    -------
    dict with nested structure — stored in job_store['evaluation']
    """
    print("\n   📊 Running evaluation...")

    report = {}

    # ══════════════════════════════════════════════════════════════════════
    # 1. FILE METADATA
    # ══════════════════════════════════════════════════════════════════════
    input_meta  = _ffprobe_metadata(input_video_path)
    output_meta = _ffprobe_metadata(output_video_path)

    report['input_file']  = input_meta
    report['output_file'] = output_meta

    original_duration = input_meta.get('duration_s', 0.0)
    edited_duration   = output_meta.get('duration_s', 0.0)

    # ══════════════════════════════════════════════════════════════════════
    # 2. TIME SAVINGS
    # ══════════════════════════════════════════════════════════════════════
    time_saved_s       = max(0.0, original_duration - edited_duration)
    time_saved_pct     = (time_saved_s / original_duration * 100) if original_duration > 0 else 0.0
    size_reduction_pct = (
        (1 - output_meta.get('size_mb', 0) / input_meta.get('size_mb', 1)) * 100
        if input_meta.get('size_mb', 0) > 0 else 0.0
    )

    report['time_savings'] = {
        'original_duration_s':  round(original_duration,   2),
        'edited_duration_s':    round(edited_duration,      2),
        'time_saved_s':         round(time_saved_s,         2),
        'time_saved_pct':       round(time_saved_pct,       1),
        'size_reduction_pct':   round(size_reduction_pct,   1),
        'original_size_mb':     input_meta.get('size_mb',  0),
        'output_size_mb':       output_meta.get('size_mb', 0),
    }

    # ══════════════════════════════════════════════════════════════════════
    # 3. PAUSE STATISTICS
    # ══════════════════════════════════════════════════════════════════════
    if not decisions_df.empty:
        total_pauses    = len(decisions_df)
        cut_pauses      = decisions_df[decisions_df['decision'] == 'CUT']
        compress_pauses = decisions_df[decisions_df['decision'] == 'COMPRESS']
        keep_pauses     = decisions_df[decisions_df['decision'] == 'KEEP']

        total_pause_time   = decisions_df['duration'].sum()
        cut_pause_time     = cut_pauses['duration'].sum()     if not cut_pauses.empty     else 0.0
        compress_pause_time= compress_pauses['duration'].sum() if not compress_pauses.empty else 0.0

        # How much time COMPRESS actually saved (kept only 0.4s of each)
        from render_engine import COMPRESS_TARGET_DURATION
        compress_saved = max(0.0, compress_pause_time - (len(compress_pauses) * COMPRESS_TARGET_DURATION))

        avg_pause_original = decisions_df['duration'].mean() if total_pauses > 0 else 0.0

        # Average pause duration in the output (KEEP pauses stay, COMPRESS trimmed)
        kept_pause_time = (
            keep_pauses['duration'].sum() +
            len(compress_pauses) * COMPRESS_TARGET_DURATION
        ) if not keep_pauses.empty or not compress_pauses.empty else 0.0
        kept_pause_count = len(keep_pauses) + len(compress_pauses)
        avg_pause_edited = (kept_pause_time / kept_pause_count) if kept_pause_count > 0 else 0.0

        # Silence ratio: what fraction of original video was silence
        silence_ratio_original = (total_pause_time / original_duration * 100) if original_duration > 0 else 0.0
        silence_ratio_edited   = (kept_pause_time  / edited_duration   * 100) if edited_duration   > 0 else 0.0

        report['pauses'] = {
            'total_detected':        total_pauses,
            'cut':                   len(cut_pauses),
            'compressed':            len(compress_pauses),
            'kept':                  len(keep_pauses),
            'total_pause_time_s':    round(total_pause_time,        2),
            'cut_pause_time_s':      round(cut_pause_time,          2),
            'compress_saved_s':      round(compress_saved,          2),
            'avg_pause_original_s':  round(avg_pause_original,      3),
            'avg_pause_edited_s':    round(avg_pause_edited,        3),
            'silence_ratio_original_pct': round(silence_ratio_original, 1),
            'silence_ratio_edited_pct':   round(silence_ratio_edited,   1),
        }
    else:
        report['pauses'] = {
            'total_detected': 0, 'cut': 0, 'compressed': 0, 'kept': 0,
            'total_pause_time_s': 0, 'cut_pause_time_s': 0,
            'compress_saved_s': 0, 'avg_pause_original_s': 0,
            'avg_pause_edited_s': 0,
            'silence_ratio_original_pct': 0, 'silence_ratio_edited_pct': 0,
        }

    # ══════════════════════════════════════════════════════════════════════
    # 4. FILLER WORD STATISTICS
    # ══════════════════════════════════════════════════════════════════════
    all_words = []
    for seg in transcript_data.get('segments', []):
        all_words.extend(seg.get('words', []))

    filler_word_count  = 0
    filler_time_s      = 0.0
    filler_word_counts = {}   # { 'um': 4, 'like': 2, ... }

    # Bug 5 fix: use the language-specific filler set if provided,
    # otherwise fall back to the default English set from filler_remover.py
    fillers_to_check = active_filler_words if active_filler_words is not None else FILLER_WORDS

    for w in all_words:
        clean = w.get('word', '').strip().lower().strip('.,!?;:')
        if clean in fillers_to_check:
            filler_word_count += 1
            filler_time_s     += w.get('end', 0) - w.get('start', 0)
            filler_word_counts[clean] = filler_word_counts.get(clean, 0) + 1

    # filler_cuts are already merged — count the regions
    filler_regions_removed = len(filler_cuts)
    filler_time_removed    = sum(e - s for s, e in filler_cuts)

    report['fillers'] = {
        'total_filler_words_detected': filler_word_count,
        'filler_regions_removed':      filler_regions_removed,
        'filler_time_removed_s':       round(filler_time_removed, 2),
        'breakdown':                   filler_word_counts,
        'filler_rate_per_minute':      round(
            filler_word_count / (original_duration / 60), 1
        ) if original_duration > 0 else 0.0,
    }

    # ══════════════════════════════════════════════════════════════════════
    # 5. SPEECH / PACING METRICS
    # ══════════════════════════════════════════════════════════════════════
    total_words = len(all_words)

    # Words per minute before editing
    wpm_original = (total_words / (original_duration / 60)) if original_duration > 0 else 0.0

    # Words per minute after editing (same words, shorter video)
    wpm_edited   = (total_words / (edited_duration / 60))   if edited_duration   > 0 else 0.0

    # Transcription confidence: average Whisper word probability
    confidences = [
        w.get('probability', w.get('avg_logprob', 0))
        for seg in transcript_data.get('segments', [])
        for w in seg.get('words', [])
        if 'probability' in w or 'avg_logprob' in w
    ]
    avg_confidence = sum(confidences) / len(confidences) if confidences else None

    # Segment-level average log probability (Whisper's confidence signal)
    seg_logprobs = [
        seg.get('avg_logprob', 0)
        for seg in transcript_data.get('segments', [])
        if 'avg_logprob' in seg
    ]
    avg_logprob = sum(seg_logprobs) / len(seg_logprobs) if seg_logprobs else None

    report['speech'] = {
        'total_words':          total_words,
        'total_segments':       len(transcript_data.get('segments', [])),
        'wpm_original':         round(wpm_original, 1),
        'wpm_edited':           round(wpm_edited,   1),
        'wpm_increase_pct':     round(
            ((wpm_edited - wpm_original) / wpm_original * 100)
            if wpm_original > 0 else 0.0, 1
        ),
        'avg_transcription_logprob': round(avg_logprob, 3) if avg_logprob is not None else None,
        'transcription_quality': _logprob_to_quality(avg_logprob),
    }

    # ══════════════════════════════════════════════════════════════════════
    # 6. EDITING DECISIONS BREAKDOWN
    # ══════════════════════════════════════════════════════════════════════
    # Categorise what drove each CUT/COMPRESS decision (from 'reason' column)
    reason_categories = {
        'duration_threshold': 0,
        'filler_word_penalty': 0,
        'boundary_boost_rescue': 0,
        'speech_density_modifier': 0,
        'rms_energy_penalty': 0,
    }

    if not decisions_df.empty and 'reason' in decisions_df.columns:
        for reason in decisions_df['reason']:
            r = str(reason).lower()
            if 'filler word' in r:         reason_categories['filler_word_penalty']     += 1
            if 'boundary boost' in r:      reason_categories['boundary_boost_rescue']   += 1
            if 'speech density' in r:      reason_categories['speech_density_modifier'] += 1
            if 'rms' in r:                 reason_categories['rms_energy_penalty']       += 1
            if 'duration' in r and 'max' in r: reason_categories['duration_threshold']  += 1

    report['decision_breakdown'] = reason_categories

    # ══════════════════════════════════════════════════════════════════════
    # 7. OVERALL QUALITY SCORE  (0–100)
    # ══════════════════════════════════════════════════════════════════════
    # A composite score that gives the user a single headline number.
    # Weighted sum of four sub-scores, each 0–100:
    #
    #   a) Time saved score      (30%) — more time saved = higher score,
    #      but capped at 50% savings to avoid rewarding over-aggressive cuts
    #   b) Pacing improvement    (25%) — WPM increase up to 30% = full score
    #   c) Filler removal        (20%) — fewer fillers per minute = better
    #   d) Silence reduction     (25%) — silence ratio drop = cleaner video

    # a) Time saved: ideal range 10-40%, penalise <5% or >55%
    ts = report['time_savings']['time_saved_pct']
    if ts < 5:
        time_score = ts * 4          # 0-20
    elif ts <= 40:
        time_score = 20 + (ts - 5) * 80/35   # 20-100
    else:
        time_score = max(0, 100 - (ts - 40) * 3)  # penalise over-cutting

    # b) Pacing: WPM increase of 25% = full score
    wpm_inc = report['speech']['wpm_increase_pct']
    pacing_score = min(100, max(0, wpm_inc * 4))

    # c) Filler rate: 0 fillers/min = 100, 10+/min = 0
    filler_rate = report['fillers']['filler_rate_per_minute']
    filler_score = max(0, 100 - filler_rate * 10)

    # d) Silence reduction: silence dropped by 50% = full score
    sil_orig   = report['pauses']['silence_ratio_original_pct']
    sil_edited = report['pauses']['silence_ratio_edited_pct']
    sil_drop   = max(0, sil_orig - sil_edited)
    silence_score = min(100, sil_drop * 4) if sil_orig > 0 else 50

    overall = (
        time_score    * 0.30 +
        pacing_score  * 0.25 +
        filler_score  * 0.20 +
        silence_score * 0.25
    )
    overall = round(min(100, max(0, overall)), 1)

    report['quality_score'] = {
        'overall':        overall,
        'grade':          _score_to_grade(overall),
        'time_saved':     round(time_score,    1),
        'pacing':         round(pacing_score,  1),
        'filler_removal': round(filler_score,  1),
        'silence_reduction': round(silence_score, 1),
    }

    # ══════════════════════════════════════════════════════════════════════
    # 8. PROCESSING PERFORMANCE
    # ══════════════════════════════════════════════════════════════════════
    segments_rendered = len(keep_segments)
    realtime_factor   = (original_duration / processing_time_s) if processing_time_s > 0 else 0

    report['performance'] = {
        'processing_time_s':  round(processing_time_s, 1),
        'segments_rendered':  segments_rendered,
        'realtime_factor':    round(realtime_factor, 2),   # e.g. 0.5x means took 2× video length
        'style_preset_used':  style_profile.get('_summary', 'custom'),
    }

    # ── Terminal summary ──────────────────────────────────────────────────
    _print_report(report)

    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _logprob_to_quality(avg_logprob) -> str:
    """Converts Whisper's average log probability to a human-readable label."""
    if avg_logprob is None:
        return 'unknown'
    if avg_logprob > -0.2:
        return 'excellent'
    if avg_logprob > -0.5:
        return 'good'
    if avg_logprob > -0.8:
        return 'fair'
    return 'poor'


def _score_to_grade(score: float) -> str:
    if score >= 85: return 'A'
    if score >= 70: return 'B'
    if score >= 55: return 'C'
    if score >= 40: return 'D'
    return 'F'


def _fmt_time(seconds: float) -> str:
    """Formats seconds as Xm Ys for display."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _print_report(r: dict):
    """Prints a clean terminal summary of the evaluation report."""
    q  = r['quality_score']
    ts = r['time_savings']
    p  = r['pauses']
    f  = r['fillers']
    sp = r['speech']
    pf = r['performance']

    print(f"\n{'─'*60}")
    print(f"   📊 EVALUATION REPORT")
    print(f"{'─'*60}")
    print(f"   Overall Score:    {q['overall']}/100  (Grade {q['grade']})")
    print(f"{'─'*60}")
    print(f"   ⏱  Time Savings")
    print(f"      Original:  {_fmt_time(ts['original_duration_s'])}  →  "
          f"Edited: {_fmt_time(ts['edited_duration_s'])}")
    print(f"      Saved:     {_fmt_time(ts['time_saved_s'])}  ({ts['time_saved_pct']}%)")
    print(f"      File size: {ts['original_size_mb']}MB  →  {ts['output_size_mb']}MB  "
          f"({ts['size_reduction_pct']:.0f}% smaller)")
    print(f"   ✂️  Pauses")
    print(f"      Detected:  {p['total_detected']}  "
          f"(CUT:{p['cut']}  COMPRESS:{p['compressed']}  KEEP:{p['kept']})")
    print(f"      Silence:   {p['silence_ratio_original_pct']}% → {p['silence_ratio_edited_pct']}% of video")
    print(f"      Avg pause: {p['avg_pause_original_s']}s → {p['avg_pause_edited_s']}s")
    print(f"   🗑  Fillers")
    print(f"      Removed:   {f['total_filler_words_detected']} words in "
          f"{f['filler_regions_removed']} region(s) "
          f"({f['filler_time_removed_s']}s)")
    print(f"      Rate:      {f['filler_rate_per_minute']}/min")
    if f['breakdown']:
        top = sorted(f['breakdown'].items(), key=lambda x: -x[1])[:4]
        print(f"      Top:       {', '.join(f'{w}×{c}' for w,c in top)}")
    print(f"   🎙  Speech")
    print(f"      WPM:       {sp['wpm_original']} → {sp['wpm_edited']}  "
          f"(+{sp['wpm_increase_pct']}%)")
    print(f"      Quality:   {sp['transcription_quality']}")
    print(f"   ⚡ Performance")
    print(f"      Processed in {pf['processing_time_s']}s  "
          f"({pf['realtime_factor']}× realtime)")
    print(f"{'─'*60}\n")