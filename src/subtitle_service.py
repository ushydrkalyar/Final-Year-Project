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