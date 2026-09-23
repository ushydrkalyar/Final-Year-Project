import subprocess
import os
import sys
import threading
import whisper

# Windows consoles often default to a legacy code page such as cp1252. The
# application logs Unicode status symbols, so use UTF-8 before the first log.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# ---------------------------------------------------------------------------
# Load Whisper model ONCE at startup — shared across all threads safely.
# Whisper's .transcribe() is NOT thread-safe, so we protect it with a lock.
# Parallel clip pipelines will queue at the transcription step and proceed
# in parallel for all other steps (FFmpeg, feature extraction, rendering).
# ---------------------------------------------------------------------------
print("Loading Whisper model (one-time startup)...")
_WHISPER_MODEL = whisper.load_model("base")
_WHISPER_LOCK  = threading.Lock()
print("✅ Whisper model ready.")

# Whisper language codes for the most common languages.
# Full list: https://github.com/openai/whisper/blob/main/whisper/tokenizer.py
SUPPORTED_LANGUAGES = {
    'auto':  None,        # Whisper auto-detects (default)
    'en':    'english',
    'ur':    'urdu',
    'ar':    'arabic',
    'hi':    'hindi',
    'zh':    'chinese',
    'fr':    'french',
    'de':    'german',
    'es':    'spanish',
    'it':    'italian',
    'pt':    'portuguese',
    'ru':    'russian',
    'ja':    'japanese',
    'ko':    'korean',
    'tr':    'turkish',
    'fa':    'persian',
}

# Language-specific filler words.
# Used by filler_remover.py when language is specified.
FILLER_WORDS_BY_LANGUAGE = {
    'en': {
        'um','uh','umm','uhh','hmm','hm','er','err','ah','ahh',
        'like','basically','literally','actually','honestly',
        'right','okay','ok','so','well','anyway','anyways',
        'you know','i mean','kind of','sort of','i guess',
    },
    'ur': {
        'matlab','yani','acha','han','haan','toh','phir',
        'woh','bas','agar','lekin','aur','bhi','hi',
        'na','nah','hmm','uh','um',
    },
    'ar': {
        'يعني','اممم','اهه','طيب','حسنا','اوكي','صح',
        'يلا','هممم','اه','اوه','عم','مم',
    },
    'hi': {
        'matlab','yani','acha','han','haan','toh','phir',
        'woh','bas','lekin','aur','bhi','hi','na','hmm',
        'um','uh','theek','sahi',
    },
}


def extract_audio(video_path, output_audio_path):
    """Extracts audio from a video file using FFmpeg."""
    print(f"Extracting audio from '{video_path}'...")
    command = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        output_audio_path
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        print(f"   ✅ Audio extracted to '{output_audio_path}'")
        return True
    except subprocess.CalledProcessError as e:
        print(f"   ❌ FFmpeg error:\n{e.stderr}")
        return False
    except FileNotFoundError:
        print("   ❌ FFmpeg not found. Install it and add to PATH.")
        return False


def transcribe_audio(audio_path, language: str = 'auto'):
    """
    Transcribes audio using the pre-loaded Whisper model.

    language: ISO 639-1 code ('en', 'ur', 'ar', 'hi', etc.) or 'auto'.
              Specifying the language is faster (skips auto-detection)
              and more accurate for non-English content.
    """
    lang_code = SUPPORTED_LANGUAGES.get(language, None)

    if lang_code:
        print(f"   Transcribing '{audio_path}' [{lang_code}]...")
    else:
        print(f"   Transcribing '{audio_path}' [auto-detect]...")

    # Lock ensures only one clip transcribes at a time — Whisper is not
    # thread-safe. All other pipeline steps (FFmpeg, feature extraction,
    # rendering) run fully in parallel across clips.
    with _WHISPER_LOCK:
        result = _WHISPER_MODEL.transcribe(
            audio_path,
            word_timestamps = True,
            language        = lang_code,   # None = auto-detect
        )

    detected = result.get('language', 'unknown')
    print(f"   ✅ Language: {detected}")
    return result


def get_filler_words(language: str) -> set:
    """
    Returns the filler word set for the detected/specified language.
    Falls back to English if no filler list exists for that language.
    """
    return FILLER_WORDS_BY_LANGUAGE.get(language, FILLER_WORDS_BY_LANGUAGE['en'])


if __name__ == "__main__":
    video_file = "test_video.mp4"
    audio_file = "extracted_audio.wav"

    if not os.path.exists(video_file):
        print(f"Error: '{video_file}' not found.")
    else:
        success = extract_audio(video_file, audio_file)
        if success:
            transcription_data = transcribe_audio(audio_file, language='auto')
            print("\n--- Transcription Results ---")
            for segment in transcription_data.get('segments', []):
                for word in segment.get('words', []):
                    print(f"Word: '{word['word'].strip():<15}' | "
                          f"Start: {word['start']:>5.2f}s | End: {word['end']:>5.2f}s")
