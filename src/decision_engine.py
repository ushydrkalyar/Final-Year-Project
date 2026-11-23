import pandas as pd

from filler_remover import is_hesitation


# ---------------------------------------------------------------------------
# Base scoring weights
# ---------------------------------------------------------------------------
HIGH_ENERGY_THRESHOLD = 0.01   # RMS above this = background noise during pause


def compress_target(style_profile: dict = None) -> float:
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


def classify_pauses(pause_df: pd.DataFrame, style_profile: dict, language: str = "en") -> pd.DataFrame:
    """
    Labels every detected pause as CUT, COMPRESS, or KEEP.

    Now uses all 4 metrics from the style profile:

    1. DURATION THRESHOLDS (max_duration, compress_duration)
       Base rule: long pauses get cut, medium ones compressed, short ones kept.

    2. SPEAKING RATE (speaking_rate)
       Fast-talking reference (>3.5 w/s): apply a tighter filler-word penalty.
       If the current word before the pause is a filler ("um", "uh", "like",
       hesitation sound) AND the reference creator speaks fast,
       we upgrade the decision one step more aggressively (KEEP→COMPRESS,
       COMPRESS→CUT). Fast creators don't tolerate fillers.

    3. SENTENCE-BOUNDARY RATIO + BOOST (boundary_ratio, boundary_keep_boost)
       If the reference creator consistently pauses at sentence boundaries,
       we give boundary pauses in the input video extra protection.
       The boost adds extra seconds to the CUT threshold specifically for
       boundary pauses — so a pause that would normally be CUT gets COMPRESSED,
       or a COMPRESS becomes KEEP.

    4. CUT FREQUENCY (cuts_per_minute → already baked into thresholds by
       style_extractor, but boundary_keep_boost counter-balances it here
       so we never over-cut natural sentence breaks even in aggressive profiles)

    Whatever the metrics say, a gap that sounds like speech (see speech_gaps.py)
    is kept: the transcript missed words there.

    Returns the input DataFrame with an added 'decision' column.
    """
    max_duration      = style_profile.get('max_duration',        1.5)
    compress_duration = style_profile.get('compress_duration',   0.8)
    keep_len          = compress_target(style_profile)
    speaking_rate     = style_profile.get('speaking_rate',       2.5)
    boundary_boost    = style_profile.get('boundary_keep_boost', 0.0)

    # Filler-word penalty is only activated for fast-speaking references
    apply_filler_penalty = speaking_rate > 3.5

    df = pause_df.copy()

    if df.empty:
        df['decision'] = pd.Series(dtype=str)
        return df

    decisions = []
    reasons   = []   # for the terminal log

    for _, row in df.iterrows():
        duration    = row['duration']
        rms_energy  = row['rms_energy']
        is_boundary    = row['is_sentence_boundary']
        prev_word      = str(row.get('previous_word', '')).lower().strip('.,!?')
        speech_density = row.get('speech_density', 0.0)

        # ── Step 1: Base decision from duration ──────────────────────────
        # For sentence-boundary pauses, apply the boost: treat the pause
        # as if it were shorter by boundary_boost seconds before comparing
        # to thresholds. This makes boundary pauses harder to cut.
        effective_duration = duration - boundary_boost if is_boundary else duration

        if effective_duration > max_duration:
            decision = 'CUT'
            reason   = f'duration {duration:.2f}s > max {max_duration}s'
        elif effective_duration > compress_duration:
            decision = 'COMPRESS'
            reason   = f'duration {duration:.2f}s > compress {compress_duration}s'
        else:
            decision = 'KEEP'
            reason   = f'duration {duration:.2f}s within keep range'

        if is_boundary and boundary_boost > 0:
            reason += f' (boundary boost -{boundary_boost}s applied)'

        # ── Step 2: Filler-word penalty (Metric 2 — speaking rate) ───────
        # Only active when the reference creator speaks fast (>3.5 w/s).
        # Pauses after filler words are hesitation gaps, not rhythm — cut them.
        if apply_filler_penalty and is_hesitation(prev_word, language):
            if decision == 'KEEP':
                decision = 'COMPRESS'
                reason  += f' → upgraded to COMPRESS (filler word "{prev_word}", fast ref)'
            elif decision == 'COMPRESS':
                decision = 'CUT'
                reason  += f' → upgraded to CUT (filler word "{prev_word}", fast ref)'

        # ── Step 3: High background energy penalty ────────────────────────
        # Significant audio energy during a "pause" usually means background
        # noise or music, not real silence. Be more aggressive cutting it.
        if rms_energy > HIGH_ENERGY_THRESHOLD and decision == 'KEEP' and not is_boundary:
            decision = 'COMPRESS'
            reason  += f' → upgraded to COMPRESS (high RMS {rms_energy:.4f})'

        # ── Step 4: Speech density modifier ──────────────────────────────
        # If the words around this pause are densely packed (>4 w/s), the
        # speaker is in a fast flow — a pause here is likely a breath or
        # emphasis, not dead air. Rescue COMPRESS → KEEP for dense speech.
        # Conversely, if speech is sparse (<1.5 w/s), be more aggressive.
        if speech_density > 4.0 and decision == 'COMPRESS' and is_boundary:
            decision = 'KEEP'
            reason  += f' → rescued to KEEP (high speech density {speech_density:.1f} w/s)'
        elif speech_density < 1.5 and decision == 'KEEP' and duration > compress_duration * 0.9:
            decision = 'COMPRESS'
            reason  += f' → downgraded to COMPRESS (low speech density {speech_density:.1f} w/s)'

        # ── Step 5: a COMPRESS must actually shorten the pause ───────────
        if decision == 'COMPRESS' and duration <= keep_len:
            decision = 'KEEP'
            reason  += f' → kept (already shorter than the {keep_len}s a compress leaves)'

        # ── Step 6: sound as loud as speech is not a pause ────────────────
        if row.get('sounds_like_speech', False):
            decision = 'KEEP'
            reason   = 'kept: as loud as speech, though no words were recognised'

        decisions.append(decision)
        reasons.append(reason)

    df['decision'] = decisions
    df['reason']   = reasons
    return df
