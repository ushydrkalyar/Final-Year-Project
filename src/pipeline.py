"""The edit pipeline: every way a job's video gets made.

    run_edit        one clip: a new upload, or a finished clip changed. What didn't change is reused:
                    the transcript, the cut list, and every encoded piece with the same content
    run_multi_clip  several clips: make those that changed, then join them all

Both report through jobs.py and run on a worker thread. Captions are never burned here: the edit is
caption-free, Studio draws the captions live, and the export burns them.
"""
import dataclasses
import os
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

import pandas as pd

import burned_captions
import clip_combiner
import cut_list
import edit_render
import jobs
import media_probe
import workspace
from color_grader import compute_grading_params
from decision_engine import classify_pauses
from evaluator import aggregate, evaluate
from feature_extractor import detect_edge_silences, extract_pause_features
from filler_remover import detect_fillers
from frame_extractor import extract_frames
from reference_caption_style import extract_caption_style
from render_engine import get_keep_segments
from scene_detector import calculate_style_match, compare_styles, detect_scenes
from segment_render import Canvas
from style_extractor import calculate_style_profile, download_youtube_audio, download_youtube_video
from transcription_service import extract_audio, transcribe_audio

MAX_PARALLEL_CLIPS = 3

PACING_PRESETS = {
    "aggressive": {
        "max_duration": 0.5, "compress_duration": 0.3,
        "speaking_rate": 4.0, "boundary_keep_boost": 0.0, "cuts_per_minute": 20
    },
    "natural": {
        "max_duration": 2.5, "compress_duration": 1.2,
        "speaking_rate": 2.0, "boundary_keep_boost": 0.5, "cuts_per_minute": 4
    },
    "balanced": {
        "max_duration": 1.5, "compress_duration": 0.8,
        "speaking_rate": 2.5, "boundary_keep_boost": 0.0, "cuts_per_minute": 8
    },
}
NO_REFERENCE = "Could not download or analyse that reference link. Double-check the URL and try again."


@dataclass(frozen=True)
class EditOptions:
    """Everything that decides one clip's edit."""
    style_preset: str = "balanced"
    language: str = "auto"
    color_grade: str = "none"
    cv_analysis: bool = True
    transition_type: str = "auto"
    frame: dict | None = None
    canvas: Canvas | None = None
    max_pause: float | None = None          # pauses longer than this are cut
    compress_pause: float | None = None     # and longer than this, shortened
    reference_link: str = ""
    copy_caption_style: bool = False
    require_reference: bool = False         # matching an existing edit: failing beats falling back

    def same_decisions(self, other: "EditOptions") -> bool:
        """Would ``other`` keep and cut the same footage?"""
        def pacing(options):
            return options.style_preset, options.language, options.max_pause, options.compress_pause
        return not self.reference_link and pacing(self) == pacing(other)

    def same_picture(self, other: "EditOptions") -> bool:
        def picture(options):
            return options.color_grade, options.frame, options.canvas
        return picture(self) == picture(other)


@dataclass(frozen=True)
class Analysis:
    """What the upload's speech tells us, whatever pacing is applied to it."""
    transcript: dict
    language: str
    pause_features: pd.DataFrame
    leading_cut_end: float
    trailing_cut_start: float | None
    filler_cuts: list
    video_duration: float


@dataclass(frozen=True)
class Reference:
    """What a reference video asks for. Analysed once, however many clips follow it."""
    profile: dict
    scenes: dict | None = None
    caption_look: dict | None = None


def project_canvas(sources: list[tuple], current: Canvas | None = None) -> Canvas | None:
    """One size and rate for clips that will be joined, given [(upright upload, frame)].

    Stable when clips are reordered: the most common wins, then the one the project already has.
    """
    if len(sources) < 2:
        return None
    natural = [edit_render.natural_canvas(path, frame) for path, frame in sources]
    counts = Counter(natural)
    most = max(counts.values())
    if current and counts[current] == most:
        return current
    return max((c for c in natural if counts[c] == most), key=lambda c: c.width * c.height)


# ---------------------------------------------------------------------------
# Shared steps
# ---------------------------------------------------------------------------

def _no_progress(step, label, percent):
    pass


def _speech_end(transcript: dict) -> float:
    """The edit boundary when the container's length can't be read: just after the last word."""
    segments = transcript.get("segments", [])
    for segment in reversed(segments):
        if segment.get("words"):
            return segment["words"][-1]["end"] + 0.3
    return segments[-1]["end"] + 0.3 if segments else 0.0


