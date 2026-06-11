import pandas as pd


# ---------------------------------------------------------------------------
# Base scoring weights
# ---------------------------------------------------------------------------
HIGH_ENERGY_THRESHOLD = 0.01   # RMS above this = background noise during pause


def classify_pauses(pause_df: pd.DataFrame, style_profile: dict) -> pd.DataFrame:
    """
    Labels every detected pause as CUT, COMPRESS, or KEEP.

    Now uses all 4 metrics from the style profile:

    1. DURATION THRESHOLDS (max_duration, compress_duration)
       Base rule: long pauses get cut, medium ones compressed, short ones kept.

    2. SPEAKING RATE (speaking_rate)
       Fast-talking reference (>3.5 w/s): apply a tighter filler-word penalty.
       If the current word before the pause is a filler ("um", "uh", "like",
       "you know", "so", "basically") AND the reference creator speaks fast,
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

    Returns the input DataFrame with an added 'decision' column.
    """
    max_duration      = style_profile.get('max_duration',        1.5)
    compress_duration = style_profile.get('compress_duration',   0.8)
    speaking_rate     = style_profile.get('speaking_rate',       2.5)
    boundary_boost    = style_profile.get('boundary_keep_boost', 0.0)

    # Filler-word penalty is only activated for fast-speaking references
    apply_filler_penalty = speaking_rate > 3.5

    FILLER_WORDS = {
        'um', 'uh', 'umm', 'uhh', 'hmm', 'er', 'err',
        'like', 'so', 'basically', 'literally', 'actually',
        'you know', 'i mean', 'right', 'okay', 'ok'
    }

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
        if apply_filler_penalty and prev_word in FILLER_WORDS:
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

        decisions.append(decision)
        reasons.append(reason)

    df['decision'] = decisions
    df['reason']   = reasons
    return df


if __name__ == "__main__":
    sample_data = pd.DataFrame([
        {'start_time': 1.0,  'end_time': 1.2,  'duration': 0.2,  'rms_energy': 0.001, 'is_sentence_boundary': False, 'previous_word': 'the',    'next_word': 'cat'},
        {'start_time': 2.5,  'end_time': 3.1,  'duration': 0.6,  'rms_energy': 0.002, 'is_sentence_boundary': False, 'previous_word': 'um',     'next_word': 'so'},
        {'start_time': 5.0,  'end_time': 6.0,  'duration': 1.0,  'rms_energy': 0.002, 'is_sentence_boundary': True,  'previous_word': 'done.',  'next_word': 'Now'},
        {'start_time': 8.0,  'end_time': 10.5, 'duration': 2.5,  'rms_energy': 0.001, 'is_sentence_boundary': False, 'previous_word': 'and',    'next_word': 'then'},
        {'start_time': 12.0, 'end_time': 12.9, 'duration': 0.9,  'rms_energy': 0.015, 'is_sentence_boundary': True,  'previous_word': 'right!', 'next_word': 'Let'},
        {'start_time': 15.0, 'end_time': 15.5, 'duration': 0.5,  'rms_energy': 0.001, 'is_sentence_boundary': False, 'previous_word': 'like',   'next_word': 'i'},
    ])

    print("=== TEST 1: Balanced profile (no reference) ===")
    balanced = {
        "max_duration": 1.5, "compress_duration": 0.8,
        "speaking_rate": 2.5, "boundary_keep_boost": 0.0
    }
    r1 = classify_pauses(sample_data, balanced)
    print(r1[['start_time', 'duration', 'previous_word', 'is_sentence_boundary', 'decision', 'reason']].to_string(index=False))

    print("\n=== TEST 2: Fast-talking reference (filler penalty active) ===")
    fast_ref = {
        "max_duration": 0.8, "compress_duration": 0.4,
        "speaking_rate": 4.2, "boundary_keep_boost": 0.3
    }
    r2 = classify_pauses(sample_data, fast_ref)
    print(r2[['start_time', 'duration', 'previous_word', 'is_sentence_boundary', 'decision', 'reason']].to_string(index=False))

    print("\n=== TEST 3: Podcast-style reference (protect sentence boundaries) ===")
    podcast = {
        "max_duration": 2.0, "compress_duration": 1.0,
        "speaking_rate": 1.8, "boundary_keep_boost": 0.5
    }
    r3 = classify_pauses(sample_data, podcast)
    print(r3[['start_time', 'duration', 'previous_word', 'is_sentence_boundary', 'decision', 'reason']].to_string(index=False))