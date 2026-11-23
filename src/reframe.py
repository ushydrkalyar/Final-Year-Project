"""
reframe.py - change a video's shape (landscape <-> vertical) while keeping the
speaker in the middle of the frame, or apply a fixed frame the user chose by
dragging on the preview.

How it plugs in
---------------
edit_render.cut() asks the plan returned here for an ffmpeg video filter for
every piece of the kept ranges. The reframed result therefore becomes
the "intermediate" video that everything after it already works from (caption
layout, restyle, re-caption), so no other stage needs to know about reframing.

Frame settings (a dict, see normalize_frame):
    fmt   "vertical" (9:16, 1080x1920) | "landscape" (16:9, 1920x1080) | "square" (1:1, 1080x1080)
    mode  "auto"   - follow the speaker (landscape -> vertical) or keep the whole
                     picture (vertical -> landscape, on a soft blurred backdrop)
          "custom" - a fixed window: centre (cx, cy) as fractions of the source
                     frame plus a zoom (1 = the largest window of that shape)
"""
import math
import os
import subprocess

import media_probe
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_PATH = os.path.join(HERE, "models", MODEL_NAME)
MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/" + MODEL_NAME)

OUT_SIZE = {"vertical": (1080, 1920), "landscape": (1920, 1080), "square": (1080, 1080)}
ASPECT = {"vertical": 9 / 16, "landscape": 16 / 9, "square": 1.0}
NATURAL = {"vertical": "portrait", "landscape": "landscape", "square": "square"}


# ── settings ───────────────────────────────────────────────────────────────
def normalize_frame(fmt, mode="auto", cx=0.5, cy=0.5, zoom=1.0):
    """Validate form input. Returns None when no reframing was requested."""
    fmt = (fmt or "").strip().lower()
    if fmt not in OUT_SIZE:
        return None

    def _f(v, lo, hi, default):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return default if v != v else max(lo, min(hi, v))

    return {
        "fmt": fmt,
        "mode": "custom" if str(mode).strip().lower() == "custom" else "auto",
        "cx": _f(cx, 0.0, 1.0, 0.5),
        "cy": _f(cy, 0.0, 1.0, 0.5),
        "zoom": _f(zoom, 1.0, 4.0, 1.0),
    }


def probe_video(path):
    """(width, height, duration_s) as DISPLAYED - phone rotation metadata applied."""
    found = media_probe.info(path)
    if not found or not found.width or not found.height:
        raise RuntimeError("Couldn't read the video's size.")
    return (*found.display_size, found.duration)


