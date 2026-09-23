import subprocess
import os
import json


# ---------------------------------------------------------------------------
# Color Grader
# ---------------------------------------------------------------------------
# Analyses the color profile of a reference video and applies matching
# color adjustments to the output video using FFmpeg's color filters.
#
# Satisfies: "color grading" requirement from Sigma Bytecraft project scope.
#
# Pipeline position: runs as Pass 3 after render_engine's Pass 2 (subtitle
# burn). Takes the final edited video and applies color grading on top.
#
# FFmpeg filters used:
#   eq          — brightness, contrast, saturation, gamma
#   colorbalance— RGB channel shifts for color temperature
#   curves      — tone curve adjustments
#   hue         — hue rotation and saturation
# ---------------------------------------------------------------------------


def compute_grading_params(reference_color_profile: dict,
                           raw_color_profile: dict) -> dict:
    """
    Computes FFmpeg color filter parameters needed to make the raw footage
    match the reference video's color profile.

    Clamps all adjustments to safe ranges to prevent extreme results.

    Returns a dict of FFmpeg eq/colorbalance parameters.
    """
    ref = reference_color_profile
    raw = raw_color_profile

    if not ref or not raw:
        return {}

    # ── Brightness adjustment ─────────────────────────────────────────────
    # FFmpeg eq brightness: -1.0 to 1.0, 0 = no change
    # Map 0-255 brightness difference to -0.3 to 0.3 range
    brightness_diff = ref.get('avg_brightness', 128) - raw.get('avg_brightness', 128)
    brightness_adj  = round(brightness_diff / 255 * 0.6, 3)
    brightness_adj  = max(-0.3, min(0.3, brightness_adj))

    # ── Contrast adjustment ───────────────────────────────────────────────
    # FFmpeg eq contrast: 0.0 to 2.0, 1.0 = no change
    ref_contrast = ref.get('avg_contrast', 50)
    raw_contrast = raw.get('avg_contrast', 50)
    if raw_contrast > 0:
        contrast_ratio = ref_contrast / raw_contrast
    else:
        contrast_ratio = 1.0
    contrast_adj = round(max(0.7, min(1.5, contrast_ratio)), 3)

    # ── Saturation adjustment ─────────────────────────────────────────────
    # FFmpeg eq saturation: 0.0 to 3.0, 1.0 = no change
    ref_sat = ref.get('avg_saturation', 100)
    raw_sat = raw.get('avg_saturation', 100)
    if raw_sat > 0:
        sat_ratio = ref_sat / raw_sat
    else:
        sat_ratio = 1.0
    saturation_adj = round(max(0.5, min(2.0, sat_ratio)), 3)

    # ── Color temperature (RGB channel balance) ───────────────────────────
    # FFmpeg colorbalance: -1.0 to 1.0 per channel per shadow/mid/highlight
    ref_rgb = ref.get('avg_rgb', [128, 128, 128])
    raw_rgb = raw.get('avg_rgb', [128, 128, 128])

    # Compute per-channel shifts needed
    r_shift = (ref_rgb[0] - raw_rgb[0]) / 255 * 0.3
    g_shift = (ref_rgb[1] - raw_rgb[1]) / 255 * 0.3
    b_shift = (ref_rgb[2] - raw_rgb[2]) / 255 * 0.3

    r_shift = round(max(-0.2, min(0.2, r_shift)), 3)
    g_shift = round(max(-0.2, min(0.2, g_shift)), 3)
    b_shift = round(max(-0.2, min(0.2, b_shift)), 3)

    # ── Color temperature preset override ─────────────────────────────────
    ref_temp = ref.get('color_temperature', 'neutral')
    raw_temp = raw.get('color_temperature', 'neutral')

    # If temperatures differ, apply a stronger correction
    if ref_temp == 'warm' and raw_temp == 'cool':
        r_shift = max(r_shift, 0.08)
        b_shift = min(b_shift, -0.08)
    elif ref_temp == 'cool' and raw_temp == 'warm':
        b_shift = max(b_shift, 0.08)
        r_shift = min(r_shift, -0.08)

    params = {
        'brightness':   brightness_adj,
        'contrast':     contrast_adj,
        'saturation':   saturation_adj,
        'r_shift':      r_shift,
        'g_shift':      g_shift,
        'b_shift':      b_shift,
        'ref_temp':     ref_temp,
        'raw_temp':     raw_temp,
        'needs_grading': (
            abs(brightness_adj) > 0.02 or
            abs(contrast_adj - 1.0) > 0.05 or
            abs(saturation_adj - 1.0) > 0.1 or
            abs(r_shift) > 0.02 or
            abs(b_shift) > 0.02
        ),
    }

    print(f"   🎨 Grading params:")
    print(f"      brightness: {brightness_adj:+.3f} "
          f"(ref={ref.get('avg_brightness'):.0f} raw={raw.get('avg_brightness'):.0f})")
    print(f"      contrast:   {contrast_adj:.3f}x "
          f"(ref={ref.get('avg_contrast'):.0f} raw={raw.get('avg_contrast'):.0f})")
    print(f"      saturation: {saturation_adj:.3f}x "
          f"(ref={ref.get('avg_saturation'):.0f} raw={raw.get('avg_saturation'):.0f})")
    print(f"      rgb shift:  R{r_shift:+.3f} G{g_shift:+.3f} B{b_shift:+.3f}")
    print(f"      temp:       {raw_temp} → {ref_temp}")
    print(f"      needs grading: {params['needs_grading']}")

    return params


