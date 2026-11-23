"""
Reference-video caption style extractor.

Given a video that already has burned-in captions, this scans sampled frames,
finds the on-screen caption text with Tesseract OCR, and measures its
font size, color, vertical position, and typical words-per-caption directly
from the pixels -- no AI/LLM/vision model involved anywhere in this file.
Everything here is OpenCV + Tesseract OCR + plain numpy/statistics.

Output is a dict shaped like one of studio.html's TPLCFG entries, ready to
be saved as a new selectable caption template.
"""
import re
import statistics

import cv2
import media_probe
import numpy as np
import pytesseract
from pytesseract import Output

MIN_CONF = 55
MIN_HEIGHT_FRAC = 0.010
MAX_HEIGHT_FRAC = 0.35
# horizontal bands scanned independently -- keeps OCR focused on a slice of
# the frame at a time instead of fighting a whole busy background at once
BANDS = [(0.0, 0.22), (0.18, 0.42), (0.38, 0.62), (0.58, 0.82), (0.78, 1.0)]
UPSCALE = 3
# junk tokens that are almost always OS/UI chrome picked up by accident
# (dock icons, function-key labels, stray punctuation) rather than captions
_JUNK_RE = re.compile(r'^(f\d{1,2}|[a-z]{1,2}|[^a-z]+)$', re.IGNORECASE)


def _sample_frame_indices(total_frames, n):
    if total_frames <= n:
        return list(range(total_frames))
    step = total_frames / float(n)
    return sorted({int(i * step) for i in range(n)})