def _even(v):
    return max(2, int(v) // 2 * 2)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _cover_window(W, H, aspect):
    """Largest window of the given aspect that fits inside a W x H frame."""
    if W / H >= aspect:
        return H * aspect, float(H)
    return float(W), W / aspect


def source_kind(W, H):
    if W > H * 1.05:
        return "landscape"
    if H > W * 1.05:
        return "portrait"
    return "square"


# ── plan ───────────────────────────────────────────────────────────────────
class ReframePlan:
    """Result of plan_reframe(): hands out one ffmpeg -vf string per kept clip."""

    def __init__(self, kind, out_w, out_h, filt=None, keys=None, cw=None, ch=None,
                 y=0, note=""):
        self.kind, self.out_w, self.out_h = kind, out_w, out_h
        self.filt, self.keys, self.cw, self.ch, self.y, self.note = filt, keys, cw, ch, y, note

    def describe(self):
        return f"{self.kind} -> {self.out_w}x{self.out_h} {self.note}".strip()

    def filter_for_clip(self, start, end):
        if self.kind != "track":
            return self.filt
        x = _x_expression(self.keys, start, end)
        return (f"crop={self.cw}:{self.ch}:x='{x}':y={self.y},"
                f"scale={self.out_w}:{self.out_h}:flags=lanczos,setsar=1")


def _lerp_track(keys, t):
    if t <= keys[0][0]:
        return keys[0][1]
    if t >= keys[-1][0]:
        return keys[-1][1]
    for (t0, x0), (t1, x1) in zip(keys, keys[1:]):
        if t0 <= t <= t1:
            return x0 if t1 == t0 else x0 + (x1 - x0) * (t - t0) / (t1 - t0)
    return keys[-1][1]


def _x_expression(keys, start, end):
    """ffmpeg expression for the crop's x over one clip, in CLIP-LOCAL time
    (ffmpeg restarts t at 0 for every -ss'd clip)."""
    pts = [(0.0, _lerp_track(keys, start))]
    pts += [(t - start, x) for t, x in keys if start < t < end]
    pts.append((max(end - start, 0.001), _lerp_track(keys, end)))
    xs = [p[1] for p in pts]
    if max(xs) - min(xs) < 1.5:
        return str(int(round(sum(xs) / len(xs))))
    expr = f"{pts[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(pts, pts[1:]))):
        dt = max(t1 - t0, 0.001)
        seg = f"({x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{dt:.3f})"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return expr


def plan_reframe(input_video, frame, progress_cb=None):
    """Decide how to reshape `input_video`. Returns None when the video should be
    left exactly as it is (nothing asked for, or it is already the right shape)."""
    if not frame:
        return None
    W, H, dur = probe_video(input_video)
    fmt = frame["fmt"]
    ow, oh = OUT_SIZE[fmt]
    aspect = ASPECT[fmt]
    kind = source_kind(W, H)

    # a fixed window chosen by the user
    if frame["mode"] == "custom":
        bw, bh = _cover_window(W, H, aspect)
        z = frame["zoom"]
        cw, ch = _even(min(W, bw / z)), _even(min(H, bh / z))
        x = _clamp(frame["cx"] * W - cw / 2, 0, W - cw)
        y = _clamp(frame["cy"] * H - ch / 2, 0, H - ch)
        if cw >= W - 1 and ch >= H - 1 and (W, H) == (ow, oh):
            return None
        filt = (f"crop={cw}:{ch}:{int(x) // 2 * 2}:{int(y) // 2 * 2},"
                f"scale={ow}:{oh}:flags=lanczos,setsar=1")
        return ReframePlan("custom", ow, oh, filt=filt,
                           note=f"window {cw}x{ch} of {W}x{H}")

    # nothing to do: already the shape that was asked for
    if kind == NATURAL[fmt]:
        return None

    # narrower -> wider: whole picture, centred, on a soft backdrop
    if fmt == "landscape" or (fmt == "square" and kind == "portrait"):
        filt = (f"split[bg][fg];"
                f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},"
                f"boxblur=40:3,eq=brightness=-0.08[b];"
                f"[fg]scale=-2:{oh}[f];[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1")
        return ReframePlan("fit", ow, oh, filt=filt, note=f"{W}x{H} on blurred backdrop")

    # wider -> narrower: follow the speaker
    cw, ch = _even(min(W, H * aspect)), _even(H)
    keys, note = _track_speaker(input_video, W, H, dur, cw, progress_cb)
    return ReframePlan("track", ow, oh, keys=keys, cw=cw, ch=ch, y=0,
                       note=f"{note}; window {cw}x{ch} of {W}x{H}")


# ── speaker tracking ───────────────────────────────────────────────────────
def _load_detector(sw, sh):
    """YuNet face detector (ships with the project; fetched once if missing)."""
    try:
        import cv2
    except Exception:
        return None
    if not hasattr(cv2, "FaceDetectorYN"):
        return None
    if not os.path.exists(MODEL_PATH):
        try:
            os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
            urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
        except Exception:
            return None
    try:
        return cv2.FaceDetectorYN.create(MODEL_PATH, "", (sw, sh), 0.6, 0.3, 50)
    except Exception:
        return None


