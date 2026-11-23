# ---------------------------------------------------------------------------
# Color Grader
# ---------------------------------------------------------------------------
# Analyses the color profile of a reference video and applies matching
# color adjustments to the output video using FFmpeg's color filters.
#
# Satisfies: "color grading" requirement from Sigma Bytecraft project scope.
#
# Pipeline position: edit_render.cut puts the filter on each piece of the cut,
# before captions, so caption colours stay exact.
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

    print("   🎨 Grading params:")
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


# The built-in looks: LUT-style grades made of curves, so no .cube files are needed.
LOOKS = {
    'cinematic': (
        "eq=saturation=0.85:contrast=1.1:brightness=0.02,"
        "colorbalance=rs=0.05:gs=0.0:bs=-0.05:"
        "rm=0.03:gm=0.0:bm=-0.02:rh=0.0:gh=0.0:bh=0.0,"
        "curves=r='0/0.0392 0.502/0.5412 1/0.9608':g='0/0.0196 0.502/0.502 1/0.9804'"
    ),
    'bright': (
        "eq=brightness=0.08:contrast=1.05:saturation=1.3,"
        "colorbalance=rs=0.0:gs=0.0:bs=0.0:rm=0.0:gm=0.0:bm=0.0"
    ),
    'matte': (
        "eq=contrast=0.85:saturation=0.9:brightness=0.05,"
        "curves=r='0/0.1176 1/0.8824':g='0/0.0784 1/0.902':b='0/0.098 1/0.8627'"
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


def grade_filter(grade: str, params: dict | None = None) -> str | None:
    """The ffmpeg filter for a grade, or None when the picture stays as it is."""
    if grade != "match_reference":
        return LOOKS.get(grade)
    if not params or not params.get("needs_grading"):
        return None
    r, g, b = params["r_shift"], params["g_shift"], params["b_shift"]
    return (f"eq=brightness={params['brightness']}:contrast={params['contrast']}:"
            f"saturation={params['saturation']}:gamma=1.0,"
            f"colorbalance=rs={r}:gs={g}:bs={b}:rm={r*0.7:.3f}:gm={g*0.7:.3f}:bm={b*0.7:.3f}:"
            f"rh={r*0.3:.3f}:gh={g*0.3:.3f}:bh={b*0.3:.3f}")