def _ocr_band(gray_band):
    """Try both normal and inverted Otsu binarization, run OCR on each,
    keep whichever found more total confidence (handles light-on-dark and
    dark-on-light captions without knowing in advance which it is)."""
    up = cv2.resize(gray_band, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
    best_data, best_score = None, -1
    for flag in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        _, th = cv2.threshold(up, 0, 255, flag + cv2.THRESH_OTSU)
        try:
            data = pytesseract.image_to_data(th, output_type=Output.DICT, config="--psm 6")
        except Exception:
            continue
        score = sum(float(c) for c in data["conf"] if str(c) not in ("-1",))
        if score > best_score:
            best_score, best_data = score, data
    return best_data


def _group_words_into_lines(data, conf_min):
    if not data:
        return []
    lines = {}
    n = len(data["text"])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if not txt or conf < conf_min:
            continue
        if _JUNK_RE.match(txt):
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append({
            "text": txt,
            "x": data["left"][i], "y": data["top"][i],
            "w": data["width"][i], "h": data["height"][i],
        })
    out = []
    for words in lines.values():
        real_words = [w for w in words if len(w["text"]) >= 3 and any(c.isalpha() for c in w["text"])]
        if not real_words:
            continue
        x0 = min(w["x"] for w in words)
        y0 = min(w["y"] for w in words)
        x1 = max(w["x"] + w["w"] for w in words)
        y1 = max(w["y"] + w["h"] for w in words)
        out.append({
            "text": " ".join(w["text"] for w in words),
            "bbox": (x0, y0, x1, y1),
            "n_words": len(words),
            "h": statistics.median(w["h"] for w in words),
        })
    return out


def _dominant_text_color(frame_bgr, bbox, margin=6):
    """Estimate the caption's text color by separating it from its local
    background with k-means (k=2) on the pixels inside the box, then picking
    the cluster that differs most from a thin ring just outside the box."""
    x0, y0, x1, y1 = bbox
    h, wimg = frame_bgr.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(wimg, x1), min(h, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    patch = frame_bgr[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
    if len(patch) < 8:
        return None

    try:
        _, labels, centers = cv2.kmeans(
            patch, 2, None,
            (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 15, 0.5),
            3, cv2.KMEANS_PP_CENTERS,
        )
    except Exception:
        return None

    rx0, ry0 = max(0, x0 - margin), max(0, y0 - margin)
    rx1, ry1 = min(wimg, x1 + margin), min(h, y1 + margin)
    ring = frame_bgr[ry0:ry1, rx0:rx1]
    oy, ox = y0 - ry0, x0 - rx0
    mask = np.ones(ring.shape[:2], dtype=bool)
    mask[oy:oy + (y1 - y0), ox:ox + (x1 - x0)] = False
    ring_px = ring[mask].astype(np.float32)
    bg_estimate = ring_px.mean(axis=0) if len(ring_px) else patch.mean(axis=0)

    d0 = np.linalg.norm(centers[0] - bg_estimate)
    d1 = np.linalg.norm(centers[1] - bg_estimate)
    text_center = centers[0] if d0 > d1 else centers[1]
    b, g, r = [int(round(c)) for c in text_center]
    return (r, g, b)


def _rgb_to_hex(rgb):
    r, g, b = rgb
    return "#{:02X}{:02X}{:02X}".format(
        max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
    )


def extract_caption_style(video_path, max_samples=24):
    """
    Returns {"config": {...TPLCFG-shaped...}, "debug": {...}, "samples": n}
    or None if no readable captions were found.
    """
    W, H = media_probe.frame_size(video_path)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    if total <= 0:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        dur = media_probe.duration(video_path, 10.0)
        total = int(dur * fps)

    idxs = _sample_frame_indices(total, max_samples)

    raw = []     # (idx, text_norm, cx_frac, cy_frac, instance_dict)
    prev_text = None
    for idx in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frame_lines = []
        for (b0, b1) in BANDS:
            y0, y1 = int(H * b0), int(H * b1)
            data = _ocr_band(gray[y0:y1, :])
            for ln in _group_words_into_lines(data, MIN_CONF):
                bx0, by0, bx1, by1 = ln["bbox"]
                # map band-local (upscaled) coords back to full-frame pixels
                ln["bbox"] = (
                    bx0 // UPSCALE, y0 + by0 // UPSCALE,
                    bx1 // UPSCALE, y0 + by1 // UPSCALE,
                )
                ln["h"] = ln["h"] / UPSCALE
                frame_lines.append(ln)

        for ln in frame_lines:
            frac_h = ln["h"] / float(H)
            if not (MIN_HEIGHT_FRAC <= frac_h <= MAX_HEIGHT_FRAC):
                continue
            norm_text = ln["text"].strip().lower()
            if prev_text is not None and norm_text == prev_text:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            cx = (x0 + x1) / 2.0 / W
            cy = (y0 + y1) / 2.0 / H
            raw.append({
                "idx": idx, "text": norm_text, "cx": cx, "cy": cy,
                "h_frac": frac_h, "n_words": ln["n_words"], "bbox": ln["bbox"],
            })
        if frame_lines:
            prev_text = max(frame_lines, key=lambda l: l["h"])["text"].strip().lower()

    cap.release()

    # Drop anything that's a static on-screen overlay rather than a caption:
    # a real caption's wording changes from one group to the next, while a
    # logo / UI control / watermark shows the same text in the same spot
    # over and over. Flag any text that repeats (near-identical position)
    # across 2+ of the sampled frames and discard all of its occurrences.
    def _cy_bucket(cy):
        return round(cy / 0.04) * 0.04

    buckets = {}
    for item in raw:
        buckets.setdefault(_cy_bucket(item["cy"]), []).append(item)

    static_buckets = set()
    for b, hits in buckets.items():
        if len(hits) < 3:
            continue
        tok_counts = {}
        for h in hits:
            for tok in set(h["text"].split()):
                tok_counts[tok] = tok_counts.get(tok, 0) + 1
        top = max(tok_counts.values()) if tok_counts else 0
        if top / len(hits) >= 0.5:
            static_buckets.add(b)

    instances_raw = [r for r in raw if _cy_bucket(r["cy"]) not in static_buckets]

    if len(instances_raw) < 3:
        return None

    # resolve color lazily only for the instances that survive filtering
    cap2 = cv2.VideoCapture(video_path)
    instances = []
    for r in instances_raw:
        cap2.set(cv2.CAP_PROP_POS_FRAMES, r["idx"])
        ok, frame = cap2.read()
        color = _dominant_text_color(frame, r["bbox"]) if ok else None
        instances.append({
            "h_frac": r["h_frac"], "cx": r["cx"], "cy": r["cy"],
            "n_words": r["n_words"], "color": color,
        })
    cap2.release()

    if len(instances) < 3:
        return None

    heights = [i["h_frac"] for i in instances]
    cys = [i["cy"] for i in instances]
    cxs = [i["cx"] for i in instances]
    wpcs = [i["n_words"] for i in instances]
    colors = [i["color"] for i in instances if i["color"] is not None]

    med_h_frac = statistics.median(heights)
    med_cy = statistics.median(cys)
    med_cx = statistics.median(cxs)
    # prefer multi-word reads for the words-per-caption estimate: a line OCR
    # only partially caught will undercount words, so lean on whichever
    # instances read the most words rather than letting partial reads drag
    # the estimate down
    multi = [i["n_words"] for i in instances if i["n_words"] >= 2]
    wpc_pool = multi if multi else wpcs
    wpc_pool_sorted = sorted(wpc_pool)
    med_wpc = wpc_pool_sorted[int(len(wpc_pool_sorted) * 0.75)]

    if colors:
        arr = np.array(colors, dtype=np.float32)
        med_color = tuple(int(round(v)) for v in np.median(arr, axis=0))
    else:
        med_color = (255, 255, 255)

    approx_fontsize_px = med_h_frac * H / 0.70
    st_size = int(round(approx_fontsize_px / 0.56))
    st_size = max(16, min(64, st_size))

    y_pct = int(round(med_cy * 100))
    y_pct = max(5, min(95, y_pct))

    if med_cx < 0.35:
        align = "l"
    elif med_cx > 0.65:
        align = "r"
    else:
        align = "c"

    wpc = max(1, min(8, med_wpc))
    lines_field = 2 if wpc > 5 else 1

    config = {
        "font": "Arial", "face": "Bold", "italic": False,
        "size": st_size, "color": _rgb_to_hex(med_color),
        "wpc": wpc, "lines": lines_field, "em": False,
        "y": y_pct, "align": align,
    }
    debug = {
        "samples": len(instances),
        "median_height_frac": round(med_h_frac, 4),
        "median_cy": round(med_cy, 4),
        "median_cx": round(med_cx, 4),
        "approx_fontsize_px": round(approx_fontsize_px, 1),
    }
    return {"config": config, "debug": debug, "samples": len(instances)}
