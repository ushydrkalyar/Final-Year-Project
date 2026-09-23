import cv2
import numpy as np
from scenedetect import open_video, SceneManager, ContentDetector, AdaptiveDetector
from scenedetect.detectors import HistogramDetector


# ---------------------------------------------------------------------------
# Scene Detector
# ---------------------------------------------------------------------------
# Uses Computer Vision (PySceneDetect + OpenCV) to analyse video at the
# frame level and extract:
#
#   1. Scene/cut list         — timestamps of every detected cut
#   2. Cut frequency          — cuts per minute (visual editing rhythm)
#   3. Average shot duration  — how long each shot is held
#   4. Transition types       — hard cut vs soft transition (crossfade)
#   5. Visual pacing score    — how fast/slow the visual editing feels
#   6. Color profile          — average brightness, contrast, saturation,
#                              color temperature of the video
#
# Used in two ways:
#   a) On the REFERENCE video → extracts target style metrics
#   b) On the RAW footage     → provides baseline for comparison
# ---------------------------------------------------------------------------


def detect_scenes(video_path: str, threshold: float = 27.0) -> dict:
    """
    Main entry point. Analyses a video file using Computer Vision and
    returns a comprehensive scene analysis report.

    threshold: ContentDetector sensitivity (lower = more sensitive).
               27.0 is a good default for talking-head content.
               Use 20.0 for fast-paced content, 35.0 for slow/static.
    """
    print(f"   🎬 Analysing scenes in: {video_path}")

    results = {
        'scenes':              [],
        'cut_times':           [],
        'scene_count':         0,
        'total_duration_s':    0.0,
        'avg_shot_duration_s': 0.0,
        'cut_frequency_per_min': 0.0,
        'visual_pacing':       'unknown',
        'transition_types':    {},
        'color_profile':       {},
        'cv_metrics':          {},
    }

    try:
        # ── Scene detection using ContentDetector ─────────────────────────
        video      = open_video(video_path)
        manager    = SceneManager()

        # ContentDetector: detects hard cuts using frame difference
        # AdaptiveDetector: better for videos with gradual lighting changes
        manager.add_detector(ContentDetector(threshold=threshold))

        manager.detect_scenes(video, show_progress=False)
        scene_list = manager.get_scene_list()

        total_duration = video.duration.get_seconds()
        results['total_duration_s'] = round(total_duration, 2)

        if not scene_list:
            print("   ℹ️  No scene changes detected — single continuous shot")
            results['scene_count'] = 1
            results['avg_shot_duration_s'] = round(total_duration, 2)
        else:
            # Extract scene timestamps
            scenes = []
            cut_times = []
            for i, (start, end) in enumerate(scene_list):
                start_s = start.get_seconds()
                end_s   = end.get_seconds()
                dur_s   = end_s - start_s
                scenes.append({
                    'scene_num':  i + 1,
                    'start_s':    round(start_s, 3),
                    'end_s':      round(end_s,   3),
                    'duration_s': round(dur_s,   3),
                })
                if i > 0:
                    cut_times.append(round(start_s, 3))

            results['scenes']      = scenes
            results['cut_times']   = cut_times
            results['scene_count'] = len(scenes)

            durations = [s['duration_s'] for s in scenes]
            results['avg_shot_duration_s'] = round(np.mean(durations), 2)

            # Cut frequency
            if total_duration > 0:
                cuts_per_min = (len(cut_times) / total_duration) * 60
                results['cut_frequency_per_min'] = round(cuts_per_min, 1)

            print(f"   ✅ Detected {len(scenes)} scenes, "
                  f"{len(cut_times)} cuts, "
                  f"avg shot {results['avg_shot_duration_s']}s")

        # ── Visual pacing classification ──────────────────────────────────
        cpmin = results['cut_frequency_per_min']
        if cpmin >= 15:
            results['visual_pacing'] = 'very_fast'
        elif cpmin >= 8:
            results['visual_pacing'] = 'fast'
        elif cpmin >= 3:
            results['visual_pacing'] = 'moderate'
        elif cpmin >= 1:
            results['visual_pacing'] = 'slow'
        else:
            results['visual_pacing'] = 'very_slow'

        print(f"   ✅ Visual pacing: {results['visual_pacing']} ({cpmin:.1f} cuts/min)")

        # ── Transition type analysis ──────────────────────────────────────
        results['transition_types'] = _analyse_transitions(video_path, cut_times)

        # ── Color profile extraction ──────────────────────────────────────
        results['color_profile'] = _extract_color_profile(video_path)

        # ── CV metrics summary ────────────────────────────────────────────
        results['cv_metrics'] = {
            'scene_count':           results['scene_count'],
            'avg_shot_duration_s':   results['avg_shot_duration_s'],
            'cut_frequency_per_min': results['cut_frequency_per_min'],
            'visual_pacing':         results['visual_pacing'],
            'dominant_transition':   results['transition_types'].get('dominant', 'hard_cut'),
            'crossfade_ratio':       results['transition_types'].get('crossfade_ratio', 0.0),
            'avg_brightness':        results['color_profile'].get('avg_brightness', 0),
            'avg_saturation':        results['color_profile'].get('avg_saturation', 0),
            'color_temperature':     results['color_profile'].get('color_temperature', 'neutral'),
        }

    except Exception as e:
        print(f"   ❌ Scene detection error: {e}")

    return results


