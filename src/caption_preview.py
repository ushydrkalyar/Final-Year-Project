"""Caption preview renderer: draws the *real* captions (same generators, same fonts, same maths as the
final burn) for a short sample phrase onto a transparent PNG, so the Studio preview matches the export.
"""
import os, shutil, subprocess, tempfile
import numpy as np
import cv2

from edit_render import build_style, write_captions
from render_engine import FONTS_DIR_ARG


def _i(f, k, d):
    try:
        return int(float(f.get(k, d)))
    except (TypeError, ValueError):
        return d


def _sample(st):
    """(words, wpc) for the sample phrase."""
    wpc = st.words_per_caption or 0
    if st.layout == "scatter":
        n = wpc or 3
        base = ["up", "inside", "like", "this", "feels", "real", "today", "again", "right", "now", "so", "good"]
        return base[:max(3, n)], n
    sentence = ("Your text will look like this" if st.layout in ("karaoke", "soft") else "Your captions will look like this on screen").split()
    n = wpc or 4
    n = max(1, min(n, len(sentence)))
    return sentence[:n], wpc


def render(f):
    W = max(160, min(1920, _i(f, "out_w", 720)))
    H = max(160, min(1920, _i(f, "out_h", 1280)))
    st, preset = build_style(f)
    words, wpc = _sample(st)
    step = 0.45
    wl = [{"word": w, "start": 0.3 + i * step, "end": 0.3 + i * step + 0.4} for i, w in enumerate(words)]
    last_start = wl[-1]["start"]
    seg = {"start": 0.0, "end": wl[-1]["end"] + 0.1, "text": " ".join(words), "words": wl}
    td = {"segments": [seg]}
    keep = [(0.0, 30.0)]
    t_shot = last_start + 0.32

    tmp = tempfile.mkdtemp(prefix="cprev_")
    try:
        blank = os.path.join(tmp, "blank.png")
        cv2.imwrite(blank, np.zeros((H, W, 3), np.uint8))
        srt, ass = os.path.join(tmp, "c.srt"), os.path.join(tmp, "c.ass")
        write_captions(td, keep, st, preset, srt, ass, blank)
        vf = "ass=c.ass:%s" % FONTS_DIR_ARG

        def shot(colour, name):
            out = os.path.join(tmp, name)
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                   "-i", "color=c=%s:s=%dx%d:r=10" % (colour, W, H),
                   "-vf", vf, "-ss", "%.2f" % t_shot, "-frames:v", "1", out]
            subprocess.run(cmd, cwd=tmp, check=True, capture_output=True, timeout=40)
            return cv2.imread(out, cv2.IMREAD_COLOR).astype(np.float32)

        b = shot("black", "b.png")
        w = shot("white", "w.png")
        # text over black = a*C ; over white = a*C + (1-a)*255  ->  recover alpha and colour
        alpha = np.clip(1.0 - (w - b).mean(axis=2) / 255.0, 0.0, 1.0)
        a3 = np.maximum(alpha, 1e-4)[..., None]
        rgb = np.clip(b / a3, 0, 255)
        out = np.dstack([rgb, alpha * 255.0]).astype(np.uint8)
        ok, buf = cv2.imencode(".png", out)
        if not ok:
            raise RuntimeError("png encode failed")
        return buf.tobytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
