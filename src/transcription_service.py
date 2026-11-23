import subprocess
import sys
import threading

import librosa
import whisper

import speech_gaps

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


# Not fed its own last sentence, Whisper gets stuck repeating (and skipping) far less
_OPTIONS = dict(word_timestamps=True, condition_on_previous_text=False, fp16=False)
_PAD_S = 0.2                # heard either side of a skipped stretch
_UNSURE_NO_SPEECH = 0.6     # a re-read segment like this is noise or music read as words
_UNSURE_LOGPROB = -1.0
_REREADS = 3                # one re-read can itself stop part-way


def _reread_skipped(audio_path, result, language):
    """Transcribe again, on its own, any stretch that sounds like speech but got no words."""
    def spoken():
        return [word for segment in result.get('segments', []) for word in segment.get('words', [])]
    if not spoken():
        return
    samples, rate = librosa.load(audio_path, sr=speech_gaps.RATE)
    tried = set()
    for _ in range(_REREADS):
        gaps = [gap for gap in speech_gaps.skipped(samples, spoken()) if gap not in tried]
        if not gaps:
            return
        tried.update(gaps)
        for start, end in gaps:
            offset = max(0.0, start - _PAD_S)
            part = samples[int(offset * rate):int((end + _PAD_S) * rate)]
            heard = _WHISPER_MODEL.transcribe(part, language=language, **_OPTIONS)
            for segment in heard.get('segments', []):
                if (segment.get('no_speech_prob', 0) > _UNSURE_NO_SPEECH
                        or segment.get('avg_logprob', 0) < _UNSURE_LOGPROB):
                    continue
                words = []
                for word in segment.get('words', []):
                    first, last = word['start'] + offset, word['end'] + offset
                    if start <= (first + last) / 2 <= end:
                        first, last = max(start, first), min(end, last)
                        if last > first:
                            words.append({**word, 'start': first, 'end': last})
                if words:
                    result['segments'].append({
                        **segment, 'start': words[0]['start'], 'end': words[-1]['end'], 'words': words,
                        'text': ''.join(word['word'] for word in words),
                    })
            _order_words(result)
        print(f"   🔁 Re-read {len(gaps)} stretch(es) the first pass skipped")


def _order_words(result):
    """Segments in time order, split where a re-read stretch falls inside one."""
    segments = sorted(result['segments'], key=lambda segment: segment['start'])
    starts, out = [segment['start'] for segment in segments], []
    for segment in segments:
        words, cuts = segment.get('words', []), [at for at in starts if segment['start'] < at < segment['end']]
        if not words or not cuts:
            out.append(segment)
            continue
        bounds = [float('-inf'), *cuts, float('inf')]
        for low, high in zip(bounds, bounds[1:]):
            part = [word for word in words if low <= word['start'] < high]
            if part:
                out.append({**segment, 'start': part[0]['start'], 'end': part[-1]['end'], 'words': part,
                            'text': ''.join(word['word'] for word in part)})
    result['segments'] = sorted(out, key=lambda segment: segment['start'])


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
            language = lang_code,   # None = auto-detect
            **_OPTIONS,
        )
        _reread_skipped(audio_path, result, lang_code or result.get('language'))

    for segment in result.get('segments', []):
        segment['words'] = join_word_fragments(segment.get('words', []))
    if lang_code:       # Whisper echoes the name it was given ("urdu"); the rest of the app uses codes
        result['language'] = language
    detected = result.get('language', 'unknown')
    print(f"   ✅ Language: {detected}")
    return result


def join_word_fragments(words: list) -> list:
    """Glue Whisper's split tokens back onto the word they belong to.

    Whisper often emits "in" + "-house" or a lone "," as separate words; shown
    as captions they read "in -house" or "word ,". A token that starts with a
    hyphen or has no letters/digits is appended to the previous word.
    """
    out = []
    for w in words:
        token = (w.get('word') or '').strip()
        fragment = token.startswith('-') or not any(ch.isalnum() for ch in token)
        if out and token and fragment:
            prev = out[-1]
            prev['word'] = prev['word'].rstrip() + token
            prev['end'] = w.get('end', prev.get('end'))
        else:
            out.append(dict(w))
    return out