def _analyse_transitions(video_path: str, cut_times: list,
                         window: float = 0.5) -> dict:
    """
    Analyses frames around each cut point to classify whether it's a
    hard cut or a soft transition (crossfade/fade).

    Strategy: at each cut point, compare the 5 frames before and after.
    Hard cut  → large sudden difference between frame N and frame N+1
    Crossfade → gradual change across several frames (lower peak diff)
    Fade      → frames approaching black/white before/after cut
    """
    if not cut_times:
        return {
            'hard_cuts':      0,
            'crossfades':     0,
            'fades':          0,
            'dominant':       'hard_cut',
            'crossfade_ratio': 0.0,
        }

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {'dominant': 'hard_cut', 'crossfade_ratio': 0.0}

    fps        = cap.get(cv2.CAP_PROP_FPS) or 30.0
    hard_cuts  = 0
    crossfades = 0
    fades      = 0

    for cut_time in cut_times:
        frame_num = int(cut_time * fps)
        samples   = []

        # Sample 6 frames around the cut point (3 before, 3 after)
        for offset in [-3, -2, -1, 0, 1, 2]:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_num + offset))
            ret, frame = cap.read()
            if ret:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                samples.append(gray.astype(np.float32))

        if len(samples) < 4:
            hard_cuts += 1
            continue

        # Calculate frame differences
        diffs = [np.mean(np.abs(samples[i+1] - samples[i]))
                 for i in range(len(samples)-1)]

        peak_diff = max(diffs)
        avg_diff  = np.mean(diffs)
        peak_idx  = diffs.index(peak_diff)

        # Check for fade (frames near black or white)
        mid_frame = samples[len(samples)//2]
        is_near_black = np.mean(mid_frame) < 15
        is_near_white = np.mean(mid_frame) > 240

        if is_near_black or is_near_white:
            fades += 1
        elif peak_diff > 30 and peak_diff / (avg_diff + 1) > 2.5:
            # Sudden large jump = hard cut
            hard_cuts += 1
        else:
            # Gradual change = crossfade
            crossfades += 1

    cap.release()

    total = len(cut_times)
    crossfade_ratio = round(crossfades / total, 2) if total > 0 else 0.0

    dominant = 'hard_cut'
    if crossfades > hard_cuts and crossfades > fades:
        dominant = 'crossfade'
    elif fades > hard_cuts and fades > crossfades:
        dominant = 'fade'

    return {
        'hard_cuts':       hard_cuts,
        'crossfades':      crossfades,
        'fades':           fades,
        'dominant':        dominant,
        'crossfade_ratio': crossfade_ratio,
    }


def _extract_color_profile(video_path: str,
                           sample_frames: int = 30) -> dict:
    """
    Samples frames evenly across the video and extracts:
      - avg_brightness    : 0-255, how bright the video is
      - avg_contrast      : standard deviation of luminance
      - avg_saturation    : 0-255 in HSV space
      - color_temperature : 'warm' / 'cool' / 'neutral'
      - dominant_hue      : 0-179 (HSV hue)
      - avg_rgb           : [R, G, B] average channel values
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {}

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames == 0:
        cap.release()
        return {}

    step        = max(1, total_frames // sample_frames)
    brightnesses = []
    contrasts    = []
    saturations  = []
    hues         = []
    r_vals, g_vals, b_vals = [], [], []

    for i in range(0, total_frames, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            continue

        # Brightness and contrast from grayscale
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        brightnesses.append(np.mean(gray))
        contrasts.append(np.std(gray))

        # Saturation and hue from HSV
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
        hues.append(np.mean(hsv[:,:,0]))
        saturations.append(np.mean(hsv[:,:,1]))

        # Average RGB channels
        b, g, r = cv2.split(frame.astype(np.float32))
        r_vals.append(np.mean(r))
        g_vals.append(np.mean(g))
        b_vals.append(np.mean(b))

    cap.release()

    if not brightnesses:
        return {}

    avg_r = np.mean(r_vals)
    avg_g = np.mean(g_vals)
    avg_b = np.mean(b_vals)

    # Color temperature: warm = more red/yellow, cool = more blue
    rg_ratio = avg_r / (avg_g + 1)
    gb_ratio = avg_g / (avg_b + 1)
    if avg_r > avg_b * 1.15:
        color_temp = 'warm'
    elif avg_b > avg_r * 1.15:
        color_temp = 'cool'
    else:
        color_temp = 'neutral'

    profile = {
        'avg_brightness':   round(float(np.mean(brightnesses)), 1),
        'avg_contrast':     round(float(np.mean(contrasts)), 1),
        'avg_saturation':   round(float(np.mean(saturations)), 1),
        'dominant_hue':     round(float(np.mean(hues)), 1),
        'color_temperature':color_temp,
        'avg_rgb':          [round(float(avg_r),1), round(float(avg_g),1), round(float(avg_b),1)],
        'frames_sampled':   len(brightnesses),
    }

    print(f"   ✅ Color profile: brightness={profile['avg_brightness']:.0f} "
          f"saturation={profile['avg_saturation']:.0f} "
          f"temp={color_temp}")

    return profile


def compare_styles(reference_analysis: dict, raw_analysis: dict) -> dict:
    """
    Compares reference video style against raw footage and returns
    a style_gap report showing what needs to change.

    Used to guide the decision_engine and color grading.
    """
    ref_cp  = reference_analysis.get('cut_frequency_per_min', 0)
    raw_cp  = raw_analysis.get('cut_frequency_per_min', 0)

    ref_shot = reference_analysis.get('avg_shot_duration_s', 0)
    raw_shot = raw_analysis.get('avg_shot_duration_s', 0)

    ref_col  = reference_analysis.get('color_profile', {})
    raw_col  = raw_analysis.get('color_profile',  {})

    return {
        'cut_frequency_gap':    round(ref_cp - raw_cp, 1),
        'shot_duration_gap_s':  round(ref_shot - raw_shot, 2),
        'brightness_gap':       round(
            ref_col.get('avg_brightness', 128) -
            raw_col.get('avg_brightness', 128), 1),
        'saturation_gap':       round(
            ref_col.get('avg_saturation', 128) -
            raw_col.get('avg_saturation', 128), 1),
        'ref_pacing':           reference_analysis.get('visual_pacing', 'unknown'),
        'raw_pacing':           raw_analysis.get('visual_pacing', 'unknown'),
        'ref_transition':       reference_analysis.get('transition_types', {}).get('dominant', 'hard_cut'),
        'needs_faster_cuts':    ref_cp > raw_cp,
        'needs_color_grade':    abs(
            ref_col.get('avg_brightness', 128) -
            raw_col.get('avg_brightness', 128)) > 15,
    }


def calculate_style_match(style_gap: dict) -> float | None:
    """
    Converts the raw style_gap (from compare_styles) into a single 0-100
    "style match" percentage — how closely the RAW footage already matches
    the REFERENCE video's visual editing style (cut rhythm, shot length,
    brightness, saturation).

    This is what the UI's Reference Analysis panel shows as "Style Match".
    It must be derived from the actual reference-vs-raw comparison — not
    substituted with an unrelated generic edit-quality score.

    Each metric is normalised against a "gap large enough to be obviously
    noticeable" scale, then combined into a weighted average. Cut rhythm and
    shot length get more weight since they're the dominant visible pacing
    cues; colour gets less weight since color_grade can correct that anyway.

    Returns None if no gap data is available (e.g. CV analysis was
    disabled, or the reference video's frames couldn't be downloaded).
    """
    if not style_gap:
        return None

    # Gap magnitude (absolute) at which a metric is considered "maximally
    # different" and contributes 0 to the match score.
    SCALES = {
        'cut_frequency_gap':   15.0,   # cuts/min
        'shot_duration_gap_s':  3.0,   # seconds
        'brightness_gap':      60.0,   # 0-255 scale
        'saturation_gap':      60.0,   # 0-255-ish scale
    }
    WEIGHTS = {
        'cut_frequency_gap':   0.35,
        'shot_duration_gap_s': 0.35,
        'brightness_gap':      0.15,
        'saturation_gap':      0.15,
    }

    def _sub_score(key: str) -> float:
        gap = style_gap.get(key)
        if gap is None:
            return 1.0   # metric unavailable — don't penalise for it
        scale = SCALES[key]
        return max(0.0, 1.0 - min(abs(gap), scale) / scale)

    weighted_sum = sum(WEIGHTS[k] * _sub_score(k) for k in WEIGHTS)
    return round(weighted_sum * 100, 1)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python scene_detector.py <video_path>")
        sys.exit(1)

    video = sys.argv[1]
    print(f"\n=== Scene Detection: {video} ===")
    result = detect_scenes(video)

    print(f"\nScenes detected:     {result['scene_count']}")
    print(f"Total duration:      {result['total_duration_s']}s")
    print(f"Avg shot duration:   {result['avg_shot_duration_s']}s")
    print(f"Cut frequency:       {result['cut_frequency_per_min']}/min")
    print(f"Visual pacing:       {result['visual_pacing']}")
    print(f"Dominant transition: {result['transition_types'].get('dominant','?')}")
    print(f"Color temperature:   {result['color_profile'].get('color_temperature','?')}")
    print(f"Avg brightness:      {result['color_profile'].get('avg_brightness','?')}")