def analyse(input_path: str, audio_path: str, language: str = "auto", report=_no_progress) -> Analysis:
    """Read the upload's speech."""
    report(2, "Transcribing speech", 15)
    transcript = transcribe_audio(audio_path, language=language)
    print(f"   ✅ {sum(len(s.get('words', [])) for s in transcript.get('segments', []))} words transcribed")
    report(4, "Analysing pauses", 35)
    pauses = extract_pause_features(transcript, audio_path)
    report(4, "Detecting edge silences", 38)
    leading, trailing = detect_edge_silences(transcript, audio_path)
    spoken = transcript.get("language", "en")
    report(4, "Detecting filler words", 40)
    fillers = detect_fillers(transcript, spoken)
    print(f"   ✅ {len(pauses)} pauses, {len(fillers)} filler cut(s), "
          f"speech from {leading:.2f}s to {trailing}")
    # Speech timestamps don't include end cards, music or trailing footage: the edit's
    # boundary is the real media length.
    duration = media_probe.duration(input_path, _speech_end(transcript))
    return Analysis(transcript, spoken, pauses, leading, trailing, fillers, duration)


def _caption_look(video: str) -> dict | None:
    """The caption look read off a reference's burned-in captions (OCR), as Studio's settings."""
    try:
        found = extract_caption_style(video)
    except Exception as exc:
        print(f"   ⚠️  Reference caption OCR failed: {exc}")
        return None
    return found["config"] if found and found.get("samples", 0) >= 3 else None


def read_reference(options: EditOptions, prefix: str, before_video=lambda: None) -> Reference | None:
    """None when the link can't be used."""
    audio_path, video_path = f"ref_audio_{prefix}.wav", f"ref_video_{prefix}.mp4"
    temporary = [audio_path, video_path]
    try:
        audio = download_youtube_audio(options.reference_link, audio_path)
        if not (audio and os.path.exists(audio)):
            return None
        temporary.append(audio)
        transcript = transcribe_audio(audio)
        profile = calculate_style_profile(extract_pause_features(transcript, audio), transcript)
        if not (options.cv_analysis or options.copy_caption_style):
            return Reference(profile)

        before_video()
        try:
            video = download_youtube_video(options.reference_link, video_path)
        except Exception as exc:
            print(f"   ⚠️  Could not download the reference video: {exc}")
            return Reference(profile)
        if not (video and os.path.exists(video)):
            return Reference(profile)
        temporary.append(video)

        look = _caption_look(video) if options.copy_caption_style else None
        scenes = None
        if options.cv_analysis:
            try:
                scenes = detect_scenes(video)
                profile["cv_cut_frequency"] = scenes.get("cut_frequency_per_min", 0)
                profile["cv_visual_pacing"] = scenes.get("visual_pacing", "unknown")
                profile["cv_avg_shot_duration"] = scenes.get("avg_shot_duration_s", 0)
                profile["cv_color_profile"] = scenes.get("color_profile", {})
                profile["cv_transition_type"] = (     # an explicit choice beats what the reference does
                    options.transition_type if options.transition_type != "auto"
                    else scenes.get("transition_types", {}).get("dominant", "hard_cut"))
            except Exception as exc:
                print(f"   ⚠️  Reference scene analysis skipped: {exc}")
        return Reference(profile, scenes, look)
    except jobs.JobCancelled:
        raise
    except Exception as exc:
        print(f"   ❌ Reference failed: {exc}\n{traceback.format_exc()}")
        return None
    finally:
        jobs.discard(temporary)


def _pacing(options: EditOptions, reference: Reference | None, own_scenes: dict) -> dict:
    if reference:
        profile = dict(reference.profile)   # copied: each clip adds its own match score, in parallel
        if reference.scenes and own_scenes:
            profile["cv_style_gap"] = compare_styles(reference.scenes, own_scenes)
            profile["cv_style_match_pct"] = calculate_style_match(profile["cv_style_gap"])
        return profile
    profile = dict(PACING_PRESETS.get(options.style_preset, PACING_PRESETS["balanced"]))
    if options.max_pause is not None:
        profile["max_duration"] = options.max_pause
    if options.compress_pause is not None:
        profile["compress_duration"] = options.compress_pause
    return profile


def _with_corrections(keep: list, corrections: list, duration: float) -> list:
    """The user's own cuts and restores outlast a new pacing."""
    for change in corrections:
        try:
            keep = cut_list.apply_correction(keep, change["start"], change["end"], change["correction"], duration)
        except ValueError:
            pass        # it doesn't apply to this cut any more
    return keep


