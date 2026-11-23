"""Guards locked caption templates against accidental changes.
Locked: Highlight Box (box_karaoke), Soft Reveal (soft_reveal).
Usage (from src_v4):  python3 locked_templates/check_locked_templates.py                  -> checks all
                      python3 locked_templates/check_locked_templates.py --update NAME    -> re-freezes NAME (box | soft) - only when YOU decide to change it
"""
import os, re, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from subtitle_service import create_remapped_box_ass, create_remapped_soft_ass
from render_engine import SubtitleStyle

def _td():
    words = "All know is just something about you. Got me feeling like I can't be without".split()
    W = [{"word": w, "start": 0.3 + i * 0.42, "end": 0.3 + i * 0.42 + 0.36} for i, w in enumerate(words)]
    return {"segments": [{"start": 0, "end": 9, "text": "", "words": W}]}

def build_box():
    st = SubtitleStyle(font_name="Poppins Medium", font_size=11, bold=False, primary_colour="&H00141414",
                       pos_y=0.62, words_per_caption=5, layout="karaoke")
    out = os.path.join(tempfile.gettempdir(), "hb_check.ass")
    create_remapped_box_ass(_td(), [(0, 30)], out, words_per_caption=5, style=st)
    return open(out, encoding="utf-8").read()

def build_soft():
    st = SubtitleStyle(font_name="Manrope", font_size=22, bold=True, primary_colour="&H00FFFFFF", pos_y=0.63,
                       words_per_caption=5, layout="soft", emphasis=True, emph_font="Instrument Serif",
                       emph_scale=1.7, emph_italic=True, emph_colour="&H00FFFFFF", emph_every=1)
    out = os.path.join(tempfile.gettempdir(), "soft_check.ass")
    create_remapped_soft_ass(_td(), [(0, 30)], out, words_per_caption=5, style=st, video_path=None)
    return open(out, encoding="utf-8").read()

def cfg(key):
    h = open(os.path.join(ROOT, "studio.html"), encoding="utf-8").read()
    m = re.search(key + r":\{[^}]*\}", h)
    return m.group(0) if m else ""

T = {"box":  ("Highlight Box", build_box,  "box_karaoke",  "highlight_box"),
     "soft": ("Soft Reveal",   build_soft, "soft_reveal",  "soft_reveal")}
def paths(n): return os.path.join(HERE, n + ".golden.ass"), os.path.join(HERE, n + ".golden.cfg.txt")

if "--update" in sys.argv:
    k = sys.argv[sys.argv.index("--update") + 1:][:1]
    for key in (k or T.keys()):
        name, b, ck, fn = T[key]; ga, gc = paths(fn)
        open(ga, "w", encoding="utf-8").write(b()); open(gc, "w", encoding="utf-8").write(cfg(ck)); print("re-frozen", name)
    sys.exit(0)
ok = True
for key, (name, b, ck, fn) in T.items():
    ga, gc = paths(fn)
    good = True
    if b() != open(ga, encoding="utf-8").read():
        print("CHANGED: %s caption output no longer matches the locked version" % name); good = False
    if cfg(ck) != open(gc, encoding="utf-8").read():
        print("CHANGED: %s template settings in studio.html no longer match the locked version" % name); good = False
    print("OK - %s is unchanged" % name if good else "FAILED - %s" % name); ok &= good
sys.exit(0 if ok else 1)
