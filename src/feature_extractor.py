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