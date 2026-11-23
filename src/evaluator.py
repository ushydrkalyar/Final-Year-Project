import pandas as pd

import cut_list
import media_probe
from decision_engine import compress_target
from filler_remover import find_fillers


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


def _file_metadata(video_path: str) -> dict:
    """The file facts shown in the evaluation report."""
    found = media_probe.info(video_path)
    if not found:
        return {}
    return {
        'duration_s':  round(found.duration, 2),
        'size_mb':     round(found.size_bytes / 1024 / 1024, 2),
        'bitrate_kbps':round(found.bit_rate / 1000, 1),
        'width':       found.width,
        'height':      found.height,
        'fps':         found.fps,
        'video_codec': found.codec,
        'audio_codec': found.audio_codec,
    }


MIN_PAUSE_S = 0.2   # gaps between words shorter than this are just articulation


def _silence_stats(transcript_data, keep_segments, original_duration, edited_duration) -> dict:
    """Silence share and mean pause, before and after the edit, from word timings.

    Silence = time not covered by any spoken word. On the edited side only the
    kept parts of the original count, so every cut can only lower it.
    """
    words = sorted((float(w['start']), float(w['end']))
                   for seg in transcript_data.get('segments', []) for w in seg.get('words', [])
                   if w.get('end') is not None and float(w['end']) > float(w['start']))
    speech = []                       # words joined across articulation gaps
    for ws, we in words:
        if speech and ws - speech[-1][1] < MIN_PAUSE_S:
            speech[-1] = (speech[-1][0], max(speech[-1][1], we))
        else:
            speech.append((ws, we))
    speech = cut_list.normalize(speech, original_duration)
    keep = cut_list.normalize(keep_segments or [(0.0, original_duration)], original_duration)

    def covered(a, b):
        return sum(max(0.0, min(b, e) - max(a, s)) for s, e in speech)

    speech_total = sum(e - s for s, e in speech)
    kept_len = sum(e - s for s, e in keep)
    kept_speech = sum(covered(s, e) for s, e in keep)

    def mean_gap(times):
        gaps = [b[0] - a[1] for a, b in zip(times, times[1:]) if b[0] - a[1] >= MIN_PAUSE_S]
        return sum(gaps) / len(gaps) if gaps else 0.0

    edited, offset = [], 0.0          # word times mapped onto the edited timeline
    for s, e in keep:
        edited += [(offset + max(ws, s) - s, offset + min(we, e) - s) for ws, we in words if we > s and ws < e]
        offset += e - s
    return {
        'silence_ratio_original': 100 * max(0.0, original_duration - speech_total) / original_duration
                                  if original_duration > 0 else 0.0,
        'silence_ratio_edited': 100 * max(0.0, kept_len - kept_speech) / kept_len if kept_len > 0 else 0.0,
        'avg_pause_original': mean_gap(words),
        'avg_pause_edited': mean_gap(edited),
    }


