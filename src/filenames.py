"""File names that are safe in paths, ffmpeg arguments and concat lists."""
import os
import re
import unicodedata

MAX_STEM_BYTES = 120        # leaves room for our prefixes inside the 255-byte limit


def _allowed(char: str) -> bool:
    # letters and digits of any script, plus the combining marks scripts like Devanagari need
    return char.isalnum() or char in "._ -" or unicodedata.category(char).startswith("M")


def safe_filename(name: str | None) -> str:
    """An uploaded file's name, reduced to something every tool in the pipeline accepts."""
    # Browsers send '"' and line breaks in multipart file names as these three escapes
    name = re.sub("%22|%0A|%0D", "_", name or "")
    base = os.path.basename(name.replace("\\", "/")) or "video.mp4"
    stem, ext = os.path.splitext(base)
    stem = "".join(c if _allowed(c) else "_" for c in stem).strip(" .")
    stem = stem.encode("utf-8")[:MAX_STEM_BYTES].decode("utf-8", "ignore").strip(" .") or "video"
    return stem + (ext if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", ext) else ".mp4")


def as_mp4(name: str | None) -> str:
    """``name`` with the extension of what we actually produce."""
    return os.path.splitext(name or "video")[0] + ".mp4"


def concat_entry(path: str) -> str:
    """One line of an ffmpeg concat list."""
    absolute = os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")
    return f"file '{absolute}'\n"