def apply_color_grade(input_video: str,
                      output_video: str,
                      grading_params: dict) -> bool:
    """
    Applies color grading to a video using FFmpeg filters.

    Builds a filter chain:
      eq (brightness/contrast/saturation) →
      colorbalance (RGB channel shifts) →
      video copy to output

    Returns True on success, False on failure.
    The input video is not modified.
    """
    if not grading_params or not grading_params.get('needs_grading', False):
        print("   ℹ️  No significant color difference — skipping grading")
        # Just copy the file
        try:
            import shutil
            shutil.copy2(input_video, output_video)
            return True
        except Exception as e:
            print(f"   ❌ Copy failed: {e}")
            return False

    brightness  = grading_params['brightness']
    contrast    = grading_params['contrast']
    saturation  = grading_params['saturation']
    r_shift     = grading_params['r_shift']
    g_shift     = grading_params['g_shift']
    b_shift     = grading_params['b_shift']

    # Build FFmpeg eq filter string
    # eq: brightness (-1 to 1), contrast (0-2), saturation (0-3), gamma (0.1-10)
    eq_filter = (
        f"eq=brightness={brightness}:"
        f"contrast={contrast}:"
        f"saturation={saturation}:"
        f"gamma=1.0"
    )

    # Build colorbalance filter for RGB channel shifts
    # rs/gs/bs = shadows, rm/gm/bm = midtones, rh/gh/bh = highlights
    cb_filter = (
        f"colorbalance="
        f"rs={r_shift}:gs={g_shift}:bs={b_shift}:"
        f"rm={r_shift*0.7:.3f}:gm={g_shift*0.7:.3f}:bm={b_shift*0.7:.3f}:"
        f"rh={r_shift*0.3:.3f}:gh={g_shift*0.3:.3f}:bh={b_shift*0.3:.3f}"
    )

    # Combined filter chain
    vf_chain = f"{eq_filter},{cb_filter}"

    command = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-vf", vf_chain,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "copy",
        output_video
    ]

    print(f"   🎨 Applying color grade...")
    print(f"      Filter: {eq_filter}")

    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True
        )
        print(f"   ✅ Color grading applied: {output_video}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Color grading failed:\n{e.stderr[-300:]}")
        return False


def apply_lut(input_video: str, output_video: str,
              lut_type: str = 'cinematic') -> bool:
    """
    Applies a built-in LUT (Look-Up Table) preset using FFmpeg's lut3d filter.

    Instead of requiring external .cube files, we simulate LUT effects
    using FFmpeg's built-in curve adjustments.

    lut_type options:
      'cinematic'  — slight desaturation, lifted shadows, warm highlights
      'bright'     — increased brightness and saturation
      'matte'      — reduced contrast, faded blacks
      'vivid'      — high saturation and contrast
      'cool'       — blue-shifted, slightly desaturated
    """
    lut_filters = {
        'cinematic': (
            "eq=saturation=0.85:contrast=1.1:brightness=0.02,"
            "colorbalance=rs=0.05:gs=0.0:bs=-0.05:"
            "rm=0.03:gm=0.0:bm=-0.02:rh=0.0:gh=0.0:bh=0.0,"
            "curves=r='0/10 128/138 255/245':g='0/5 128/128 255/250'"
        ),
        'bright': (
            "eq=brightness=0.08:contrast=1.05:saturation=1.3,"
            "colorbalance=rs=0.0:gs=0.0:bs=0.0:rm=0.0:gm=0.0:bm=0.0"
        ),
        'matte': (
            "eq=contrast=0.85:saturation=0.9:brightness=0.05,"
            "curves=r='0/30 255/225':g='0/20 255/230':b='0/25 255/220'"
        ),
        'vivid': (
            "eq=saturation=1.5:contrast=1.15:brightness=-0.02,"
            "colorbalance=rs=-0.03:gs=0.0:bs=-0.03:rm=0.0:gm=0.0:bm=0.0"
        ),
        'cool': (
            "eq=saturation=0.9:contrast=1.05:brightness=0.0,"
            "colorbalance=rs=-0.05:gs=-0.02:bs=0.1:"
            "rm=-0.03:gm=0.0:bm=0.07:rh=-0.02:gh=0.0:bh=0.05"
        ),
    }

    vf = lut_filters.get(lut_type, lut_filters['cinematic'])

    command = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        output_video
    ]

    print(f"   🎨 Applying '{lut_type}' LUT preset...")
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        print(f"   ✅ LUT applied: {output_video}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"   ❌ LUT failed: {e.stderr[-200:]}")
        return False


if __name__ == "__main__":
    # Quick smoke test of parameter computation
    ref_profile = {
        'avg_brightness':   180,
        'avg_contrast':     60,
        'avg_saturation':   140,
        'avg_rgb':          [210, 160, 100],
        'color_temperature':'warm',
    }
    raw_profile = {
        'avg_brightness':   120,
        'avg_contrast':     45,
        'avg_saturation':   100,
        'avg_rgb':          [130, 130, 140],
        'color_temperature':'cool',
    }
    params = compute_grading_params(ref_profile, raw_profile)
    print(f"\nComputed params: {params}")
    assert params['needs_grading'] == True
    assert params['brightness'] > 0      # ref is brighter
    assert params['saturation'] > 1.0    # ref is more saturated
    assert params['r_shift'] > 0         # ref is warmer (more red)
    assert params['b_shift'] < 0         # raw is cooler (less blue needed)
    print("✅ All parameter assertions passed")
