import media_probe


def _seg_duration(i, a, b, real_durations):
    """
    Returns how much edited-timeline time segment i of keep_segments
    actually contributes.

    Normally that's just the theoretical (b - a). But FFmpeg's -ss/-t
    re-encode doesn't land on exactly the requested duration — it rounds
    up to the next frame/audio-block boundary, typically 60-100ms of
    overshoot per clip — and that overshoot accumulates across many cuts,
    making captions drift later and later out of sync with the speaker as
    the video progresses.

    real_durations, when supplied (see edit_render.Cut.real_durations),
    is a list the same length as keep_segments holding each segment's ACTUAL
    encoded clip duration, measured via ffprobe after extraction. Using it
    here instead of (b - a) keeps the accumulated offset matching the real
    stitched video, so captions stay aligned with the words being spoken.
    """
    if real_durations is not None and i < len(real_durations) and real_durations[i] is not None:
        return real_durations[i]
    return b - a


CAPTION_CHARS_PER_LINE = 32   # a comfortable reading line for burned-in captions


def auto_caption_groups(words, lines=2):
    """Group (text, start, end, ...) words into natural captions.

    Words are first split into phrases at sentence ends and pauses; a phrase
    too long for ``lines`` reading lines (or on screen over 6 s) is divided
    into the fewest evenly sized captions that fit, so no word is orphaned.
    """
    budget = CAPTION_CHARS_PER_LINE * max(1, lines)
    phrases, cur = [], []
    for w in words:
        if cur and (cur[-1][0][-1:] in ".?!" or w[1] - cur[-1][2] > 0.7):
            phrases.append(cur)
            cur = []
        cur.append(w)
    if cur:
        phrases.append(cur)
    groups = []
    for ph in phrases:
        total = len(" ".join(x[0] for x in ph))
        k = max(-(-total // budget), -(-int((ph[-1][2] - ph[0][1]) * 10) // 60), 1)
        groups.extend(_balanced_chunks(ph, k))
    return groups


def _balanced_chunks(words, k):
    """Split words into k runs of similar character length."""
    if k <= 1 or len(words) <= 1:
        return [words]
    target = len(" ".join(x[0] for x in words)) / k
    chunks, cur, length = [], [], 0
    for w in words:
        add = len(w[0]) + (1 if cur else 0)
        clause_end = cur and cur[-1][0][-1:] in ",;:" and length >= 0.6 * target
        if cur and len(chunks) < k - 1 and (clause_end or abs(length + add - target) > abs(length - target)):
            chunks.append(cur)
            cur, length, add = [], 0, len(w[0])
        cur.append(w)
        length += add
    chunks.append(cur)
    return chunks


def split_caption_lines(tokens, lines=2):
    """Up to ``lines`` lines: one line if it fits, else two balanced lines
    (preferring to break after punctuation)."""
    text = " ".join(tokens)
    if lines < 2 or len(tokens) < 2 or len(text) <= CAPTION_CHARS_PER_LINE:
        return [text]
    best, best_score = 1, None
    for i in range(1, len(tokens)):
        a, b = " ".join(tokens[:i]), " ".join(tokens[i:])
        score = max(len(a), len(b)) - (4 if tokens[i - 1][-1:] in ",.;:?!" else 0)
        if best_score is None or score < best_score:
            best, best_score = i, score
    return [" ".join(tokens[:best]), " ".join(tokens[best:])]


def create_remapped_srt(transcription_data, keep_segments, srt_filename="subtitles_remapped.srt",
                        words_per_caption=0, caption_lines=2, style=None, real_durations=None):
    """
    Generates a subtitle file whose timestamps match the EDITED video timeline,
    not the original.

    The problem with burning the original SRT into cut/compressed clips:
      - Each extracted clip resets its internal clock to t=0.
      - But the SRT still references original timestamps (e.g. 00:00:05,000).
      - FFmpeg therefore shows whichever subtitle was at t=5s in the original,
        even if the clip is now playing speech from t=12s — causing repeats,
        overlaps, and wrong captions.

    This function walks the keep_segments list (the edited timeline) and
    maps every Whisper segment whose original time falls inside a kept region
    to its new time in the output video.

    keep_segments: list of (orig_start, orig_end) tuples — same list passed
                   to edit_render.cut.
    """

    def format_time(seconds):
        seconds = max(0.0, seconds)
        hours   = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs    = int(seconds % 60)
        millis  = int((seconds - int(seconds)) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def original_to_edited(orig_time, keep_segs):
        """
        Converts an original-video timestamp to its position in the edited video.

        We accumulate how much time has been kept before the segment containing
        orig_time, then add the offset within that segment.
        Returns None if orig_time falls inside a cut region.
        """
        edited_offset = 0.0
        for i, (seg_start, seg_end) in enumerate(keep_segs):
            seg_duration = _seg_duration(i, seg_start, seg_end, real_durations)
            if seg_start <= orig_time <= seg_end:
                # orig_time is inside this kept segment
                return edited_offset + (orig_time - seg_start)
            if orig_time < seg_start:
                # orig_time is in a gap (cut region) — skip this subtitle
                return None
            # orig_time is past this segment — accumulate its (real, not
            # theoretical) duration so later captions don't drift out of sync.
            edited_offset += seg_duration
        return None  # past the end of all kept segments

    whisper_segments = transcription_data.get('segments', [])

    remapped = []
    wpc = int(words_per_caption or 0)
    words = []
    for si, seg in enumerate(whisper_segments):
        for w in seg.get('words', []):
            txt = (w.get('word') or '').strip()
            ns = original_to_edited(float(w.get('start', seg['start'])), keep_segments)
            ne = original_to_edited(float(w.get('end', seg['end'])), keep_segments)
            if not txt or ns is None or ne is None:
                continue
            if ne <= ns:
                ne = ns + 0.05
            words.append((txt, ns, ne, si))
    if words:
        if transcription_data.get('respect_segments'):
            # edited caption list: every caption stays exactly one cue (no re-grouping)
            _groups, _cur = [], []
            for wd in words:
                if _cur and wd[3] != _cur[-1][3]:
                    _groups.append(_cur); _cur = []
                _cur.append(wd)
            if _cur: _groups.append(_cur)
        elif wpc > 0:
            _groups = [words[i:i + wpc] for i in range(0, len(words), wpc)]
        else:
            _groups = auto_caption_groups(words, int(caption_lines))
        # Line spacing for this layout: ASS has no native per-line "leading"
        # style field, so an above-default gap is produced by inserting an
        # invisible spacer "line" (alpha-hidden glyph, sized to the extra
        # pixels wanted) between the two real lines, then a bare \r resets
        # every override back to the Default style before the second real line.
        _line_sp = float(getattr(style, 'line_spacing', 1.0) or 1.0)
        _base_fs = int(getattr(style, 'font_size', 28) or 28)
        _extra_px = max(1, round(_base_fs * 1.2 * (_line_sp - 1.0))) if _line_sp > 1.0 else 0
        for grp in _groups:
            lines = split_caption_lines([g[0] for g in grp], int(caption_lines))
            sep = f"\n{{\\alpha&HFF&\\fs{_extra_px}}}\u00b7{{\\r}}\n" if _extra_px else "\n"
            text = "{\\q2}" + sep.join(lines)  # \q2: no automatic wrapping beyond our lines
            remapped.append({'start': grp[0][1], 'end': max(grp[-1][2], grp[0][1] + 0.2), 'text': text})
        # avoid overlaps between consecutive captions
        for a, b in zip(remapped, remapped[1:]):
            if a['end'] > b['start']:
                a['end'] = max(a['start'] + 0.05, b['start'])
    if not remapped:
        for seg in whisper_segments:
            new_start = original_to_edited(seg['start'], keep_segments)
            new_end   = original_to_edited(seg['end'],   keep_segments)
            if new_start is None or new_end is None:
                continue
            if new_end <= new_start:
                new_end = new_start + 0.5
            remapped.append({'start': new_start, 'end': new_end, 'text': seg['text'].strip()})

    with open(srt_filename, "w", encoding="utf-8") as f:
        for index, entry in enumerate(remapped):
            f.write(f"{index + 1}\n")
            f.write(f"{format_time(entry['start'])} --> {format_time(entry['end'])}\n")
            f.write(f"{entry['text']}\n\n")

    print(f"   ✅ Remapped subtitles saved: {srt_filename} ({len(remapped)} entries)")
    return srt_filename


# ---------------------------------------------------------------------------
# Animated ASS subtitle helpers (Reels / Shorts / TikTok style)
# ---------------------------------------------------------------------------
# ASS colour format: &HAABBGGRR   (alpha, blue, green, red — note BGR order)
#   White      = &H00FFFFFF
#   Yellow     = &H0000FFFF   ← active-word highlight
#   Dim white  = &H80FFFFFF   ← upcoming words (semi-transparent)
# ---------------------------------------------------------------------------

_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: AnimSub,{font},60,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,{alignment},{ml},{mr},60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def _x_margins(pos_x, W, base):
    """Left/right margins that centre a caption block on pos_x (0-1) instead of the frame centre.
    pos_x == 0.5 returns (base, base) so the default look is byte-for-byte unchanged."""
    try:
        px = float(pos_x)
    except (TypeError, ValueError):
        px = 0.5
    if abs(px - 0.5) < 1e-6:
        return base, base
    cx = W * px
    hw = max(W * 0.1, min(cx, W - cx) - base)
    return max(0, int(round(cx - hw))), max(0, int(round(W - cx - hw)))


_CHUNK_SIZE    = 4   # words per on-screen subtitle chunk
_ASS_ALIGNMENT = 2   # 2=bottom-centre (standard), 8=top-centre (Instagram Reels)


def _ass_time(seconds: float) -> str:
    """Convert float seconds to ASS timestamp H:MM:SS.cs (centiseconds)."""
    seconds = max(0.0, seconds)
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    cs = int((seconds - int(seconds)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _build_ass_events(word_chunks: list, letter_spacing: float = 0.0) -> list:
    """
    For each chunk, emit one Dialogue line per word.
    During word W's slot:
      • words before W → white  &H00FFFFFF
      • word W itself  → yellow &H0000FFFF + bold  (active highlight)
      • words after W  → dim    &H80FFFFFF

    letter_spacing: extra pixels between characters (ASS \\fsp). 0 (default)
    adds no tag at all, so existing output is unchanged.
    """
    fsp_tag = f"\\fsp{letter_spacing:g}" if letter_spacing else ""
    events = []
    for chunk in word_chunks:
        if not chunk:
            continue
        for w_idx, (word, w_start, w_end) in enumerate(chunk):
            parts = []
            for i, (w, _, _) in enumerate(chunk):
                clean = w.strip().replace("{", "").replace("}", "")
                if i < w_idx:
                    parts.append(f"{{\\c&H00FFFFFF&{fsp_tag}}}{clean}")
                elif i == w_idx:
                    parts.append(f"{{\\c&H0000FFFF&{fsp_tag}}}{{\\b1}}{clean}{{\\b0}}")
                else:
                    parts.append(f"{{\\c&H80FFFFFF&{fsp_tag}}}{clean}")
            text = " ".join(parts) + "{\\c&H00FFFFFF&}"
            events.append(
                f"Dialogue: 0,{_ass_time(w_start)},{_ass_time(w_end)},"
                f"AnimSub,,0000,0000,0000,,{text}"
            )
    return events


def _words_to_chunks(all_words: list, size: int = 0) -> list:
    """Split flat (word, start, end) list into fixed-size chunks."""
    size = int(size) if size else _CHUNK_SIZE
    return [all_words[i: i + size] for i in range(0, len(all_words), size)]


def create_remapped_ass(transcription_data: dict,
                        keep_segments: list,
                        ass_filename: str = "subtitles_animated_remapped.ass",
                        font: str = "Arial",
                        alignment: int = _ASS_ALIGNMENT,
                        chunk_size: int = 0,
                        real_durations: list = None,
                        letter_spacing: float = 0.0,
                        pos_x: float = 0.5) -> str:
    """
    Word-by-word animated captions, timed on the edited timeline.
    Words inside cut regions are discarded.
    Returns the path to the written .ass file.
    """
    def original_to_edited(orig_time: float):
        edited_offset = 0.0
        for i, (seg_start, seg_end) in enumerate(keep_segments):
            seg_dur = _seg_duration(i, seg_start, seg_end, real_durations)
            if seg_start <= orig_time <= seg_end:
                return edited_offset + (orig_time - seg_start)
            if orig_time < seg_start:
                return None
            edited_offset += seg_dur
        return None

    segments  = transcription_data.get("segments", [])
    all_words = []
    for seg in segments:
        for w in seg.get("words", []):
            word  = w.get("word", "").strip()
            ns    = original_to_edited(float(w.get("start", seg["start"])))
            ne    = original_to_edited(float(w.get("end",   seg["end"])))
            if ns is None or ne is None:
                continue
            if ne <= ns:
                ne = ns + 0.05
            if word:
                all_words.append((word, ns, ne))

    if not all_words:
        print("   ⚠️  No remapped words — animated ASS will be empty.")
        with open(ass_filename, "w", encoding="utf-8") as f:
            f.write(_ASS_HEADER.format(font=font, alignment=alignment, ml=_x_margins(pos_x, 1920, 60)[0], mr=_x_margins(pos_x, 1920, 60)[1]))
        return ass_filename

    chunks = _words_to_chunks(all_words, chunk_size)
    events = _build_ass_events(chunks, letter_spacing=letter_spacing)

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(_ASS_HEADER.format(font=font, alignment=alignment, ml=_x_margins(pos_x, 1920, 60)[0], mr=_x_margins(pos_x, 1920, 60)[1]))
        f.write("\n".join(events) + "\n")

    print(f"   ✅ Remapped animated ASS: {ass_filename} "
          f"({len(all_words)} words, {len(chunks)} chunks)")
    return ass_filename

# ---------------------------------------------------------------------------
# "Neon Pop" animated captions
# Small white words scattered around a BIG neon-lime keyword:
#     me                <- words before the keyword: small, above-left
#        FEELING        <- keyword: big, CAPS, neon glow
#              as if    <- words after the keyword: small, below-right
# Captions move to a slightly different spot each time, and every word pops in.
# ---------------------------------------------------------------------------
_POP_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,{font},{size},{primary},&H000000FF,&H00000000,&H78000000,{bold},{italic},0,0,100,100,0,0,1,0,2,5,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _size_ref(W, H):
    """Height that caption sizes scale with: the real height for portrait video; for
    landscape, 1.6x the short side, so text reads the same size as on a vertical video."""
    return H if H >= W else min(W, H) * 1.6


def _probe_dims(video_path):
    return media_probe.frame_size(video_path)


LIME_LANDSCAPE_PLAIN = 1.35


def create_remapped_pop_ass(transcription_data, keep_segments,
                            ass_filename="subtitles_pop_remapped.ass",
                            words_per_caption=3, caption_lines=1,
                            font="Arial", size=None, emph_colour="&H0030EAB4",
                            video_path=None, style=None, real_durations=None):
    W, H = _probe_dims(video_path) if video_path else (1080, 1920)
    g = lambda k, d: getattr(style, k, d) if style is not None else d
    font = g('font_name', font)
    primary = g('primary_colour', '&H00FFFFFF')
    bold_f = -1 if g('bold', True) else 0
    ital_f = -1 if g('italic', False) else 0
    emph_colour = g('emph_colour', emph_colour)
    emph_font = g('emph_font', 'Arial Black')
    emph_scale = float(g('emph_scale', 2.0))
    emph_glow = bool(g('emph_glow', True))
    emph_italic = bool(g('emph_italic', False))
    emph_every = max(1, int(g('emph_every', 2)))
    lime = (str(g('emph_look', 'classic')) == 'lime')   # 'lime' = Lime Pop look; anything else = original (Red Editorial / custom) look
    pos_y = float(g('pos_y', 0.53))
    pos_x = min(0.85, max(0.15, float(g('pos_x', 0.5))))   # 0.5 = centred (unchanged)
    ref = _size_ref(W, H)
    small = int(ref * float(g('font_size', 20)) / 450.0)
    keyword_base = small
    if lime and W > H:      # Montserrat's small x-height left the plain words unreadable on a wide frame
        small = int(small * LIME_LANDSCAPE_PLAIN)
    big_max = int(ref * (0.125 if lime else 0.30))
    # Letter spacing (ASS \\fsp) and line spacing — at the defaults (0 / 1.0)
    # these are exact no-ops: fsp_tag is empty and every "* line_sp" below
    # multiplies by 1.0, which floating point performs exactly, so Lime
    # Pop's fixed position and Red Editorial's current look are both
    # unchanged unless the user actually moves these sliders.
    letter_sp = float(g('letter_spacing', 0.0) or 0.0)
    line_sp   = float(g('line_spacing', 1.0) or 1.0)
    fsp_tag   = f"\\fsp{letter_sp:g}" if letter_sp else ""

    def o2e(t):
        off = 0.0
        for i, (a, b) in enumerate(keep_segments):
            if a <= t <= b:
                return off + (t - a)
            if t < a:
                return None
            off += _seg_duration(i, a, b, real_durations)
        return None

    words = []
    for si, seg in enumerate(transcription_data.get("segments", [])):
        for w in seg.get("words", []):
            txt = (w.get("word") or "").strip()
            ns = o2e(float(w.get("start", seg["start"])))
            ne = o2e(float(w.get("end", seg["end"])))
            if not txt or ns is None or ne is None:
                continue
            words.append((txt, ns, max(ne, ns + 0.05), si))

    def clean(w):
        return w.strip(".,!?;:\"'")

    wpc = max(1, int(words_per_caption or 3))
    STOP = set("""a an the and or but so if of to in on at by for with from as is are was were be been am it its it's i i'm i've i'll i'd you your you're we they he she me my mine our us them him her his this that these those there here what when where who how why not no yes do does did done have has had will would can could should just very really then than too also about into out up down over again all any some more most much many oh okay ok like got get before after because while every thing things going gonna well know see said says say let lets make made way one two""".split())
    def wwidth(fontname):
        # Average uppercase advance as a fraction of libass \fs — ONE table
        # for both looks, because the glyph width of a given font doesn't
        # change depending on which template is using it. This used to be
        # two separate, unverified guesses (e.g. Anton at 0.22 on the
        # classic path and 0.38 on the lime path); re-measured by rendering
        # real glyphs at a known \fs and reading back the ink bounding box,
        # and biased toward the WIDE end of what was measured (not the
        # average) since a word like "WORKS," measures ~0.57-0.63/fs even
        # though "TIME"/"FEELING" measure ~0.50 — an average constant still
        # lets wide-letter words overflow. The bsize fit below also clamps
        # against the real measured width as a second safety net, so a
        # remaining per-word estimate error shrinks the font instead of
        # letting the text run off the frame.
        f = (fontname or "").lower()
        if "montserrat" in f: return 0.50
        if "black" in f or "heavy" in f: return 0.66
        if "impact" in f or "anton" in f or "condensed" in f or "bebas" in f or "oswald" in f: return 0.64
        if "georgia" in f or "times" in f or "serif" in f: return 0.48
        return 0.58
    KW = wwidth(emph_font)

    # ---- Lime Pop only: keep every piece of text inside the safe-render area ----
    # A 9:16 Reels/TikTok/Shorts frame has platform UI (profile bar, like/share
    # rail, caption bar) covering roughly the top ~11% and bottom ~23% of the
    # frame, so captions need to stay clear of both. These are only used on the
    # `lime` path below — the Red Editorial ('classic') look is untouched.
    SAFE_TOP, SAFE_BOTTOM = 0.05, 0.95   # keep text on screen; the Y slider decides where it sits
    MIN_FS = max(10, int(ref * 0.02))

    def _fit_pop_line(n_chars, base_fs, avail_w, char_w_frac=0.46):
        """
        Returns (fs, width) for a line of text estimated at n_chars characters.
        If the line would be wider than avail_w at base_fs, the font size is
        shrunk (down to MIN_FS) so it actually fits — instead of letting the
        text run off the edge of the frame, which is what the old code did:
        it only repositioned the text's center, and once the estimated width
        exceeded the frame width that repositioning became self-contradictory
        and the text overflowed one side by however much it was too wide.
        """
        tw = n_chars * base_fs * char_w_frac
        if tw <= avail_w or n_chars <= 0:
            return base_fs, tw
        fs = max(MIN_FS, int(base_fs * avail_w / tw))
        return fs, n_chars * fs * char_w_frac

    # ---- group words into natural phrases (max wpc words, break on punctuation / pauses) ----
    if transcription_data.get('respect_segments'):
        # edited caption list: each caption is one group (edits to text/timing are honoured exactly)
        groups, cur = [], []
        for w in words:
            if cur and w[3] != cur[-1][3]:
                groups.append(cur); cur = []
            cur.append(w)
        if cur: groups.append(cur)
    elif lime:
        # words_per_caption is a hard ceiling here: a group never holds more
        # than wpc words, so selecting "3" in the UI always shows at most 3
        # words at a time. (The previous version let a group grow past wpc —
        # up to wpc+3 — whenever the words so far hadn't been on screen long
        # enough yet, which is why a "3 words per caption" setting could show
        # 4+ words together on fast speech.) Groups can still end up SHORTER
        # than wpc on a sentence end or a long pause — that's still the right
        # behavior, it just never goes over.
        groups, cur = [], []
        def _dur(c): return c[-1][2] - c[0][1]
        for w in words:
            if cur:
                gap = w[1] - cur[-1][2]
                full = len(cur) >= wpc
                hard = gap > 0.7            # still break early on a long pause
                if full or hard:
                    groups.append(cur); cur = []
            cur.append(w)
            if w[0][-1:] in ".!?" and len(cur) >= 2 and _dur(cur) >= 0.6:
                groups.append(cur); cur = []
            elif w[0][-1:] == "," and len(cur) >= 2:
                groups.append(cur); cur = []
        if cur:
            groups.append(cur)

    else:
        groups, cur = [], []
        for w in words:
            if cur and (len(cur) >= wpc or w[1] - cur[-1][2] > 0.7):
                groups.append(cur); cur = []
            cur.append(w)
            if w[0][-1:] in ".!?" or (w[0][-1:] == "," and len(cur) >= 2):
                groups.append(cur); cur = []
        if cur:
            groups.append(cur)


    events = []
    kw_count = 0
    for gi, chunk in enumerate(groups):
        n = len(chunk)
        g_start = chunk[0][1]
        g_end = chunk[-1][2] + (0.85 if lime else 0.25)
        if gi + 1 < len(groups):
            g_end = min(g_end, groups[gi + 1][0][1] - (0.04 if lime else 0.0))
        g_end = max(g_end, g_start + 0.2)

        # keyword = longest "content" word (not a filler/stop word)
        cands = [k for k in range(n) if len(clean(chunk[k][0])) >= 4 and clean(chunk[k][0]).lower() not in STOP]
        emph = -1
        if cands and emph_every > 0:
            if kw_count % emph_every == 0:
                emph = max(cands, key=lambda k: (len(clean(chunk[k][0])), k))
            kw_count += 1

        cx = W * pos_x
        cy = H * pos_y
        units = []
        if emph < 0:
            if lime:
                # Shrink-to-fit instead of just repositioning: the old formula
                # only moved the text's center when it was too wide, which — once
                # the estimated width exceeded the frame — produced a position
                # that still ran text off one edge (this was the main cause of
                # text going outside the frame on 3-word captions with no
                # emphasized keyword).
                margin = W * 0.05
                n_chars = sum(len(chunk[k][0]) + 1 for k in range(n))
                avail = W - 2 * margin
                if abs(pos_x - 0.5) > 1e-6:       # moved off-centre: only the room on the near side is usable
                    avail = max(W * 0.3, min(avail, 2 * (min(W * pos_x, W - W * pos_x) - margin)))
                fs_line, tw = _fit_pop_line(n_chars, small, avail)
                cx = min(max(cx, tw / 2 + margin), W - tw / 2 - margin)
                cy = min(max(cy, H * SAFE_TOP + fs_line * 0.5), H * SAFE_BOTTOM - fs_line * 0.5)
                units.append((list(range(n)), cx, cy, 5, fs_line, False))
            else:
                tw = sum(len(chunk[k][0]) + 1 for k in range(n)) * small * 0.58
                cx = min(max(cx, tw / 2 + W * 0.05), W - tw / 2 - W * 0.05)
                units.append((list(range(n)), cx, cy, 5, small, False))
        else:
            kw = clean(chunk[emph][0])
            # Size off the word as it will actually be displayed (with any
            # trailing punctuation, e.g. "WORKS,") rather than the clean()'d
            # form used only for candidate-picking above — a trailing comma or
            # period still takes up a sliver of width that clean() ignores,
            # which on the classic (non-lime) path previously had nothing to
            # correct for it.
            kw_len = len(kw) if lime else max(len(kw), len(chunk[emph][0]))
            # reference look: keyword auto-fits ~82% of the frame width (short words are capped by height / size slider)
            target_w = W * (0.82 if lime else 0.88)
            room_w = W - 2 * (W * 0.05)
            if abs(pos_x - 0.5) > 1e-6:       # moved off-centre: keyword must fit in the room on the near side
                room_w = max(W * 0.3, 2 * (min(W * pos_x, W - W * pos_x) - W * 0.05))
                target_w = min(target_w, room_w)
            bsize = int(min(big_max, keyword_base * emph_scale, target_w / (max(kw_len, 1) * KW)))
            bw = kw_len * bsize * KW
            # Safety net: if this word's real estimated width still doesn't
            # fit (big_max or emph_scale was the binding constraint instead
            # of the width term, so the width check above never applied),
            # shrink bsize directly against the frame rather than letting it
            # overflow — this is what actually caught and fixed Lime Pop's
            # share of the bug (its big_max is small enough that the width
            # term rarely won, which is exactly why the wrong KW went
            # unnoticed there until this check started rendering real words).
            frame_budget = room_w
            if bw > frame_budget:
                bsize = max(MIN_FS, int(bsize * frame_budget / bw))
                bw = kw_len * bsize * KW
            cx = min(max(cx, bw / 2 + W * 0.05), W - bw / 2 - W * 0.05)
            if lime:
                # keep the keyword's own vertical extent inside the safe zone too
                cy = min(max(cy, H * SAFE_TOP + bsize * 0.5), H * SAFE_BOTTOM - bsize * 0.5)
            left, right = cx - bw / 2, cx + bw / 2
            pre, post = list(range(0, emph)), list(range(emph + 1, n))
            if lime:
                margin = W * 0.05
                # Clearance, calibrated by rendering isolated glyphs and
                # measuring actual ink pixel rows (not nominal \fs): a
                # keyword's true visual half-height is ~0.25x its \fs, far
                # less than the 0.38x this used before — that gap between
                # assumed and true size is exactly why the spacing still
                # looked too large after the first reduction. The small-text
                # term is subtracted (not added): pre/post units are
                # anchored \an1/\an9, whose anchor point already sits
                # ~0.17x its own \fs beyond the visible glyph edge, so a
                # larger small-font actually needs LESS extra clearance, not
                # more. Re-verified empirically (incl. worst-case
                # ascender+descender word combos) to keep a small but
                # positive non-overlap gap at every size instead of guessing.
                clearance = max(bsize * 0.32 - small * 0.08, small * 0.32) * line_sp
                if pre:
                    pre_chars = sum(len(chunk[k][0]) + 1 for k in pre)
                    pre_fs, pre_tw = _fit_pop_line(pre_chars, small, W - 2 * margin)
                    pre_x = min(max(left + small * 0.05, margin), W - margin - pre_tw)
                    pre_y = min(max(cy - clearance, H * SAFE_TOP + pre_fs * 0.5), H * SAFE_BOTTOM - pre_fs * 0.5)
                    units.append((pre, pre_x, pre_y, 1, pre_fs, False))
                units.append(([emph], cx, cy, 5, bsize, True))
                if post:
                    post_chars = sum(len(chunk[k][0]) + 1 for k in post)
                    post_fs, post_tw = _fit_pop_line(post_chars, small, W - 2 * margin)
                    post_x = min(max(right - small * 0.05, margin + post_tw), W - margin)
                    post_y = min(max(cy + clearance, H * SAFE_TOP + post_fs * 0.5), H * SAFE_BOTTOM - post_fs * 0.5)
                    units.append((post, post_x, post_y, 9, post_fs, False))
            else:
                if pre:
                    units.append((pre, cx, cy - bsize * 0.30 * line_sp, 2, small, False))
                units.append(([emph], cx, cy, 5, bsize, True))
                if post:
                    units.append((post, cx, cy + bsize * 0.30 * line_sp, 8, small, False))

        if lime:
            for (idxs, x, y, an, fs, big) in units:
                ev_start = chunk[idxs[0]][1]
                # Hard ceiling: this piece must disappear before the next
                # caption's first word appears — captions all render at the
                # same screen position, so any time overlap between two
                # groups shows up as garbled double-exposed text. Both the
                # "want to stay longer" target AND the "never vanish too
                # fast" floor are clamped to this ceiling, so neither one is
                # allowed to push the caption past it (that override was the
                # bug: the floor was applied AFTER the ceiling clamp, so it
                # could win and re-introduce the overlap it was supposed to
                # avoid).
                ceiling = (groups[gi + 1][0][1] - 0.04) if gi + 1 < len(groups) else None
                want  = max(g_end, ev_start + (1.3 if big else 0.9))
                floor = ev_start + (0.65 if big else 0.5)
                if ceiling is not None:
                    want  = min(want, ceiling)
                    floor = min(floor, ceiling)
                end = max(want, floor)
                # Degenerate case only: the next word is spoken essentially
                # the instant this one ends, leaving no legal gap at all.
                # Allow a minimal sliver of display instead of a zero/negative
                # -length cue — a few frames of overlap here is far less
                # noticeable than a caption that never renders.
                end = max(end, ev_start + 0.15)
                dur_ms = int((end - ev_start) * 1000)
                # Fade-in/out timings, slowed again on top of the first pass —
                # user feedback was that captions still popped in and
                # vanished too fast to read. Floors and caps raised ~40-55%
                # and the big-text settle/fade-out now claim a larger share
                # of dur_ms, while still capped relative to dur_ms so a very
                # short-lived cue (the degenerate near-overlap case above)
                # never has an animation longer than its own on-screen time.
                fade_in_big   = min(200, max(70, dur_ms // 2))
                settle_big    = min(300, max(140, dur_ms * 2 // 3))
                fade_in_small = min(230, max(90, dur_ms // 2))
                fade_out      = min(300, max(130, dur_ms // 3))
                parts = []
                for j in idxs:
                    txt = chunk[j][0].upper() if big else chunk[j][0]
                    s_ms = int(max(0.0, chunk[j][1] - ev_start) * 1000)
                    if big:
                        heavy = any(k in emph_font.lower() for k in ("black", "extrabold", "heavy", "bold", "anton", "impact", "luckiest", "bangers", "bungee"))
                        look = "\\fn%s\\b%d\\i%d\\c%s&" % (emph_font, 0 if heavy else 1, 1 if emph_italic else 0, emph_colour)
                        if emph_glow:
                            look += "\\3c%s&\\3a&H70&\\bord2" % emph_colour
                        # keyword pops in with a brief settle, then keeps a slow
                        # zoom-in for as long as it is on screen
                        anim = ("\\alpha&H80&\\fscx95\\fscy95\\blur%s"
                                "\\t(%d,%d,\\alpha&H00&)"
                                "\\t(%d,%d,0.4,\\fscx100\\fscy100)"
                                "\\t(%d,%d,\\fscx105\\fscy105)") % ("3" if emph_glow else "0.6", s_ms, s_ms + fade_in_big,
                                                                    s_ms, s_ms + settle_big, s_ms + settle_big, max(s_ms + 500, dur_ms))
                    else:
                        look = ""
                        anim = ("\\alpha&HFF&\\blur0.4"
                                "\\t(%d,%d,\\alpha&H00&)") % (s_ms, s_ms + fade_in_small)
                    parts.append("{%s%s}%s" % (look, anim, txt))
                    if j != idxs[-1]:
                        parts.append(" ")
                if big:
                    place = "\\pos(%d,%d)" % (x, y)
                else:
                    place = "\\move(%d,%d,%d,%d,0,140)" % (x, y + int(small * 0.25), x, y)
                text = "{\\an%d%s\\fs%d%s\\q2\\fad(0,%d)}%s" % (an, place, fs, fsp_tag, fade_out, "".join(parts))
                events.append("Dialogue: 0,%s,%s,Pop,,0,0,0,,%s" % (_ass_time(ev_start), _ass_time(end), text))

        else:
            for (idxs, x, y, an, fs, big) in units:
                ev_start = chunk[idxs[0]][1]
                parts = []
                for j in idxs:
                    txt = chunk[j][0].upper() if big else chunk[j][0]
                    s_ms = int(max(0.0, chunk[j][1] - ev_start) * 1000)
                    if big:
                        look = "\\fn%s\\i%d\\c%s&" % (emph_font, 1 if emph_italic else 0, emph_colour)
                        if emph_glow:
                            look += "\\3c%s&\\3a&H70&\\bord2\\blur4" % emph_colour
                        anim = ("\\alpha&HFF&\\fscx80\\fscy80"
                                "\\t(%d,%d,0.5,\\alpha&H00&)"
                                "\\t(%d,%d,0.6,\\fscx104\\fscy104)"
                                "\\t(%d,%d,\\fscx100\\fscy100)") % (s_ms, s_ms + 110, s_ms, s_ms + 170, s_ms + 170, s_ms + 260)
                    else:
                        look = ""
                        anim = ("\\alpha&HFF&\\fscx90\\fscy90"
                                "\\t(%d,%d,0.5,\\alpha&H00&)"
                                "\\t(%d,%d,0.6,\\fscx100\\fscy100)") % (s_ms, s_ms + 100, s_ms, s_ms + 160)
                    parts.append("{%s%s}%s" % (look, anim, txt))
                    if j != idxs[-1]:
                        parts.append(" ")
                end = max(g_end, ev_start + 0.3)
                text = "{\\an%d\\pos(%d,%d)\\fs%d%s\\q2\\fad(0,120)}%s" % (an, x, y, fs, fsp_tag, "".join(parts))
                events.append("Dialogue: 0,%s,%s,Pop,,0,0,0,,%s" % (_ass_time(ev_start), _ass_time(end), text))


    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(_POP_HEADER.format(w=W, h=H, font=font, size=small, primary=primary, bold=bold_f, italic=ital_f))
        f.write("\n".join(events) + "\n")
    print(f"   ✅ Neon Pop ASS: {ass_filename} ({len(words)} words, {len(events)} events, {W}x{H})")
    return ass_filename


# ─────────────────────────────────────────────────────────────────────────────
# 🔒 LOCKED TEMPLATE — do not change. Its output is frozen by locked_templates/check_locked_templates.py
# (run it after any edit to this file; it must print OK).
# "Highlight Box" template: the whole phrase sits in a white rounded-ish box; words are light grey until they are
# spoken, then turn dark (karaoke-style progressive highlight).
# ─────────────────────────────────────────────────────────────────────────────
_BOX_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Box,{font},{size},{spoken},&H000000FF,{box},&H00000000,{bold},{italic},0,0,100,100,0,0,3,{pad},0,5,{mx},{mx},20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def create_remapped_box_ass(transcription_data, keep_segments,
                            ass_filename="subtitles_box_remapped.ass",
                            words_per_caption=5, font="Poppins Medium", size=None,
                            video_path=None, style=None, real_durations=None):
    W, H = _probe_dims(video_path) if video_path else (1080, 1920)
    g = lambda k, d: getattr(style, k, d) if style is not None else d
    font = g('font_name', font)
    spoken = g('primary_colour', '&H00141414')
    if str(spoken).upper().startswith('&H00FFFFFF'):      # white text would vanish on a white box
        spoken = '&H00141414'
    unspoken = "&H00C8C8C8"
    bold_f = -1 if g('bold', False) else 0
    ital_f = -1 if g('italic', False) else 0
    pos_y = float(g('pos_y', 0.62))
    pos_x = min(0.85, max(0.15, float(g('pos_x', 0.5))))   # 0.5 = centred (unchanged)
    small = max(12, int(_size_ref(W, H) * float(g('font_size', 11)) / 450.0))
    pad = max(3, int(small * 0.30))
    wpc = max(1, int(words_per_caption or 5))
    # Letter/line spacing (see create_remapped_srt for the same reasoning):
    # at the defaults (0 / 1.0) these are complete no-ops, so unmodified
    # callers (incl. the locked-template regression check) get byte-for-byte
    # identical output.
    letter_sp = float(g('letter_spacing', 0.0) or 0.0)
    line_sp   = float(g('line_spacing', 1.0) or 1.0)
    fsp_tag   = f"\\fsp{letter_sp:g}" if letter_sp else ""
    extra_px  = max(1, round(small * 1.2 * (line_sp - 1.0))) if line_sp > 1.0 else 0

    def o2e(t):
        off = 0.0
        for i, (a, b) in enumerate(keep_segments):
            if a <= t <= b:
                return off + (t - a)
            if t < a:
                return None
            off += _seg_duration(i, a, b, real_durations)
        return None

    words = []
    for si, seg in enumerate(transcription_data.get("segments", [])):
        for w in seg.get("words", []):
            txt = (w.get("word") or "").strip()
            ns = o2e(float(w.get("start", seg["start"])))
            ne = o2e(float(w.get("end", seg["end"])))
            if not txt or ns is None or ne is None:
                continue
            words.append((txt, ns, max(ne, ns + 0.05), si))

    groups, cur = [], []
    respect = bool(transcription_data.get("respect_segments"))
    for w in words:
        if cur:
            brk = (w[3] != cur[-1][3]) if respect else (len(cur) >= wpc or w[1] - cur[-1][2] > 0.55)
            if brk:
                groups.append(cur); cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    events = []
    cx, cy = int(W * pos_x), int(H * pos_y)
    maxw = min(W * 0.84, 2 * min(cx, W - cx) - W * 0.08)   # narrower when moved off-centre so text stays on screen
    for gi, chunk in enumerate(groups):
        g_start = max(0.0, chunk[0][1] - 0.12)
        g_end = chunk[-1][2] + 0.35
        if gi > 0:
            g_start = max(g_start, groups[gi - 1][-1][2] + 0.02)
        if gi + 1 < len(groups):
            g_end = min(g_end, groups[gi + 1][0][1] - 0.14)
        g_end = max(g_end, chunk[-1][2] + 0.1, g_start + 0.4)
        # balance into two lines only when the phrase would be wider than the screen allows
        toks = [c[0] for c in chunk]
        est = sum(len(t) + 1 for t in toks) * small * 0.55
        brk_at = None
        if est > maxw and len(toks) >= 2:
            brk_at = (len(toks) + 1) // 2
        parts = []
        for k, c in enumerate(chunk):
            ms = int(max(0.0, c[1] - g_start) * 1000)
            parts.append("{\\1c%s\\t(%d,%d,\\1c%s)}%s" % (unspoken, ms, ms + 90, spoken, c[0]))
            if k != len(chunk) - 1:
                if brk_at is not None and k + 1 == brk_at:
                    if extra_px:
                        parts.append("\\N{\\alpha&HFF&\\fs%d}\u00b7{\\r%s}\\N" % (extra_px, fsp_tag))
                    else:
                        parts.append("\\N")
                else:
                    parts.append(" ")
        text = ("{\\an5\\pos(%d,%d)\\fs%d%s\\bord%d\\3a&H12&\\blur0.6\\shad0\\1c%s\\fad(120,110)}%s"
                % (cx, cy, small, fsp_tag, pad, unspoken, "".join(parts)))
        events.append("Dialogue: 0,%s,%s,Box,,0,0,0,,%s" % (_ass_time(g_start), _ass_time(g_end), text))

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(_BOX_HEADER.format(w=W, h=H, font=font, size=small, spoken=spoken, box="&H00FFFFFF",
                                   bold=bold_f, italic=ital_f, pad=pad, mx=int(W * 0.06)))
        f.write("\n".join(events) + "\n")
    print(f"   ✅ Highlight-box ASS: {ass_filename} ({len(words)} words, {len(events)} events, {W}x{H})")
    return ass_filename


# ─────────────────────────────────────────────────────────────────────────────
# SOFT REVEAL template (emph_look == 'soft', layout 'soft')
# Modelled on a reference clip: small bold white grotesk, words fade/blur in one
# by one and stay until the phrase ends; occasional standalone italic-serif
# "feature" word, larger and slightly translucent, with a soft glow.
# ─────────────────────────────────────────────────────────────────────────────
_SOFT_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Soft,{font},{size},{main},&H000000FF,&H00000000,&H80000000,{bold},{italic},0,0,100,100,0,0,1,0,1,5,{mx},{mx},20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# 🔒 LOCKED TEMPLATE — do not change (Soft Reveal). Guard: locked_templates/check_locked_templates.py
def create_remapped_soft_ass(transcription_data, keep_segments,
                             ass_filename="subtitles_soft_remapped.ass",
                             words_per_caption=5, font="Manrope", size=None,
                             video_path=None, style=None, real_durations=None):
    W, H = _probe_dims(video_path) if video_path else (1080, 1920)
    g = lambda k, d: getattr(style, k, d) if style is not None else d
    font = g('font_name', font)
    main = g('primary_colour', '&H00FFFFFF')
    bold_f = -1 if g('bold', True) else 0
    ital_f = -1 if g('italic', False) else 0
    pos_y = float(g('pos_y', 0.63))
    pos_x = min(0.85, max(0.15, float(g('pos_x', 0.5))))   # 0.5 = centred (unchanged)
    ref = _size_ref(W, H)
    small = max(14, int(ref * float(g('font_size', 19)) / 450.0 * 1.12))
    dsh = max(2, int(small * 0.06))                   # drop-shadow distance
    wpc = max(1, int(words_per_caption or 5))
    use_em = bool(g('emphasis', True))
    em_font = g('emph_font', 'Instrument Serif') or 'Instrument Serif'
    em_scale = min(2.4, max(1.1, float(g('emph_scale', 1.7))))
    em_ital = bool(g('emph_italic', True))
    em_col = g('emph_colour', '&H00FFFFFF') or '&H00FFFFFF'
    em_gap = 3 + 2 * max(1, int(g('emph_every', 1)))       # min. words between two feature words
    # Letter/line spacing — no-ops at the defaults (0 / 1.0), so unmodified
    # callers (incl. the locked-template regression check) are unaffected.
    letter_sp = float(g('letter_spacing', 0.0) or 0.0)
    line_sp   = float(g('line_spacing', 1.0) or 1.0)
    fsp_tag   = f"\\fsp{letter_sp:g}" if letter_sp else ""
    extra_px  = max(1, round(small * 1.2 * (line_sp - 1.0))) if line_sp > 1.0 else 0

    def o2e(t):
        off = 0.0
        for i, (a, b) in enumerate(keep_segments):
            if a <= t <= b:
                return off + (t - a)
            if t < a:
                return None
            off += _seg_duration(i, a, b, real_durations)
        return None

    words = []
    for si, seg in enumerate(transcription_data.get("segments", [])):
        for w in seg.get("words", []):
            txt = (w.get("word") or "").strip()
            ns = o2e(float(w.get("start", seg["start"])))
            ne = o2e(float(w.get("end", seg["end"])))
            if not txt or ns is None or ne is None:
                continue
            words.append((txt, ns, max(ne, ns + 0.05), si))

    respect = bool(transcription_data.get("respect_segments"))
    # pick feature words: long content words, spaced apart
    feat, since = set(), em_gap
    if use_em and not respect:
        for i, w in enumerate(words):
            core = "".join(ch for ch in w[0] if ch.isalpha())
            since += 1
            if len(core) >= 6 and since >= em_gap:
                feat.add(i); since = 0

    groups, cur = [], []                    # each group = list of (idx, word) ; feature groups are singletons
    for i, w in enumerate(words):
        if i in feat:
            if cur: groups.append(cur); cur = []
            groups.append([(i, w)]); continue
        if cur:
            last = cur[-1][1]
            brk = (w[3] != last[3]) if respect else (len(cur) >= wpc or w[1] - last[2] > 0.55)
            if brk:
                groups.append(cur); cur = []
        cur.append((i, w))
    if cur:
        groups.append(cur)

    cx, cy = int(W * pos_x), int(H * pos_y)
    maxw = min(W * 0.84, 2 * min(cx, W - cx) - W * 0.08)   # narrower when moved off-centre so text stays on screen
    events = []
    for gi, chunk in enumerate(groups):
        ws = [c[1] for c in chunk]
        is_feat = len(chunk) == 1 and chunk[0][0] in feat
        g_start = max(0.0, ws[0][1] - 0.06)
        g_end = ws[-1][2] + (0.45 if is_feat else 0.55)
        if gi + 1 < len(groups):
            g_end = min(g_end, groups[gi + 1][0][1][1] - 0.04)
        g_end = max(g_end, ws[-1][2] + 0.1, g_start + 0.5)
        if is_feat:
            fs = int(small * em_scale)
            t = ws[0]
            # feature word: soft rise + fade in, translucent white with a light glow
            txt = ("{\\an5\\pos(%d,%d)\\fn%s\\fs%d%s\\i%d\\1c%s\\1a&H28&\\bord1\\3c&HFFFFFF&\\3a&HB0&\\blur1.6\\shad%d\\4c&H000000&\\4a&H58&"
                   "\\alpha&HFF&\\t(0,260,\\alpha&H28&)\\fscx92\\fscy92\\t(0,700,\\fscx100\\fscy100)\\fad(0,170)}%s"
                   % (cx, cy, em_font, fs, fsp_tag, 1 if em_ital else 0, em_col, dsh, t[0]))
            events.append("Dialogue: 0,%s,%s,Soft,,0,0,0,,%s" % (_ass_time(g_start), _ass_time(g_end), txt))
            continue
        toks = [w[0] for w in ws]
        est = sum(len(t) + 1 for t in toks) * small * 0.40
        brk_at = (len(toks) + 1) // 2 if (est > maxw and len(toks) >= 2) else None
        parts = []
        for k, w in enumerate(ws):
            ms = int(max(0.0, w[1] - g_start - 0.05) * 1000)
            parts.append("{\\alpha&HFF&\\blur3\\t(%d,%d,\\alpha&H00&\\blur1)}%s" % (ms, ms + 230, w[0]))
            if k != len(ws) - 1:
                if brk_at is not None and k + 1 == brk_at:
                    if extra_px:
                        parts.append("\\N{\\alpha&HFF&\\fs%d}\u00b7{\\r%s}\\N" % (extra_px, fsp_tag))
                    else:
                        parts.append("\\N")
                else:
                    parts.append(" ")
        text = ("{\\an5\\pos(%d,%d)\\fs%d%s\\1c%s\\shad%d\\4c&H000000&\\4a&H50&\\fad(0,170)}%s"
                % (cx, cy, small, fsp_tag, main, dsh, "".join(parts)))
        events.append("Dialogue: 0,%s,%s,Soft,,0,0,0,,%s" % (_ass_time(g_start), _ass_time(g_end), text))

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(_SOFT_HEADER.format(w=W, h=H, font=font, size=small, main=main,
                                    bold=bold_f, italic=ital_f, mx=int(W * 0.07)))
        f.write("\n".join(events) + "\n")
    print(f"   ✅ Soft-reveal ASS: {ass_filename} ({len(words)} words, {len(events)} events, {W}x{H})")
    return ass_filename


# ---------------------------------------------------------------------------
# Plain ("Custom") captions placed exactly where the Studio preview shows them.
# Same look as the old SRT + force_style burn (384x288 script canvas, same style numbers), but every caption
# gets an explicit \an/\pos so the position is exact instead of snapping to top / middle / bottom.
# ---------------------------------------------------------------------------
def _srt_cues(srt_path):
    """(start, end, text-with-\\N) for every cue in an SRT file."""
    import re

    def t(h, m, sec, ms):
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0

    raw = open(srt_path, encoding='utf-8').read().replace('\r\n', '\n').strip()
    cues = []
    for blk in re.split(r'\n\s*\n', raw):
        lines = blk.split('\n')
        if len(lines) < 3:
            continue
        m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', lines[1].strip())
        if m:
            cues.append((t(*m.groups()[:4]), t(*m.groups()[4:]), "\\N".join(lines[2:])))
    return cues


def _srt_canvas_height(video_path):
    """Canvas height for SRT-style captions: libass's standard 384x288 for landscape; for
    portrait video, the video's own aspect (384 wide), else text scales off the frame."""
    W, H = _probe_dims(video_path) if video_path else (1920, 1080)
    return 288 if W >= H else int(round(384 * H / W))


def _srt_style_ass(srt_path, ass_filename, style, an, ml, mr, mv, pos=None, canvas_h=288):
    """SRT cues as ASS on a 384-wide canvas (libass's SRT canvas), with ``style`` as the Default style.

    ``pos`` = (x, y) pins every cue (Custom layout); otherwise alignment + margins place it,
    exactly as ffmpeg's subtitles filter does with force_style.
    """
    g = lambda k, d: getattr(style, k, d)
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 384\nPlayResY: %d\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,%s,%s,%s,%s,%s,%s,%d,%d,0,0,100,100,%g,0,%s,%s,%s,%d,%d,%d,%d,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (canvas_h, g('font_name', 'Arial'), g('font_size', 28), g('primary_colour', '&H00FFFFFF'), g('primary_colour', '&H00FFFFFF'),
         g('outline_colour', '&H00000000'), g('back_colour', '&H80000000'),
         -1 if g('bold', False) else 0, -1 if g('italic', False) else 0,
         float(g('letter_spacing', 0.0) or 0.0), g('border_style', 1), g('outline', 2), g('shadow', 1), an, ml, mr, mv)
    lead = "{\\an%d\\pos(%.1f,%.1f)}" % (an, pos[0], pos[1]) if pos else ""
    events = ["Dialogue: 0,%s,%s,Default,,0,0,0,,%s%s" % (_ass_time(a), _ass_time(b), lead, text)
              for a, b, text in _srt_cues(srt_path)]
    with open(ass_filename, "w", encoding="utf-8") as fh:
        fh.write(header)
        fh.write("\n".join(events) + "\n")
    return ass_filename


def _x_margins_384(style):
    """MarginL/MarginR that centre the caption at pos_x on the 384-wide canvas."""
    px = float(getattr(style, 'pos_x', 0.5) or 0.5)
    ml, mr = int(getattr(style, 'margin_l', 20)), int(getattr(style, 'margin_r', 20))
    if abs(px - 0.5) > 1e-6:
        cx0 = 384.0 * px
        hw = max(24.0, min(cx0, 384.0 - cx0) - ml)
        ml, mr = max(0, int(round(cx0 - hw))), max(0, int(round(384.0 - cx0 - hw)))
    return ml, mr


# SubtitleStyle.alignment uses legacy SSA numbering (as force_style does); ASS v4+ uses numpad.
_SSA_TO_NUMPAD = {1: 1, 2: 2, 3: 3, 5: 7, 6: 8, 7: 9, 9: 4, 10: 5, 11: 6}


def create_remapped_line_ass(srt_path, ass_filename, style, video_path=None):
    """A template (SRT + force_style) look as an ASS file, so preview and export share one file."""
    ml, mr = _x_margins_384(style)
    an = _SSA_TO_NUMPAD.get(int(getattr(style, 'alignment', 2)), 2)
    return _srt_style_ass(srt_path, ass_filename, style, an, ml, mr, int(getattr(style, 'margin_v', 30)),
                          canvas_h=_srt_canvas_height(video_path))


def create_remapped_custom_ass(srt_path, ass_filename, style, video_path=None):
    """Custom layout: every cue pinned at (pos_x, pos_y) with the chosen text alignment."""
    ml, mr = _x_margins_384(style)
    align = getattr(style, 'text_align', 'c')
    an, x = (4, ml) if align == 'l' else ((6, 384 - mr) if align == 'r' else (5, ml + (384 - ml - mr) / 2.0))
    canvas_h = _srt_canvas_height(video_path)
    y = canvas_h * float(getattr(style, 'pos_y', 0.82))
    return _srt_style_ass(srt_path, ass_filename, style, an, ml, mr, 0, pos=(x, y), canvas_h=canvas_h)
