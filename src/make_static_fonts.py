"""Turns variable Google fonts into static Regular/Bold files (libass can't pick weights from variable fonts),
then rebuilds fonts/fonts.json.  Usage: python3 make_static_fonts.py fonts"""
import os, sys, json, shutil
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
D = sys.argv[1]; VAR = os.path.join(os.path.dirname(os.path.abspath(D)), "fonts_variable_src")
os.makedirs(VAR, exist_ok=True)
for f in sorted(os.listdir(D)):
    if not f.endswith(".ttf"): continue
    p = os.path.join(D, f)
    t = TTFont(p, lazy=True)
    if "fvar" not in t: continue
    axes = {a.axisTag: (a.minValue, a.defaultValue, a.maxValue) for a in t["fvar"].axes}
    base = f.split("_")[0].split("[")[0]
    italic = "Italic" in f
    made = 0
    for wname, w in (("Regular", 400), ("Bold", 700)):
        loc = {}
        for tag, (lo, df, hi) in axes.items():
            loc[tag] = min(max(w, lo), hi) if tag == "wght" else df
        try:
            vf = TTFont(p)
            inst = instancer.instantiateVariableFont(vf, loc, inplace=False, updateFontNames=True)
            out = os.path.join(D, f"{base}{'-Italic' if italic else ''}-{wname}.ttf")
            if os.path.exists(out) and os.path.getsize(out) > 0 and "fvar" not in TTFont(out, lazy=True): continue
            inst.save(out); made += 1
        except Exception as e:
            print("skip", f, wname, e)
    if made: shutil.move(p, os.path.join(VAR, f))
# manifest
out = {}
for f in sorted(os.listdir(D)):
    if not f.endswith(".ttf"): continue
    t = TTFont(os.path.join(D, f), lazy=True); n = t["name"]
    fam = n.getDebugName(16) or n.getDebugName(1); sub = n.getDebugName(17) or n.getDebugName(2) or ""
    e = out.setdefault(fam, {"family": fam, "files": []})
    e["files"].append({"file": f, "weight": t["OS/2"].usWeightClass, "variable": "fvar" in t, "italic": "italic" in sub.lower()})
json.dump(sorted(out.values(), key=lambda x: x["family"].lower()), open(os.path.join(D, "fonts.json"), "w"), indent=0)
print(len(out), "families in fonts.json")
