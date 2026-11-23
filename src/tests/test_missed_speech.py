"""Speech the transcript skipped is never cut as silence."""
import wave

import numpy as np
import pytest

import decision_engine
import feature_extractor
import transcription_service

RATE = 16000
PROFILE = {"max_duration": 0.5, "compress_duration": 0.3, "speaking_rate": 4.0, "boundary_keep_boost": 0.0}


def recording(path, spoken, length=20, floor=0.002):
    t = np.arange(length * RATE) / RATE
    rng = np.random.default_rng(1)
    y = rng.normal(0, floor, len(t))
    for start, end in spoken:
        inside = (t >= start) & (t < end)
        # syllables: bursts with short dips, like speech
        y[inside] += 0.2 * np.sin(2 * np.pi * 180 * t[inside]) * (np.sin(2 * np.pi * 4 * t[inside]) > -0.3)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(RATE)
        out.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    return str(path)


def transcript(*spans):
    words = [{"word": " word", "start": at, "end": min(end, at + 0.5)}
             for start, end in spans for at in np.arange(start, end - 0.01, 0.5)]
    return {"language": "en", "segments": [{"start": words[0]["start"], "end": words[-1]["end"], "words": words}]}


def decided(said, path):
    pauses = feature_extractor.extract_pause_features(said, path)
    return list(decision_engine.classify_pauses(pauses, PROFILE)["decision"])


def test_a_skipped_stretch_is_kept(tmp_path):
    path = recording(tmp_path / "a.wav", [(1, 19)])
    assert decided(transcript((1, 5), (14, 19)), path) == ["KEEP"]


def test_real_silence_is_still_cut(tmp_path):
    path = recording(tmp_path / "a.wav", [(1, 5), (9, 19)])
    assert decided(transcript((1, 5), (9, 19)), path) == ["CUT"]


def test_room_noise_is_still_cut(tmp_path):
    path = recording(tmp_path / "a.wav", [(1, 5), (9, 19)], floor=0.02)
    assert decided(transcript((1, 5), (9, 19)), path) == ["CUT"]


@pytest.mark.parametrize("heard,expected", [((6, 19), (0.0, None)), ((1, 12), (0.9, None))])
def test_skipped_speech_at_either_edge_is_kept(tmp_path, heard, expected):
    path = recording(tmp_path / "a.wav", [(1, 19.9)])
    assert feature_extractor.detect_edge_silences(transcript(heard), path) == pytest.approx(expected)


def test_a_skipped_stretch_is_transcribed_again(tmp_path, monkeypatch):
    path = recording(tmp_path / "a.wav", [(1, 19)])
    whole = transcript((1, 5), (14, 19))

    class Skips:
        calls = []

        def transcribe(self, audio, **options):
            self.calls.append(options)
            if isinstance(audio, str):
                return whole
            return {"segments": [{"start": 0.5, "end": 1.2, "no_speech_prob": 0.05, "avg_logprob": -0.2,
                                  "words": [{"word": " found", "start": 0.5, "end": 1.2}]}]}
    monkeypatch.setattr(transcription_service, "_WHISPER_MODEL", Skips())
    got = transcription_service.transcribe_audio(path, "en")
    words = [w for s in got["segments"] for w in s["words"]]
    found = next(w for w in words if "found" in w["word"])
    assert 5 <= found["start"] < 14 and [w["start"] for w in words] == sorted(w["start"] for w in words)
    assert all(call["condition_on_previous_text"] is False for call in Skips.calls)
