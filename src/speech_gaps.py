"""Stretches with no transcribed words that sound like speech. They are not silence.

Whisper sometimes skips seconds, even a minute, of clear speech. The audio says so: those
stretches are as loud as the words around them, while real pauses almost never are.
Pure functions over 16 kHz mono samples.
"""
import numpy as np

RATE = 16000
MIN_GAP_S = 1.0         # shorter gaps are breaths and timing slack
LOUD_OF_SPEECH = 0.4    # of the median loudness inside words
LOUD_SHARE = 0.2        # measured: real pauses up to 0.04, skipped speech 0.40 and over
LOUD_S = 0.4

_FRAME = 480
_HOP = 160


class Loudness:
    """How loud a recording is, against its own words."""

    def __init__(self, samples: np.ndarray, words: list[dict]):
        count = max(0, (len(samples) - _FRAME) // _HOP + 1)
        energy = np.cumsum(np.insert(np.square(samples, dtype=np.float64), 0, 0.0))
        starts = np.arange(count) * _HOP
        self._rms = np.sqrt(np.maximum(0, energy[starts + _FRAME] - energy[starts]) / _FRAME)
        inside = [self._span(word["start"], word["end"]) for word in words]
        inside = np.concatenate(inside) if inside else np.zeros(0)
        self._threshold = LOUD_OF_SPEECH * float(np.median(inside)) if len(inside) else 0.0

    def _span(self, start: float, end: float) -> np.ndarray:
        return self._rms[max(0, int(start * RATE / _HOP)):max(0, int(end * RATE / _HOP))]

    def sounds_like_speech(self, start: float, end: float) -> bool:
        if end - start < MIN_GAP_S or not self._threshold:
            return False
        frames = self._span(start, end)
        loud = int((frames > self._threshold).sum())
        return loud * _HOP / RATE >= LOUD_S and loud >= LOUD_SHARE * len(frames)


def skipped(samples: np.ndarray, words: list[dict]) -> list[tuple[float, float]]:
    """The gaps between words, and at both ends, that hold speech."""
    if not words:
        return []
    loudness = Loudness(samples, words)
    starts = [0.0, *(word["end"] for word in words)]
    ends = [*(word["start"] for word in words), len(samples) / RATE]
    return [(a, b) for a, b in zip(starts, ends) if loudness.sounds_like_speech(a, b)]
