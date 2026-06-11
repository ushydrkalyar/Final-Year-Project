import pandas as pd


# ---------------------------------------------------------------------------
# Filler words to detect and remove from speech
# ---------------------------------------------------------------------------
FILLER_WORDS = {
    # Hesitation sounds
    'um', 'uh', 'umm', 'uhh', 'hmm', 'hm', 'er', 'err', 'ah', 'ahh',
    # Verbal fillers
    'like', 'basically', 'literally', 'actually', 'honestly',
    'right', 'okay', 'ok', 'so', 'well', 'anyway', 'anyways',
    'you know', 'i mean', 'kind of', 'sort of', 'i guess',
}

# A filler is only removed if it appears as a standalone word (not inside a
# longer word like "Somalia" or "meaningful"). We check the cleaned word
# with punctuation stripped rather than doing substring matching.


def detect_fillers(transcript_data: dict) -> list:
    """
    Scans the Whisper transcript for filler words and returns a list of
    (start, end) timestamp tuples for every filler word found.

    These are SPEECH segments to cut — not pauses — so they are handled
    separately from pause_df and merged in render_engine.get_keep_segments().

    Strategy:
      1. Walk every word in the transcript.
      2. If the word (stripped of punctuation, lowercased) matches a filler,
         record its (start, end) timestamps.
      3. Merge adjacent fillers that are within 0.1s of each other so that
         "um, uh," doesn't produce two tiny cuts with a 0.05s blip between them.

    Returns: list of (start_time, end_time) tuples, sorted by start_time.
    """
    all_words = []
    for segment in transcript_data.get('segments', []):
        all_words.extend(segment.get('words', []))

    if not all_words:
        return []

    raw_cuts = []

    for word_data in all_words:
        word_text  = word_data.get('word', '').strip()
        word_clean = word_text.lower().strip('.,!?;:"\'-()[]')

        if word_clean in FILLER_WORDS:
            start = word_data.get('start', 0.0)
            end   = word_data.get('end',   0.0)
            if end > start:   # skip zero-length Whisper quirks
                raw_cuts.append((round(start, 3), round(end, 3)))
                print(f"   🗑️  Filler detected: '{word_text}' at {start:.2f}s–{end:.2f}s")

    if not raw_cuts:
        return []

    # ── Merge adjacent filler cuts ──────────────────────────────────────
    # If two fillers are within MERGE_GAP seconds of each other, merge them
    # into one cut so we don't produce sub-100ms clips between them.
    MERGE_GAP   = 0.15
    merged      = [raw_cuts[0]]

    for start, end in raw_cuts[1:]:
        prev_start, prev_end = merged[-1]
        if start - prev_end <= MERGE_GAP:
            merged[-1] = (prev_start, end)   # extend previous cut
        else:
            merged.append((start, end))

    print(f"   ✅ Filler cuts: {len(raw_cuts)} words → {len(merged)} cut region(s)")
    return merged


if __name__ == "__main__":
    from transcription_service import extract_audio, transcribe_audio

    video_file = "test_video.mp4"
    audio_file = "extracted_audio.wav"

    if extract_audio(video_file, audio_file):
        transcript = transcribe_audio(audio_file)
        cuts = detect_fillers(transcript)
        print(f"\nFiller cuts to apply: {cuts}")