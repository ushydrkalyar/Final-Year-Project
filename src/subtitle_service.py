def create_srt(transcription_data, srt_filename="subtitles.srt"):
    """Converts Whisper JSON output into a standard SRT subtitle file."""

    def format_time(seconds):
        hours   = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs    = int(seconds % 60)
        millis  = int((seconds - int(seconds)) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    segments = transcription_data.get('segments', [])

    if not segments:
        print("   ⚠️  No segments found — empty subtitle file will be created.")

    with open(srt_filename, "w", encoding="utf-8") as f:
        for index, segment in enumerate(segments):
            start_time = format_time(segment['start'])
            end_time   = format_time(segment['end'])
            text       = segment['text'].strip()

            f.write(f"{index + 1}\n")
            f.write(f"{start_time} --> {end_time}\n")
            f.write(f"{text}\n\n")

    print(f"   ✅ Subtitles saved: {srt_filename}")
    return srt_filename


def create_remapped_srt(transcription_data, keep_segments, srt_filename="subtitles_remapped.srt"):
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
                   to render_final_video.
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
        for (seg_start, seg_end) in keep_segs:
            seg_duration = seg_end - seg_start
            if seg_start <= orig_time <= seg_end:
                # orig_time is inside this kept segment
                return edited_offset + (orig_time - seg_start)
            if orig_time < seg_start:
                # orig_time is in a gap (cut region) — skip this subtitle
                return None
            # orig_time is past this segment — accumulate its duration
            edited_offset += seg_duration
        return None  # past the end of all kept segments

    whisper_segments = transcription_data.get('segments', [])

    remapped = []
    for seg in whisper_segments:
        new_start = original_to_edited(seg['start'], keep_segments)
        new_end   = original_to_edited(seg['end'],   keep_segments)

        # Skip segments that land in cut regions or map to None
        if new_start is None or new_end is None:
            continue

        # Ensure end is always after start (edge case: segment straddles a cut)
        if new_end <= new_start:
            new_end = new_start + 0.5

        remapped.append({
            'start': new_start,
            'end':   new_end,
            'text':  seg['text'].strip()
        })

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
Style: AnimSub,{font},60,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,{alignment},60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

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


def _build_ass_events(word_chunks: list) -> list:
    """
    For each chunk, emit one Dialogue line per word.
    During word W's slot:
      • words before W → white  &H00FFFFFF
      • word W itself  → yellow &H0000FFFF + bold  (active highlight)
      • words after W  → dim    &H80FFFFFF
    """
    events = []
    for chunk in word_chunks:
        if not chunk:
            continue
        for w_idx, (word, w_start, w_end) in enumerate(chunk):
            parts = []
            for i, (w, _, _) in enumerate(chunk):
                clean = w.strip().replace("{", "").replace("}", "")
                if i < w_idx:
                    parts.append(f"{{\\c&H00FFFFFF&}}{clean}")
                elif i == w_idx:
                    parts.append(f"{{\\c&H0000FFFF&}}{{\\b1}}{clean}{{\\b0}}")
                else:
                    parts.append(f"{{\\c&H80FFFFFF&}}{clean}")
            text = " ".join(parts) + "{\\c&H00FFFFFF&}"
            events.append(
                f"Dialogue: 0,{_ass_time(w_start)},{_ass_time(w_end)},"
                f"AnimSub,,0000,0000,0000,,{text}"
            )
    return events


def _words_to_chunks(all_words: list) -> list:
    """Split flat (word, start, end) list into fixed-size chunks."""
    return [all_words[i: i + _CHUNK_SIZE] for i in range(0, len(all_words), _CHUNK_SIZE)]


def create_animated_ass(transcription_data: dict,
                        ass_filename: str = "subtitles_animated.ass",
                        font: str = "Arial",
                        alignment: int = _ASS_ALIGNMENT) -> str:
    """
    Build a word-by-word animated ASS file from raw Whisper output.
    Each word is highlighted yellow exactly while being spoken.
    Returns the path to the written .ass file.
    """
    segments  = transcription_data.get("segments", [])
    all_words = []
    for seg in segments:
        for w in seg.get("words", []):
            word = w.get("word", "").strip()
            s    = float(w.get("start", seg["start"]))
            e    = float(w.get("end",   seg["end"]))
            if word:
                all_words.append((word, s, e))

    if not all_words:
        print("   ⚠️  No word-level timestamps — animated subtitles will be empty.")
        with open(ass_filename, "w", encoding="utf-8") as f:
            f.write(_ASS_HEADER.format(font=font, alignment=alignment))
        return ass_filename

    chunks = _words_to_chunks(all_words)
    events = _build_ass_events(chunks)

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(_ASS_HEADER.format(font=font, alignment=alignment))
        f.write("\n".join(events) + "\n")

    print(f"   ✅ Animated ASS subtitles: {ass_filename} "
          f"({len(all_words)} words, {len(chunks)} chunks)")
    return ass_filename


def create_remapped_ass(transcription_data: dict,
                        keep_segments: list,
                        ass_filename: str = "subtitles_animated_remapped.ass",
                        font: str = "Arial",
                        alignment: int = _ASS_ALIGNMENT) -> str:
    """
    Like create_animated_ass() but remaps timestamps to the edited timeline.
    Words inside cut regions are discarded.
    Returns the path to the written .ass file.
    """
    def original_to_edited(orig_time: float):
        edited_offset = 0.0
        for (seg_start, seg_end) in keep_segments:
            seg_dur = seg_end - seg_start
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
            f.write(_ASS_HEADER.format(font=font, alignment=alignment))
        return ass_filename

    chunks = _words_to_chunks(all_words)
    events = _build_ass_events(chunks)

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(_ASS_HEADER.format(font=font, alignment=alignment))
        f.write("\n".join(events) + "\n")

    print(f"   ✅ Remapped animated ASS: {ass_filename} "
          f"({len(all_words)} words, {len(chunks)} chunks)")
    return ass_filename