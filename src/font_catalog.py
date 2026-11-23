"""Which font files render each caption font family, for the browser's live preview.

The export burns captions with ffmpeg's libass, which finds fonts in ``fonts/``
(bundled, listed in fonts/fonts.json) and, for a few template fonts, in the
operating system via fontconfig. The browser preview runs libass too, so it
must load the very same files: this module maps family -> file URLs and serves
the allowlisted system files.

Note: serving OS fonts suits a local/academic deployment; a public deployment
needs web-embedding rights for them, or open replacements.
"""
import functools
import json
import os
import subprocess

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
SYSTEM_FAMILIES = ("Arial", "Arial Black", "Impact", "Georgia")  # template fonts not bundled in fonts/
_FACES = ("", ":bold", ":italic", ":bold:italic")


def _fc_match(pattern: str):
    try:
        out = subprocess.run(["fc-match", "-f", "%{family}\t%{file}", pattern],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    family, _, path = out.partition("\t")
    return family, path


@functools.lru_cache(maxsize=1)
def _system_files() -> dict:
    """{filename: (family, absolute path)} for the faces of SYSTEM_FAMILIES installed here."""
    files = {}
    for fam in SYSTEM_FAMILIES:
        for face in _FACES:
            hit = _fc_match(fam + face)
            if hit and hit[0].split(",")[0].strip().lower() == fam.lower() and os.path.isfile(hit[1]):
                files[os.path.basename(hit[1])] = (fam, hit[1])
    return files


@functools.lru_cache(maxsize=1)
def catalog() -> dict:
    """{family in lower case: [font file URLs]} for every family captions can use."""
    out = {}
    try:
        with open(os.path.join(FONTS_DIR, "fonts.json"), encoding="utf-8") as f:
            for entry in json.load(f):
                out[entry["family"].lower()] = [f"/fonts/{x['file']}" for x in entry.get("files", [])]
    except (OSError, ValueError, KeyError):
        pass
    for name, (fam, _) in _system_files().items():
        out.setdefault(fam.lower(), []).append(f"/system-fonts/{name}")
    return out


def files(family: str) -> list[str]:
    """The files on disk that render ``family``: the ones catalog() serves the browser."""
    out = []
    for url in catalog().get(family.strip().lower(), []):
        name = url.rsplit("/", 1)[-1]
        path = system_font_path(name) if url.startswith("/system-fonts/") else os.path.join(FONTS_DIR, name)
        if path and os.path.isfile(path):
            out.append(path)
    return out


def system_font_path(filename: str):
    """Absolute path of an allowlisted system font file, or None."""
    hit = _system_files().get(filename)
    return hit[1] if hit else None
