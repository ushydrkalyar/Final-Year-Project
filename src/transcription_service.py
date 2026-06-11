import subprocess
import os
import whisper

# ---------------------------------------------------------------------------
# FIX: Load Whisper model ONCE at module import time, not on every call.
# Loading takes 10-15s and allocates ~500MB RAM each time.
# The module-level variable is shared across all threads safely because
# Whisper's transcribe() is stateless (it doesn't modify the model object).
# ---------------------------------------------------------------------------
print("Loading Whisper model (one-time startup)...")
_WHISPER_MODEL = whisper.load_model("base")
print("✅ Whisper model ready.")


def extract_audio(video_path, output_audio_path):
    """Extracts audio from a video file using FFmpeg."""
    print(f"Extracting audio from '{video_path}'...")

    command = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        output_audio_path
    ]

    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"   ✅ Audio extracted to '{output_audio_path}'")
        return True
    except subprocess.CalledProcessError as e:
        print(f"   ❌ FFmpeg error:\n{e.stderr.decode()}")
        return False
    except FileNotFoundError:
        print("   ❌ FFmpeg not found. Install it and add to PATH.")
        return False


def transcribe_audio(audio_path):
    """Transcribes audio using the pre-loaded Whisper model."""
    print(f"   Transcribing '{audio_path}'...")
    result = _WHISPER_MODEL.transcribe(audio_path, word_timestamps=True)
    return result


if __name__ == "__main__":
    video_file = "test_video.mp4"
    audio_file = "extracted_audio.wav"

    if not os.path.exists(video_file):
        print(f"Error: '{video_file}' not found.")
    else:
        success = extract_audio(video_file, audio_file)
        if success:
            transcription_data = transcribe_audio(audio_file)
            print("\n--- Transcription Results ---")
            for segment in transcription_data.get('segments', []):
                for word in segment.get('words', []):
                    clean_word = word['word'].strip()
                    print(f"Word: '{clean_word:<15}' | Start: {word['start']:>5.2f}s | End: {word['end']:>5.2f}s")