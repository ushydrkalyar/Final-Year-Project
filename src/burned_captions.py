"""Does an upload already have captions in its picture (an earlier export, a downloaded reel)?

Our own exports say so in their metadata. For anything else a few frames are read with OCR: captions
are the words being spoken, so writing that happens to be in shot (a whiteboard, a slide) doesn't count.
"""
import difflib
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import pytesseract

import media_probe

EXPORT_MARK = "cutsmart:captions"

SAMPLES = 6
WIDTH = 1280
NEARBY_S = 4.0          # a caption shows words said around then
MIN_WORD = 3            # shorter words match by chance
SAME_WORD = 0.84        # OCR misreads a letter here and there
MIN_IN_FRAME = 2        # spoken words in one frame for it to count
MIN_FRAMES = 2
MIN_WORDS = 4           # different spoken words read off the picture, over all the frames
_WORD = re.compile(r"[^\W\d_]+|\d+")


def marked(path: str) -> bool:
    return media_probe.comment(path) == EXPORT_MARK


def _plain(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if len(word) >= MIN_WORD}


def _frame(path: str, at: float) -> np.ndarray | None:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", path, "-map", "0:v:0", "-frames:v", "1",
         "-vf", f"scale={WIDTH}:-2", "-f", "image2pipe", "-c:v", "png", "-"],
        capture_output=True, timeout=30).stdout
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE) if raw else None


def _read(frame: np.ndarray) -> set[str]:
    found = set()
    # as it is, then only near-white and only dark: caption text is one or the other
    for image in (frame, cv2.threshold(frame, 215, 255, cv2.THRESH_BINARY_INV)[1],
                  cv2.threshold(frame, 60, 255, cv2.THRESH_BINARY)[1]):
        found |= _plain(pytesseract.image_to_string(image, config="--psm 11", timeout=8))
    return found


def _heard_in(picture: set[str], spoken: set[str]) -> set[str]:
    return {word for word in spoken
            if word in picture or (len(word) >= 5 and difflib.get_close_matches(word, picture, 1, SAME_WORD))}


def detect(path: str, transcript: dict) -> bool:
    """Never raises: when it can't tell, the answer is no."""
    try:
        if marked(path):
            return True
        words = [word for segment in transcript.get("segments", []) for word in segment.get("words", [])]
        if len(words) < MIN_WORDS:
            return False
        picked = {round(i * (len(words) - 1) / (SAMPLES - 1)) for i in range(SAMPLES)}

        def heard_at(at: float) -> set[str]:
            frame = _frame(path, at)
            if frame is None:
                return set()
            near = _plain(" ".join(word["word"] for word in words if abs(word["start"] - at) <= NEARBY_S))
            return _heard_in(_read(frame), near)

        moments = sorted((words[i]["start"] + words[i]["end"]) / 2 for i in picked)
        with ThreadPoolExecutor(SAMPLES, thread_name_prefix="ocr") as pool:     # tesseract is its own process
            captioned = [heard for heard in pool.map(heard_at, moments) if len(heard) >= MIN_IN_FRAME]
        return len(captioned) >= MIN_FRAMES and len(set().union(*captioned)) >= MIN_WORDS
    except Exception as exc:
        print(f"   ⚠️  Couldn't check for captions already in the picture: {exc}")
        return False
