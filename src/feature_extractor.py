import librosa
import numpy as np
import pandas as pd


def extract_pause_features(transcription_data, audio_path, min_pause_duration=0.2):
    """
    Finds pauses between words and extracts audio features for each.

    FIX: Returns a DataFrame with guaranteed columns even when no pauses are
    detected, preventing .sum() / column-access crashes in app.py.
    """
    # These are the columns app.py and decision_engine.py depend on.
    # Always return a DataFrame with these columns — never a bare empty one.
    COLUMNS = [
        'start_time', 'end_time', 'duration',
        'rms_energy', 'is_sentence_boundary',
        'previous_word', 'next_word',
        'speech_density'
    ]

    print("   Loading audio for signal analysis...")
    y, sr = librosa.load(audio_path, sr=16000)

    # Flatten all words from every segment into one list
    all_words = []
    for segment in transcription_data.get('segments', []):
        all_words.extend(segment.get('words', []))

    print(f"   Analyzing {len(all_words)} words for pauses...")

    # FIX: Guard against empty transcripts early
    if len(all_words) < 2:
        print("   ⚠️  Not enough words to detect pauses.")
        return pd.DataFrame(columns=COLUMNS)

    pauses = []

    for i in range(len(all_words) - 1):
        current_word = all_words[i]
        next_word    = all_words[i + 1]

        gap_start = current_word['end']
        gap_end   = next_word['start']
        duration  = gap_end - gap_start

        # Skip gaps that are too short or negative (Whisper timestamp quirks)
        if duration < min_pause_duration:
            continue

        # --- Feature 1: Sentence boundary ---
        word_text = current_word['word'].strip()
        is_sentence_boundary = word_text.endswith(('.', '?', '!'))

        # --- Feature 2: RMS audio energy during the pause ---
        start_sample = int(gap_start * sr)
        end_sample   = int(gap_end   * sr)
        pause_audio  = y[start_sample:end_sample]

        if len(pause_audio) > 0:
            rms_energy = float(np.sqrt(np.mean(pause_audio ** 2)))
        else:
            rms_energy = 0.0

        # Feature 3: Speech density — words/sec in 5-word window around pause
        # High density = fast speech = pause is likely intentional breath
        # Low density  = slow speech = pause may be unnecessary dead air
        window           = 5
        win_start        = max(0, i - window)
        win_end          = min(len(all_words) - 1, i + 1 + window)
        win_words        = all_words[win_start:win_end + 1]
        if len(win_words) >= 2:
            win_dur      = win_words[-1]['end'] - win_words[0]['start']
            speech_density = len(win_words) / win_dur if win_dur > 0 else 0.0
        else:
            speech_density = 0.0

        pauses.append({
            'start_time':           round(gap_start, 3),
            'end_time':             round(gap_end,   3),
            'duration':             round(duration,  3),
            'rms_energy':           round(rms_energy, 4),
            'is_sentence_boundary': is_sentence_boundary,
            'previous_word':        word_text,
            'next_word':            next_word['word'].strip(),
            'speech_density':       round(float(speech_density), 3),
        })

    if not pauses:
        print("   ⚠️  No pauses found above the minimum threshold.")
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(pauses, columns=COLUMNS)
    return df


if __name__ == "__main__":
    from transcription_service import extract_audio, transcribe_audio

    video_file = "test_video.mp4"
    audio_file = "extracted_audio.wav"

    if extract_audio(video_file, audio_file):
        transcript = transcribe_audio(audio_file)
        pause_data = extract_pause_features(transcript, audio_file)

        if not pause_data.empty:
            print("\nDetected Pauses:")
            print(pause_data.to_string(index=False))
        else:
            print("\nNo pauses detected.")


# ---------------------------------------------------------------------------
# Leading and trailing silence detection
# ---------------------------------------------------------------------------

def detect_edge_silences(transcript_data: dict, audio_path: str,
                         min_silence: float = 0.3) -> tuple:
    """
    Detects silence at the START and END of the video that falls outside
    the word-gap detection range of extract_pause_features().

    Returns (leading_cut_end, trailing_cut_start):
      - leading_cut_end:    seconds — cut from 0.0 to this point
      - trailing_cut_start: seconds — cut from this point to end of video,
                             or None if there's no significant trailing
                             silence (caller should not add a trailing cut).

    min_silence: minimum duration in seconds to bother cutting.
                 Silences shorter than this are left alone.
    """
    import numpy as np

    all_words = []
    for segment in transcript_data.get('segments', []):
        all_words.extend(segment.get('words', []))

    if not all_words:
        return (0.0, None)

    first_word_start = all_words[0].get('start', 0.0)
    last_word_end    = all_words[-1].get('end',   0.0)

    # Leading silence: time from 0 to first word
    leading_duration = first_word_start

    leading_cut_end = 0.0
    if leading_duration >= min_silence:
        # Keep a tiny 0.1s buffer before the first word
        # so it doesn't feel abrupt
        leading_cut_end = max(0.0, first_word_start - 0.1)
        print(f"   ✂️  Leading silence detected: {leading_duration:.2f}s "
              f"→ cutting 0.0s to {leading_cut_end:.2f}s")
    else:
        print(f"   ✅ No significant leading silence ({leading_duration:.2f}s < {min_silence}s threshold)")

    # Trailing silence: time from the last spoken word to the actual end
    # of the audio track. Whisper's transcript only tells us when speech
    # *stops* — it says nothing about how much dead air follows (the
    # speaker could pause for 10s before the recording is cut, or the
    # camera could simply keep rolling after they finish talking). That
    # has to be measured against the real audio duration, not assumed.
    trailing_cut_start = None
    try:
        real_duration = librosa.get_duration(path=audio_path)
    except TypeError:
        # older librosa versions use the `filename` kwarg instead of `path`
        real_duration = librosa.get_duration(filename=audio_path)
    except Exception as e:
        print(f"   ⚠️  Could not read audio duration for trailing-silence check: {e}")
        real_duration = None

    if real_duration is not None:
        trailing_duration = real_duration - last_word_end
        if trailing_duration >= min_silence:
            # Keep a tiny 0.1s buffer after the last word so the cut
            # doesn't feel abrupt, same as the leading-silence buffer.
            trailing_cut_start = min(real_duration, last_word_end + 0.1)
            print(f"   ✂️  Trailing silence detected: {trailing_duration:.2f}s "
                  f"→ cutting {trailing_cut_start:.2f}s to {real_duration:.2f}s")
        else:
            print(f"   ✅ No significant trailing silence "
                  f"({trailing_duration:.2f}s < {min_silence}s threshold)")

    return (leading_cut_end, trailing_cut_start)