def decide(analysis: Analysis, profile: dict, corrections: list) -> dict:
    """Pacing decisions and the cut list they give, as job fields."""
    decisions = classify_pauses(analysis.pause_features, profile, analysis.language)
    if not decisions.empty:
        counts = decisions["decision"].value_counts()
        print(f"   ✅ CUT:{counts.get('CUT', 0)}  COMPRESS:{counts.get('COMPRESS', 0)}  "
              f"KEEP:{counts.get('KEEP', 0)}  FILLER_CUTS:{len(analysis.filler_cuts)}")
    # Dead air after the last word is inside the media length, so it needs a cut of its own.
    edges = cut_list.edge_cuts(analysis.leading_cut_end, analysis.trailing_cut_start, analysis.video_duration)
    keep = get_keep_segments(decisions, analysis.video_duration, filler_cuts=analysis.filler_cuts,
                             silence_cuts=edges, style_profile=profile)
    columns = [c for c in ("start_time", "end_time", "duration", "decision", "reason") if c in decisions.columns]
    return {
        "keep_segments": _with_corrections(keep, corrections, analysis.video_duration),
        "edge_cuts": edges,
        "decisions": decisions[columns].to_dict("records") if not decisions.empty else [],
    }


def _evaluation(input_path, output, analysis: Analysis, decided: dict, profile: dict, elapsed: float) -> dict:
    try:
        return evaluate(
            input_video_path=input_path, output_video_path=output, transcript_data=analysis.transcript,
            decisions_df=pd.DataFrame(decided["decisions"]), filler_cuts=analysis.filler_cuts,
            keep_segments=decided["keep_segments"], style_profile=profile, processing_time_s=elapsed)
    except Exception as exc:
        print(f"   ⚠️  Evaluation failed (non-critical): {exc}")
        return {}


# ---------------------------------------------------------------------------
# One clip
# ---------------------------------------------------------------------------

def run_edit(job_id: str, options: EditOptions, reference: Reference | None = None,
             previous: jobs.Job | None = None) -> None:
    """Make a clip. ``previous`` is the finished clip it changes; a job registered with
    ``keep_segments`` (a timestamp correction, or a picture-only change) keeps that cut list."""
    started, prefix = time.time(), job_id[:8]
    audio = f"temp_audio_{prefix}.wav"

    def report(step, label, percent):
        jobs.progress(job_id, step, label, percent)

    try:
        job = jobs.store[job_id]
        input_path = job["input_video_path"]
        before = previous or {}
        same_speech = bool(previous) and before["options"].language == options.language
        same_footage = bool(previous) and before["input_video_path"] == input_path
        print(f"\n🚀 JOB {job_id} STARTED — {job['filename']}")

        report(1, "Reading the video", 5)
        if same_speech:
            analysis = before["analysis"]
        elif not extract_audio(input_path, audio):
            raise RuntimeError("Couldn't read the audio from this video.")

        scenes = before.get("raw_scene_analysis") or {}
        if options.cv_analysis and not scenes:
            report(1, "Analysing scenes (CV)", 7)
            try:
                scenes = detect_scenes(input_path)
            except Exception as exc:
                print(f"   ⚠️  Raw scene detection failed: {exc}")
                scenes = {}

        if same_footage:
            filmstrip = before["filmstrip"]
        else:
            report(1, "Extracting thumbnails", 9)
            filmstrip = extract_frames(video_path=input_path, req_id=prefix,
                                       video_duration=media_probe.duration(input_path, 60.0))

        if not same_speech:
            analysis = analyse(input_path, audio, options.language, report=report)

        if previous:
            captioned = before.get("burned_captions", False)
        else:
            report(4, "Checking for captions already in the picture", 44)
            # as uploaded: turned for the edit, the writing would be sideways
            captioned = burned_captions.detect(job.get("source_path") or input_path, analysis.transcript)

        if "keep_segments" in job:      # the cut list is already settled
            decided = {key: before[key] for key in ("edge_cuts", "decisions")}
            decided["keep_segments"] = job["keep_segments"]
            profile, used, look = before["style_profile"], before["reference_used"], None
        else:
            report(5, "Building style profile", 48)
            if options.reference_link and reference is None:
                reference = read_reference(options, prefix,
                                           lambda: report(5, "Analysing the reference video", 50))
                if reference is None and options.require_reference:
                    raise RuntimeError(NO_REFERENCE)
            profile, used = _pacing(options, reference, scenes), reference is not None
            look = reference.caption_look if reference else None
            report(6, "Making AI decisions", 58)
            decided = decide(analysis, profile, job.get("corrections", []))

        grade_params = None
        if options.color_grade == "match_reference":
            own, wanted = scenes.get("color_profile", {}), profile.get("cv_color_profile", {})
            if own and wanted:
                grade_params = compute_grading_params(wanted, own)
            else:
                print("   Reference colour profile unavailable — skipping colour match")

        report(7, "Rendering video", 65)
        keep = decided["keep_segments"]
        made = edit_render.cut(
            input_path, keep, prefix, options.frame, options.color_grade, grade_params,
            lambda label, percent: report(7, label, percent), before.get("cut"), options.canvas)
        report(8, "Evaluating quality", 88)
        elapsed = time.time() - started
        edited = sum(made.real_durations)
        jobs.finish(job_id, {
            **decided,
            "options": options,
            "analysis": analysis,
            "cut": made,
            "piece_paths": [piece.path for piece in made.pieces],
            "intermediate_path": made.path,
            "intermediate_duration": edited,
            "real_durations": made.real_durations,
            "clips_to_keep": keep,
            "transcript_data": analysis.transcript,
            "language": analysis.transcript.get("language", "unknown"),
            "filler_cuts": analysis.filler_cuts,
            "original_duration": analysis.video_duration,
            "edited_duration": edited,
            "style_profile": profile,
            "reference_used": used,
            "caption_look": look,
            "burned_captions": captioned,
            "raw_scene_analysis": scenes,
            "filmstrip": filmstrip,
            "evaluation": _evaluation(input_path, made.path, analysis, decided, profile, elapsed),
            "elapsed": elapsed,
        })
        print(f"✅ JOB {job_id} DONE — {elapsed:.1f}s\n")
    except Exception as exc:
        # The upload stays: the user may want to retry
        print(f"\n❌ JOB {job_id} FAILED: {exc}\n")
        jobs.fail(job_id, exc)
    finally:
        jobs.discard([audio])