def evaluate(
    input_video_path:  str,
    output_video_path: str,
    transcript_data:   dict,
    decisions_df:      pd.DataFrame,
    filler_cuts:       list,
    keep_segments:     list,
    style_profile:     dict,
    processing_time_s: float,
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
    input_meta  = _file_metadata(input_video_path)
    output_meta = _file_metadata(output_video_path)

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

        # How much time COMPRESS actually saved (each keeps the style's compress target)
        keep_len = compress_target(style_profile)
        compress_saved = float((compress_pauses['duration'] - keep_len).clip(lower=0).sum()) \
            if not compress_pauses.empty else 0.0

        # Silence and pause lengths measured from the words and the actual cut
        # list, so leading/trailing silence counts and the edit can't look worse.
        sil = _silence_stats(transcript_data, keep_segments, original_duration, edited_duration)
        avg_pause_original = sil['avg_pause_original']
        avg_pause_edited = sil['avg_pause_edited']
        silence_ratio_original = sil['silence_ratio_original']
        silence_ratio_edited = sil['silence_ratio_edited']

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
        sil = _silence_stats(transcript_data, keep_segments, original_duration, edited_duration)
        report['pauses'] = {
            'total_detected': 0, 'cut': 0, 'compressed': 0, 'kept': 0,
            'total_pause_time_s': 0, 'cut_pause_time_s': 0, 'compress_saved_s': 0,
            'avg_pause_original_s': round(sil['avg_pause_original'], 3),
            'avg_pause_edited_s': round(sil['avg_pause_edited'], 3),
            'silence_ratio_original_pct': round(sil['silence_ratio_original'], 1),
            'silence_ratio_edited_pct': round(sil['silence_ratio_edited'], 1),
        }

    # ══════════════════════════════════════════════════════════════════════
    # 4. FILLER WORD STATISTICS
    # ══════════════════════════════════════════════════════════════════════
    all_words = []
    for seg in transcript_data.get('segments', []):
        all_words.extend(seg.get('words', []))

    found = find_fillers(transcript_data)   # the same rules, and the same language, that made the cuts
    filler_word_count  = len(found)
    filler_word_counts = {}   # { 'um': 4, 'i mean': 2, ... }
    for f in found:
        filler_word_counts[f['text']] = filler_word_counts.get(f['text'], 0) + 1

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


def _weakest(qualities: list[str]) -> str:
    known = [q for q in ('poor', 'fair', 'good', 'excellent') if q in qualities]
    return known[0] if known else 'unknown'


def aggregate(reports: list[dict], original_duration: float, edited_duration: float) -> dict:
    """A combined video's report, from its clips' reports. Empty when none was evaluated."""
    reports = [r for r in reports if r]

    def values(section, key):
        found = (r.get(section, {}).get(key) for r in reports)
        return [v for v in found if v is not None]

    def mean(numbers):
        return round(sum(numbers) / len(numbers), 1) if numbers else None

    scores = values('quality_score', 'overall')
    if not scores:
        return {}
    score = mean(scores)
    saved = sum(values('time_savings', 'time_saved_s'))
    before = mean([v for v in values('speech', 'wpm_original') if v])     # a clip without speech has no pace
    after = mean([v for v in values('speech', 'wpm_edited') if v])
    return {
        'quality_score': {'overall': score, 'grade': _score_to_grade(score)},
        'time_savings': {
            'time_saved_s':        round(saved, 2),
            'time_saved_pct':      round(saved / original_duration * 100, 1) if original_duration > 0 else 0,
            'original_duration_s': round(original_duration, 2),
            'edited_duration_s':   round(edited_duration, 2),
        },
        'speech': {'wpm_original': before, 'wpm_edited': after,
                   'wpm_increase_pct': round((after - before) / before * 100, 1) if before and after else None,
                   'transcription_quality': _weakest(values('speech', 'transcription_quality'))},
        'fillers': {'total_filler_words_detected': sum(values('fillers', 'total_filler_words_detected'))},
        'pauses': {
            'cut':                        sum(values('pauses', 'cut')),
            'compressed':                 sum(values('pauses', 'compressed')),
            'kept':                       sum(values('pauses', 'kept')),
            'total_detected':             sum(values('pauses', 'total_detected')),
            'silence_ratio_original_pct': mean(values('pauses', 'silence_ratio_original_pct')),
            'silence_ratio_edited_pct':   mean(values('pauses', 'silence_ratio_edited_pct')),
        },
    }


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
    print("   📊 EVALUATION REPORT")
    print(f"{'─'*60}")
    print(f"   Overall Score:    {q['overall']}/100  (Grade {q['grade']})")
    print(f"{'─'*60}")
    print("   ⏱  Time Savings")
    print(f"      Original:  {_fmt_time(ts['original_duration_s'])}  →  "
          f"Edited: {_fmt_time(ts['edited_duration_s'])}")
    print(f"      Saved:     {_fmt_time(ts['time_saved_s'])}  ({ts['time_saved_pct']}%)")
    print(f"      File size: {ts['original_size_mb']}MB  →  {ts['output_size_mb']}MB  "
          f"({ts['size_reduction_pct']:.0f}% smaller)")
    print("   ✂️  Pauses")
    print(f"      Detected:  {p['total_detected']}  "
          f"(CUT:{p['cut']}  COMPRESS:{p['compressed']}  KEEP:{p['kept']})")
    print(f"      Silence:   {p['silence_ratio_original_pct']}% → {p['silence_ratio_edited_pct']}% of video")
    print(f"      Avg pause: {p['avg_pause_original_s']}s → {p['avg_pause_edited_s']}s")
    print("   🗑  Fillers")
    print(f"      Removed:   {f['total_filler_words_detected']} words in "
          f"{f['filler_regions_removed']} region(s) "
          f"({f['filler_time_removed_s']}s)")
    print(f"      Rate:      {f['filler_rate_per_minute']}/min")
    if f['breakdown']:
        top = sorted(f['breakdown'].items(), key=lambda x: -x[1])[:4]
        print(f"      Top:       {', '.join(f'{w}×{c}' for w,c in top)}")
    print("   🎙  Speech")
    print(f"      WPM:       {sp['wpm_original']} → {sp['wpm_edited']}  "
          f"(+{sp['wpm_increase_pct']}%)")
    print(f"      Quality:   {sp['transcription_quality']}")
    print("   ⚡ Performance")
    print(f"      Processed in {pf['processing_time_s']}s  "
          f"({pf['realtime_factor']}× realtime)")
    print(f"{'─'*60}\n")