def _sample_frames(path, fps, sw, sh):
    """Yield small BGR frames at `fps` through one ffmpeg pass."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-an", "-sn",
           "-vf", f"fps={fps},scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = sw * sh * 3
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
        p.kill()
        p.wait()


def _track_speaker(path, W, H, dur, cw, progress_cb=None):
    """Smooth crop-x keyframes [(time_s, x_px)] that keep the speaker centred."""
    fallback = [(0.0, (W - cw) / 2.0)]
    try:
        import cv2
    except Exception:
        return fallback, "OpenCV unavailable - centred"

    fps = 4 if dur <= 300 else (3 if dur <= 900 else 2)
    sw = 480
    sh = max(2, int(round(H * sw / W)) // 2 * 2)
    det = _load_detector(sw, sh)
    expected = max(1, int(dur * fps))

    ts, face_x, move_x = [], [], []
    prev_gray, last_x = None, None
    for i, fr in enumerate(_sample_frames(path, fps, sw, sh)):
        ts.append(i / fps)
        fx = np.nan
        if det is not None:
            try:
                _, faces = det.detect(fr)
            except Exception:
                faces = None
            if faces is not None and len(faces):
                # largest face; when several are similar in size prefer the one
                # nearest the previous pick so we never flip between two people
                areas = faces[:, 2] * faces[:, 3]
                ok = [k for k in range(len(faces)) if areas[k] >= 0.4 * areas.max()]
                if last_x is not None:
                    k = min(ok, key=lambda k: abs((faces[k, 0] + faces[k, 2] / 2) / sw - last_x))
                else:
                    k = max(ok, key=lambda k: areas[k])
                fx = (faces[k, 0] + faces[k, 2] / 2) / sw
                last_x = fx
        face_x.append(fx)
        # secondary signal: where the movement is (used only if faces are missing)
        g = cv2.GaussianBlur(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), (9, 9), 0)
        mx = np.nan
        if prev_gray is not None:
            d = cv2.absdiff(g, prev_gray)
            col = (d > 18).sum(axis=0).astype(np.float64)
            if col.sum() > sw * 0.6:
                mx = float((col * np.arange(sw)).sum() / col.sum() / sw)
        move_x.append(mx)
        prev_gray = g
        if progress_cb and i % 20 == 0:
            progress_cb(min(1.0, (i + 1) / expected))

    if len(ts) < 2:
        return fallback, "too short to track - centred"
    t = np.array(ts)
    fxa, mxa = np.array(face_x, float), np.array(move_x, float)
    face_cov = float(np.mean(~np.isnan(fxa)))
    if face_cov >= 0.05:
        sig, how = fxa, f"faces in {face_cov * 100:.0f}% of samples"
    elif np.mean(~np.isnan(mxa)) >= 0.05:
        sig, how = mxa, "no faces found - following movement"
    else:
        return fallback, "no speaker found - centred"
    good = ~np.isnan(sig)
    sig = np.interp(t, t[good], sig[good])                  # fill gaps by holding / interpolating

    # median filter against single-frame misdetections
    k = 5
    pad = np.pad(sig, (k // 2, k // 2), mode="edge")
    sig = np.array([np.median(pad[i:i + k]) for i in range(len(sig))])

    # camera-operator follow: stay still while the speaker is near the middle of
    # the window, then glide (rate-limited) when they move out of the dead zone
    half = cw / 2.0
    cx_px = sig * W
    dead = 0.12 * cw
    vmax = 0.45 * cw                                         # px per second
    dt = 1.0 / fps
    cam, track = cx_px[0], []
    for ti, target in zip(t, cx_px):
        err = target - cam
        if abs(err) > dead:
            goal = target - math.copysign(dead * 0.5, err)
            cam += _clamp(goal - cam, -vmax * dt, vmax * dt)
        track.append((float(ti), _clamp(cam - half, 0.0, W - cw)))

    keys = _simplify(track)
    return keys, how


def _simplify(points, tol=4.0, max_points=48):
    """Reduce a dense track to a few keyframes (error never above `tol` px)."""
    if len(points) <= 2:
        return points

    def reduce_once(tol):
        keep = {0, len(points) - 1}
        stack = [(0, len(points) - 1)]
        while stack:
            lo, hi = stack.pop()
            (t0, x0), (t1, x1) = points[lo], points[hi]
            span = (t1 - t0) or 1.0
            worst, idx = 0.0, None
            for i in range(lo + 1, hi):
                t, x = points[i]
                e = abs(x - (x0 + (x1 - x0) * (t - t0) / span))
                if e > worst:
                    worst, idx = e, i
            if idx is not None and worst > tol:
                keep.add(idx)
                stack.append((lo, idx))
                stack.append((idx, hi))
        return sorted(keep)

    while True:
        idxs = reduce_once(tol)
        if len(idxs) <= max_points or tol > 400:
            return [points[i] for i in idxs]
        tol *= 1.6