# ---------------------------------------------------------------------------
# Several clips
# ---------------------------------------------------------------------------

def _run_clips(master_id: str, todo: dict, reference: Reference | None) -> str | None:
    """Make the clips in ``todo`` {id: (options, previous)}, a few at a time.

    Returns what went wrong, once every clip thread has ended.
    """
    if not todo:
        return None
    failure, total = None, len(todo)
    with ThreadPoolExecutor(max_workers=min(total, MAX_PARALLEL_CLIPS), thread_name_prefix="clip") as pool:
        futures = {pool.submit(run_edit, clip_id, options, reference, previous): clip_id
                   for clip_id, (options, previous) in todo.items()}
        while True:
            finished, running = wait(futures, timeout=0.5)      # the futures say when it is over
            for future in finished:
                clip = jobs.store.get(futures[future], {})
                if future.exception() or clip.get("status") != "done":
                    reason = future.exception() or clip.get("error") or "It stopped unexpectedly."
                    failure = f"Clip '{clip.get('filename', futures[future])}' failed: {reason}"
                    break
            master = jobs.store.get(master_id)
            if master is None or master.get("cancelled"):
                failure = failure or "This job was stopped."
            if failure:
                jobs.fail(master_id, failure, "A clip failed")
                for clip_id in todo:    # running clips stop at their next progress update
                    if clip_id in jobs.store:
                        jobs.store[clip_id]["cancelled"] = True
                pool.shutdown(wait=False, cancel_futures=True)
                break
            if not running:
                break
            done = sum(jobs.store.get(c, {}).get("progress", 0) for c in todo) / total
            label = (f"Editing {total} clips ({len(finished)} done)" if total > 1
                     else jobs.store.get(next(iter(todo)), {}).get("step_label", "Editing"))
            master.update(step_label=label, progress=int(5 + done * 0.75))      # clips are 5-80% of the master
    return failure


def _combined_transcript(clips: list[jobs.Job], starts: list[float]) -> dict:
    """One transcript on the joined timeline, with no two words on screen at once."""
    segments = []
    for clip, start in zip(clips, starts):
        for segment in edit_render.edited_transcript(clip["transcript_data"], clip["clips_to_keep"],
                                                     clip["real_durations"], offset=start)["segments"]:
            first = segment["start"]
            if segments and segments[-1]["end"] > first:    # under a transition the incoming clip takes over
                last = segments[-1]
                last["words"] = [{**w, "end": min(w["end"], first)} for w in last["words"] if w["start"] < first]
                if last["words"]:
                    last["end"] = first
                    last["text"] = " ".join(w["word"].strip() for w in last["words"])
                else:
                    segments.pop()
            segments.append(segment)
    return {"segments": segments, "language": clips[0].get("language")}


def _combined_summary(clips: list[jobs.Job], edited: float) -> dict:
    offset, fillers, decisions, keep = 0.0, [], [], []
    for clip in clips:
        keep += [(s + offset, e + offset) for s, e in clip["keep_segments"]]
        fillers += [(s + offset, e + offset) for s, e in clip["filler_cuts"]]
        decisions += [{**d, "start_time": d["start_time"] + offset, "end_time": d["end_time"] + offset}
                      for d in clip["decisions"]]
        offset += clip["original_duration"]
    return {
        "keep_segments": keep,
        "filler_cuts": fillers,
        "decisions": decisions,
        "original_duration": offset,
        "edited_duration": edited,
        "evaluation": aggregate([clip.get("evaluation", {}) for clip in clips], offset, edited),
    }


def combine_jobs(master_id: str, seconds: float, known=()) -> dict:
    """Join the master's clips, in its order, as one caption-free edit with one transcript."""
    master = jobs.store[master_id]
    clips = [jobs.store[clip_id] for clip_id in master["clip_jobs"]]
    for clip_id, clip in zip(master["clip_jobs"], clips):
        if not os.path.exists(clip.get("intermediate_path") or ""):
            raise RuntimeError(f"Output file missing for clip job {clip_id}")

    prefix = master_id[:8]
    detected = clip_combiner.resolve_transition_type(clips[0]["style_profile"])
    transitions = [detected if kind == "auto" else kind for kind in master["transitions"]]
    cuts = [clip["cut"] for clip in clips]
    joined = clip_combiner.join_clips(
        cuts, transitions, seconds, prefix, f"_intermediate_{prefix}_combined.mp4", known,
        lambda done, total: master.update(step_label=f"Rendering transitions ({done}/{total})"))
    total = cuts[0].canvas.seconds(joined.frames)
    return {
        **_combined_summary(clips, total),
        "joined": joined,
        "piece_paths": [piece.path for piece in joined.made],
        "clip_starts": joined.starts,
        "transitions_used": transitions,
        "intermediate_path": joined.path,
        "intermediate_duration": total,
        "transcript_data": _combined_transcript(clips, joined.starts),
        "clips_to_keep": [(0.0, total + 1.0)],
        "real_durations": None,
        "language": clips[0].get("language", "unknown"),
        "reference_used": clips[0].get("reference_used", False),
        "style_profile": clips[0].get("style_profile", {}),
        "caption_look": master.get("caption_look"),
    }


def run_multi_clip(master_id: str, todo: dict, options: EditOptions, seconds: float,
                   base: jobs.Job | None = None) -> None:
    """``todo`` holds the clips to make; the master's other clips are used as they are."""
    started = time.time()
    master = jobs.store[master_id]
    master.update(status="processing", step=1, progress=5, step_label="Editing clips")

    reference = None
    if options.reference_link:
        master.update(step_label="Fetching reference video", progress=3)
        reference = read_reference(options, master_id[:8])
        if reference:
            master["caption_look"] = reference.caption_look
        elif options.require_reference:
            for clip_id in todo:
                jobs.fail(clip_id, NO_REFERENCE)
            jobs.fail(master_id, NO_REFERENCE, "Reference failed")
            return
        else:   # the preset, not a download for every clip
            todo = {clip_id: (_without_reference(opts), previous) for clip_id, (opts, previous) in todo.items()}

    if _run_clips(master_id, todo, reference):
        for clip_id in todo:
            if jobs.store.get(clip_id, {}).get("status") != "error":
                jobs.fail(clip_id, "Stopped because another clip failed.")
            workspace.remove_job_files(clip_id[:8], keep=jobs.referenced_files({clip_id, master_id}))
        if master_id in jobs.store:
            jobs.store[master_id]["elapsed"] = time.time() - started
        return

    try:
        jobs.progress(master_id, 2, "Joining clips", 82)
        known = base["joined"].made if base and base.get("joined") else ()
        fields = combine_jobs(master_id, seconds, known)
    except Exception as exc:
        workspace.remove_job_files(master_id[:8], keep=jobs.referenced_files({master_id}))
        jobs.fail(master_id, exc, "Combine failed", elapsed=time.time() - started)
        return
    jobs.finish(master_id, {**fields, "elapsed": time.time() - started})
    print(f"\n✅ MULTI-CLIP MASTER {master_id} COMPLETE in {time.time() - started:.1f}s")


def _without_reference(options: EditOptions) -> EditOptions:
    return dataclasses.replace(options, reference_link="")
