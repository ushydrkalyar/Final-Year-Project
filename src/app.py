from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # Load DATABASE_URL, JWT_SECRET etc. from project root .env

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, status, BackgroundTasks, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
import os
import uuid
import time
import threading
from typing import List

from transcription_service import extract_audio, transcribe_audio, get_filler_words
from feature_extractor      import extract_pause_features, detect_edge_silences
from filler_remover         import detect_fillers
from style_extractor        import download_youtube_audio, download_youtube_video, calculate_style_profile
from decision_engine        import classify_pauses
from render_engine          import get_keep_segments, render_final_video, SubtitleStyle, SUBTITLE_PRESETS
from subtitle_service       import create_srt, create_remapped_srt, create_remapped_ass
from feedback_service       import apply_feedback
from evaluator              import evaluate
from scene_detector         import detect_scenes, compare_styles, calculate_style_match
from color_grader           import compute_grading_params, apply_color_grade, apply_lut
from frame_extractor        import extract_frames
from clip_combiner          import combine_clips, resolve_transition_type
import auth

# ── yt-dlp availability check ───────────────────────────────────────────────
# Warn early if yt-dlp is missing or broken — prevents silent fallback to
# balanced preset when the user provides a YouTube reference URL.
import subprocess as _sp
try:
    _ytdlp_ver = _sp.check_output(['yt-dlp', '--version'], stderr=_sp.DEVNULL).decode().strip()
    print(f"✅ yt-dlp {_ytdlp_ver} found — reference video analysis available")
except Exception:
    print("⚠️  yt-dlp NOT found — reference YouTube URLs will be ignored.")
    print("   Install with: pip install yt-dlp")
# ────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="CutSmart", version="1.0")

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# NOTE: allow_origins=["*"] cannot be combined with allow_credentials=True —
# browsers reject that combination outright once real cookies are involved
# (which auth now uses for the session). Since the frontend is served by
# this same app via StaticFiles below, it's same-origin and doesn't need
# CORS at all in normal use — this explicit localhost list is just a safety
# net for local dev tools (e.g. a separate Live Server port).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5500",   # VS Code Live Server default
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth — user accounts, login/signup/profile (see auth.py)
# ---------------------------------------------------------------------------
# SessionMiddleware is required by authlib (Google OAuth) to store the
# OAuth state/nonce between the redirect and callback requests.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("JWT_SECRET", "dev-session-secret"),
)
auth.init_db()
app.include_router(auth.router)

# Max upload size: 500MB. Larger files are rejected before reading into memory.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

# ---------------------------------------------------------------------------
# Job store — keeps state for all active/completed jobs
# Stored in memory (resets on server restart, fine for FYP demo)
# ---------------------------------------------------------------------------
# Each entry: {
#   status:           "queued" | "processing" | "done" | "error"
#   step:             int (1-8)
#   step_label:       str
#   progress:         int (0-100)
#   output_path:      str | None
#   input_video_path: str | None   (kept for feedback splice-in)
#   error:            str | None
#   elapsed:          float | None
# }
job_store: dict = {}

# Thread pool for background processing
_executor = None

def _get_executor():
    global _executor
    if _executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _executor = ThreadPoolExecutor(max_workers=2)
    return _executor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_video_duration(transcript_data: dict, fallback: float = 0.0) -> float:
    segments = transcript_data.get('segments', [])
    if not segments:
        return fallback
    last_word_end = None
    for seg in reversed(segments):
        words = seg.get('words', [])
        if words:
            last_word_end = words[-1]['end']
            break
    if last_word_end is not None:
        return last_word_end + 0.3
    return segments[-1]['end'] + 0.3


def _get_media_duration(video_path: str, fallback: float = 0.0) -> float:
    """Read the real container duration without relying on speech timestamps."""
    try:
        import json
        result = _sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", video_path],
            check=True, capture_output=True, text=True,
        )
        duration = float(json.loads(result.stdout)['format']['duration'])
        return duration if duration > 0 else fallback
    except Exception as exc:
        print(f"   Warning: could not probe video duration ({exc}); using transcript duration.")
        return fallback


def _cleanup(paths: list):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
                print(f"   🗑️  Deleted: {p}")
        except OSError as e:
            print(f"   ⚠️  Could not delete {p}: {e}")


def _require_owner(job_id: str, job: dict, user: dict):
    """
    Raises 403 if `user` doesn't own `job`. Jobs created before auth was
    added (or anything with a missing owner_id) have no owner recorded —
    those are treated as inaccessible rather than silently public.
    Call this in every endpoint that reads or mutates a specific job_id.
    """
    if job.get("owner_id") != user["id"]:
        raise HTTPException(status_code=403, detail="You don't have access to this job")


def _cleanup_pattern(pattern: str):
    """Delete all files matching a glob pattern — used for temp file sweeps."""
    import glob
    for p in glob.glob(pattern):
        try:
            os.remove(p)
            print(f"   🗑️  Deleted (pattern): {p}")
        except OSError as e:
            print(f"   ⚠️  Could not delete {p}: {e}")


def _update_job(job_id: str, step: int, label: str, progress: int):
    """Update job progress — called from the background pipeline thread."""
    if job_id in job_store:
        job_store[job_id].update({
            'step':        step,
            'step_label':  label,
            'progress':    progress,
            'status':      'processing'
        })
        print(f"   [Job {job_id}] Step {step}/8 — {label} ({progress}%)")


# ---------------------------------------------------------------------------
# Main pipeline (runs in background thread)
# ---------------------------------------------------------------------------

def _run_pipeline(job_id: str, input_video_path: str, safe_name: str,
                  reference_link: str, style_preset: str, subtitle_preset: str,
                  subtitle_style: SubtitleStyle = None,
                  language: str = 'auto',
                  color_grade: str = 'none',
                  cv_analysis: bool = True,
                  transition_type: str = 'auto',
                  transition_duration: float = 0.5,
                  prefetched_style_profile: dict = None):
    """
    Full editing pipeline. Runs in a background thread so POST /edit-video/
    can return a job_id immediately without waiting 2-10 minutes.
    """
    start_time = time.time()
    req_id     = job_id[:8]

    audio_path        = f"temp_audio_{req_id}.wav"
    srt_path          = f"subtitles_{req_id}.srt"
    remapped_srt_path = f"subtitles_remapped_{req_id}.srt"
    remapped_ass_path = f"subtitles_remapped_{req_id}.ass"
    ref_audio_path    = f"ref_audio_{req_id}.wav"
    ref_video_path    = f"ref_video_{req_id}.mp4"
    output_filename   = f"edited_{req_id}_{safe_name}"
    if not output_filename.lower().endswith('.mp4'):
        output_filename = os.path.splitext(output_filename)[0] + '.mp4'

    # The remapped SRT is needed by /srt and /restyle until the job is
    # downloaded, so it must not be treated as a short-lived temporary file.
    temp_files = [audio_path, srt_path, ref_audio_path, ref_video_path]

    print(f"\n{'='*70}")
    print(f"🚀 JOB {job_id} STARTED — {safe_name}")
    print(f"{'='*70}")

    try:
        # ── Step 1: Audio extraction ──────────────────────────────────────
        _update_job(job_id, 1, "Extracting audio", 5)
        success = extract_audio(input_video_path, audio_path)
        if not success:
            raise RuntimeError("Audio extraction failed. Check FFmpeg is installed.")

                # ── Step 1.4: CV Scene detection on raw footage ─────────────────────
        _update_job(job_id, 1, "Analysing scenes (CV)", 7)
        if cv_analysis:
            try:
                raw_scene_analysis = detect_scenes(input_video_path)
                print(f"   ✅ Raw: {raw_scene_analysis['scene_count']} scenes, "
                      f"pacing={raw_scene_analysis['visual_pacing']}")
            except Exception as e:
                print(f"   ⚠️  Raw scene detection failed: {e}")
                raw_scene_analysis = {}
        else:
            print("   ⏭️  CV scene analysis skipped (disabled by user)")
            raw_scene_analysis = {}

        # ── Step 1.5: Frame extraction (filmstrip thumbnails) ─────────────
        # Run immediately after audio extraction while the input video is
        # guaranteed to be on disk. Uses a rough duration estimate here;
        # refined duration from Whisper is used later for /timeline/ data.
        _update_job(job_id, 1, "Extracting thumbnails", 9)
        # Rough duration from ffprobe — available before Whisper runs
        try:
            import subprocess as _sp, json as _json
            _probe = _sp.run(
                ["ffprobe","-v","quiet","-print_format","json",
                 "-show_format", input_video_path],
                check=True, capture_output=True, text=True
            )
            _rough_dur = float(_json.loads(_probe.stdout)
                               .get('format',{}).get('duration', 60))
        except Exception:
            _rough_dur = 60.0   # safe fallback

        filmstrip_frames = extract_frames(
            video_path      = input_video_path,
            req_id          = req_id,
            video_duration  = _rough_dur,
        )
        print(f"   ✅ {len(filmstrip_frames)} filmstrip frames ready")

        # ── Step 2: Transcription ─────────────────────────────────────────
        _update_job(job_id, 2, "Transcribing speech", 15)
        transcript_data = transcribe_audio(audio_path, language=language)
        num_words = sum(len(s.get('words', [])) for s in transcript_data.get('segments', []))
        print(f"   ✅ {num_words} words transcribed")

        # ── Step 3: Subtitle generation ───────────────────────────────────
        # NOTE: the animated ASS file is intentionally NOT built here.
        # It needs to be timed to the EDITED timeline, which isn't known
        # until Step 7 (after cuts are decided) — see create_remapped_ass()
        # below. Building an "original timeline" animated ASS at this point
        # would be thrown away unused, so we skip straight to the SRT.
        _update_job(job_id, 3, "Generating subtitles", 25)
        # Always create SRT as a fallback/UI reference
        create_srt(transcript_data, srt_path)

        # ── Step 4: Pause analysis ────────────────────────────────────────
        _update_job(job_id, 4, "Analysing pauses", 35)
        pause_features = extract_pause_features(transcript_data, audio_path)
        print(f"   ✅ {len(pause_features)} pauses detected")

        # ── Step 4.4: Edge silence detection ─────────────────────────────
        _update_job(job_id, 4, "Detecting edge silences", 38)
        leading_cut_end, trailing_cut_start = detect_edge_silences(transcript_data, audio_path)
        print(f"   ✅ Leading silence cut end: {leading_cut_end:.2f}s")
        print(f"   ✅ Trailing silence cut start: {trailing_cut_start}")

        # ── Step 4.5: Filler word detection ───────────────────────────────
        _update_job(job_id, 4, "Detecting filler words", 40)
        # Use language-specific filler words based on detected/specified language
        detected_lang = transcript_data.get('language', 'en')
        lang_fillers  = get_filler_words(detected_lang)
        filler_cuts   = detect_fillers(transcript_data, filler_words=lang_fillers)
        print(f"   ✅ {len(filler_cuts)} filler cut region(s) detected")

        # ── Step 5: Style profile ─────────────────────────────────────────
        _update_job(job_id, 5, "Building style profile", 48)

        reference_success = False   # True only when yt-dlp download + analysis succeeds
        if prefetched_style_profile:
            # Master pipeline already downloaded and analysed the reference once —
            # reuse the result directly so we don't hammer YouTube N times in parallel.
            #
            # IMPORTANT: copy the dict rather than reusing the same object.
            # Multiple clip pipelines run in parallel threads and each needs
            # to attach ITS OWN cv_style_gap/cv_style_match_pct below (every
            # clip's raw footage differs) — mutating the single shared dict
            # would let clips race and overwrite each other's match score.
            style_profile     = dict(prefetched_style_profile)
            reference_success = True
            print(f"   📺 Using pre-fetched reference style profile (skipping download)")

            # Compute this clip's own style-match against the reference,
            # using the reference's CV metrics gathered once at master level
            # plus THIS clip's own raw_scene_analysis (computed per-clip above).
            if raw_scene_analysis:
                ref_cv_like = {
                    'cut_frequency_per_min': style_profile.get('cv_cut_frequency', 0),
                    'avg_shot_duration_s':   style_profile.get('cv_avg_shot_duration', 0),
                    'color_profile':         style_profile.get('cv_color_profile', {}),
                    'visual_pacing':         style_profile.get('cv_visual_pacing', 'unknown'),
                    'transition_types':      {'dominant': style_profile.get('cv_transition_type', 'hard_cut')},
                }
                style_profile['cv_style_gap']       = compare_styles(ref_cv_like, raw_scene_analysis)
                style_profile['cv_style_match_pct'] = calculate_style_match(style_profile['cv_style_gap'])
                print(f"   ✅ Style match vs reference: {style_profile['cv_style_match_pct']}%")
        elif reference_link:
            print(f"   📺 Analysing reference video...")
            try:
                ref_audio = download_youtube_audio(reference_link, ref_audio_path)
                if ref_audio and os.path.exists(ref_audio):
                    ref_transcript   = transcribe_audio(ref_audio)
                    ref_pauses       = extract_pause_features(ref_transcript, ref_audio)
                    style_profile    = calculate_style_profile(ref_pauses, ref_transcript)
                    reference_success = True
                    try:
                        _update_job(job_id, 5, "CV scene analysis", 50)
                        # NOTE: yt-dlp downloads audio only, so we cannot run
                        # detect_scenes() on the reference video directly.
                        # Instead we use the raw footage CV analysis as a proxy
                        # for transition type, and copy the raw scene metrics
                        # into the style profile so compare_styles() still works.
                        # When a full reference video download is implemented,
                        # replace `input_video_path` below with the ref video path.
                        ref_video = download_youtube_video(reference_link, ref_video_path)
                        ref_cv = detect_scenes(ref_video) if ref_video else {}
                        style_profile['cv_cut_frequency']   = ref_cv.get('cut_frequency_per_min', 0)
                        style_profile['cv_visual_pacing']   = ref_cv.get('visual_pacing', 'unknown')
                        # If user explicitly picked a transition, use it; otherwise derive from CV
                        if transition_type != 'auto':
                            style_profile['cv_transition_type'] = transition_type
                        else:
                            style_profile['cv_transition_type'] = ref_cv.get('transition_types', {}).get('dominant', 'hard_cut')
                        style_profile['cv_color_profile']   = ref_cv.get('color_profile', {})
                        style_profile['cv_style_gap']       = compare_styles(ref_cv, raw_scene_analysis)
                        style_profile['cv_style_match_pct'] = calculate_style_match(style_profile['cv_style_gap'])
                        print(f"   ✅ CV metrics added to style profile (transition={style_profile['cv_transition_type']}, "
                              f"match={style_profile['cv_style_match_pct']}%)")
                    except Exception as cv_e:
                        print(f'   ⚠️  CV skipped: {cv_e}')
                else:
                    print("   ⚠️  Download failed — using balanced defaults")
                    style_profile  = _preset_profile("balanced")
            except Exception as e:
                print(f"   ⚠️  Reference error: {e}")
                style_profile = _preset_profile("balanced")
        else:
            style_profile = _preset_profile(style_preset)
            print(f"   ✅ Preset: {style_preset} — "
                  f"cut>{style_profile['max_duration']}s  "
                  f"compress>{style_profile['compress_duration']}s")

        # ── Step 6: AI decisions ──────────────────────────────────────────
        _update_job(job_id, 6, "Making AI decisions", 58)
        decisions_df = classify_pauses(pause_features, style_profile)

        if not decisions_df.empty:
            cut_c  = len(decisions_df[decisions_df['decision'] == 'CUT'])
            comp_c = len(decisions_df[decisions_df['decision'] == 'COMPRESS'])
            keep_c = len(decisions_df[decisions_df['decision'] == 'KEEP'])
            print(f"   ✅ CUT:{cut_c}  COMPRESS:{comp_c}  KEEP:{keep_c}  "
                  f"FILLER_CUTS:{len(filler_cuts)}")
            print(f"\n   📊 Decision table:")
            print(f"   {decisions_df[['start_time','duration','previous_word','is_sentence_boundary','decision','reason']].to_string(index=False)}")

        # ── Step 7: Render ────────────────────────────────────────────────
        _update_job(job_id, 7, "Rendering video", 65)

        # Speech timestamps do not include end cards, music, or intentional
        # trailing footage. Use the media duration as the editing boundary.
        video_duration = _get_media_duration(
            input_video_path,
            _get_video_duration(transcript_data),
        )

        # Pass filler_cuts into get_keep_segments so they're merged with
        # pause decisions into one unified cut list.
        # Also pass leading_cut_end / trailing_cut_start to trim silence at
        # the start and end of the video — video_duration above is the REAL
        # container duration (via ffprobe), which includes any dead air
        # after the last spoken word, so that trailing silence has to be
        # explicitly added as a cut here or it survives untouched in the
        # final render.
        all_filler_cuts = list(filler_cuts)
        if leading_cut_end > 0.05:
            # Add leading silence as a filler cut at the very start
            all_filler_cuts = [(0.0, leading_cut_end)] + all_filler_cuts
            print(f"   ✂️  Leading silence added to cut list: 0.0s–{leading_cut_end:.2f}s")
        if trailing_cut_start is not None and (video_duration - trailing_cut_start) > 0.05:
            # Add trailing silence as a cut from the last word to the true end
            all_filler_cuts = all_filler_cuts + [(trailing_cut_start, video_duration)]
            print(f"   ✂️  Trailing silence added to cut list: {trailing_cut_start:.2f}s–{video_duration:.2f}s")

        clips_to_keep = get_keep_segments(decisions_df, video_duration,
                                          filler_cuts=all_filler_cuts,
                                          style_profile=style_profile)

        create_remapped_srt(transcript_data, clips_to_keep, remapped_srt_path)
        if subtitle_preset == "animated":
            create_remapped_ass(transcript_data, clips_to_keep, remapped_ass_path)
            pass_ass = remapped_ass_path
        else:
            pass_ass = None

        _update_job(job_id, 7, "Encoding clips", 75)
        intermediate_path = render_final_video(
            input_video_path, clips_to_keep,
            remapped_srt_path, output_filename,
            subtitle_style=subtitle_style,
            job_prefix=req_id,
            ass_path=pass_ass
        )

        # ── Step 7.5: Color grading (Pass 3) ───────────────────────────────
        if color_grade != 'none':
            _update_job(job_id, 7, "Applying color grade", 82)
            graded_filename = f"graded_{req_id}_{safe_name}"
            if not graded_filename.lower().endswith('.mp4'):
                graded_filename = os.path.splitext(graded_filename)[0] + '.mp4'
            try:
                if color_grade == 'match_reference':
                    raw_cp = raw_scene_analysis.get('color_profile', {})
                    ref_cp = style_profile.get('cv_color_profile', {})
                    if not raw_cp or not ref_cp:
                        print('   Reference colour profile unavailable — skipping colour match')
                        success = False
                    else:
                        grade_params = compute_grading_params(ref_cp, raw_cp)
                        success = apply_color_grade(output_filename, graded_filename, grade_params)
                else:
                    success = apply_lut(output_filename, graded_filename, color_grade)
                if success and os.path.exists(graded_filename):
                    os.remove(output_filename)
                    output_filename = graded_filename
                    print(f'   ✅ Color grade applied: {color_grade}')
                else:
                    print('   ⚠️  Color grading failed — using ungraded video')
                    if os.path.exists(graded_filename):
                        os.remove(graded_filename)
            except Exception as cg_e:
                print(f'   ⚠️  Color grading error (non-critical): {cg_e}')

        # ── Step 8: Evaluation ────────────────────────────────────────────
        _update_job(job_id, 8, "Evaluating quality", 88)
        elapsed = time.time() - start_time
        try:
            evaluation = evaluate(
                input_video_path     = input_video_path,
                output_video_path    = output_filename,
                transcript_data      = transcript_data,
                decisions_df         = decisions_df,
                filler_cuts          = filler_cuts,
                keep_segments        = clips_to_keep,
                style_profile        = style_profile,
                processing_time_s    = elapsed,
                active_filler_words  = lang_fillers,   # Bug 5 fix
            )
        except Exception as e:
            print(f"   ⚠️  Evaluation failed (non-critical): {e}")
            evaluation = {}

        # ── Step 9: Cleanup ───────────────────────────────────────────────
        _update_job(job_id, 8, "Cleaning up", 95)
        _cleanup(temp_files)
        # raw upload is kept until download (needed for /restyle/ and /feedback/)
        # intermediate is kept until download (needed for /restyle/)

        print(f"\n{'='*70}")
        print(f"✅ JOB {job_id} DONE — {elapsed:.1f}s ({elapsed/60:.1f}m)")
        print(f"{'='*70}\n")

        # Mark job as done — keep input_video_path for feedback splicing
        # Also store timeline data for the dual-timeline UI
        decisions_records = []
        if not decisions_df.empty:
            cols = ['start_time', 'end_time', 'duration', 'decision', 'reason']
            existing = [c for c in cols if c in decisions_df.columns]
            decisions_records = decisions_df[existing].to_dict('records')

        job_store[job_id].update({
            'status':              'done',
            'step':                8,
            'step_label':          'Complete',
            'progress':            100,
            'output_path':         output_filename,
            'input_video_path':    input_video_path,
            'elapsed':             elapsed,
            'evaluation':          evaluation,
            'style_profile':       style_profile,
            'reference_used':      reference_success,
            'raw_scene_analysis':  raw_scene_analysis,
            # Timeline data — used by GET /timeline/{job_id}
            'language':            transcript_data.get('language', 'unknown'),
            'keep_segments':       clips_to_keep,
            'filler_cuts':         filler_cuts,
            'decisions':           decisions_records,
            'original_duration':   video_duration,
            'edited_duration':     evaluation.get('time_savings', {}).get('edited_duration_s', 0),
            'remapped_srt_path':   remapped_srt_path,   # kept for /restyle/ and /srt/
            'remapped_ass_path':   pass_ass,             # kept for /restyle/ (animated jobs only, else None)
            'intermediate_path':   intermediate_path,   # kept for /restyle/
            'subtitle_style':      subtitle_style,       # kept for /restyle/
            'subtitle_preset':     subtitle_preset,      # kept for /restyle/ to know if animated
            'filmstrip':           filmstrip_frames,     # thumbnail frames for timeline UI
        })

    except Exception as e:
        # Keep the SRT only for successful jobs.
        _cleanup(temp_files + [remapped_srt_path, remapped_ass_path])
        # Don't delete the input video on error — user may want to retry
        error_msg = str(e)
        print(f"\n❌ JOB {job_id} FAILED: {error_msg}\n")
        job_store[job_id].update({
            'status':    'error',
            'step_label': 'Failed',
            'error':      error_msg,
        })


def _preset_profile(preset: str) -> dict:
    presets = {
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
    return presets.get(preset, presets["balanced"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _run_multi_clip_pipeline(
    master_id:           str,
    clip_job_ids:        list,
    reference_link:      str,
    style_preset:        str,
    subtitle_preset:     str,
    sub_style,
    language:            str,
    color_grade:         str,
    cv_analysis:         bool,
    transition_type:     str,
    transition_duration: float = 0.5,
):
    """
    Parallel multi-clip orchestrator (Step 3 & 4):

    1. Submits every clip pipeline to the ThreadPoolExecutor simultaneously —
       clips run in parallel. All FFmpeg, feature extraction, decision engine
       and render steps overlap across clips.

    2. Whisper transcription is internally serialised via _WHISPER_LOCK in
       transcription_service.py — only one clip transcribes at a time, but
       all other steps proceed fully in parallel.

    3. Master job progress = average progress across all running clip jobs.

    4. Once ALL clips reach status='done', combine_clips() merges them in
       the original upload order.
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = _time.time()
    n  = len(clip_job_ids)

    print(f"\n{'='*70}")
    print(f"🎬 MULTI-CLIP MASTER {master_id} — {n} clips IN PARALLEL")
    print(f"{'='*70}")

    job_store[master_id].update({
        "status":     "processing",
        "step":       1,
        "step_label": f"Editing {n} clips in parallel",
        "progress":   5,
    })

    # ── Step 0: Pre-fetch reference style profile once (if URL provided) ────
    # Each sub-clip pipeline would otherwise download the same YouTube audio
    # N times in parallel, which causes rate-limiting and silent failures.
    # We download once here in the master thread and pass the result through.
    prefetched_style_profile = None
    if reference_link:
        job_store[master_id].update({"step_label": "Fetching reference video", "progress": 3})
        print(f"\n{'='*60}")
        print(f"📺 PRE-FETCHING REFERENCE VIDEO (once for {n} clips)")
        print(f"   URL: {reference_link}")
        print(f"{'='*60}")
        try:
            _ref_audio_path = f"ref_audio_{master_id[:8]}_master.wav"
            _ref_audio = download_youtube_audio(reference_link, _ref_audio_path)
            print(f"   download_youtube_audio returned: {_ref_audio!r}")
            if _ref_audio and os.path.exists(_ref_audio):
                print(f"   ✅ Audio file confirmed on disk — transcribing...")
                _ref_transcript          = transcribe_audio(_ref_audio)
                _ref_pauses              = extract_pause_features(_ref_transcript, _ref_audio)
                prefetched_style_profile = calculate_style_profile(_ref_pauses, _ref_transcript)
                print(f"   ✅ Reference style profile ready — sharing across all {n} clips")
                if cv_analysis:
                    _ref_video_path = f"ref_video_{master_id[:8]}_master.mp4"
                    _ref_video = download_youtube_video(reference_link, _ref_video_path)
                    if _ref_video:
                        _ref_cv = detect_scenes(_ref_video)
                        prefetched_style_profile['cv_cut_frequency'] = _ref_cv.get('cut_frequency_per_min', 0)
                        prefetched_style_profile['cv_visual_pacing'] = _ref_cv.get('visual_pacing', 'unknown')
                        prefetched_style_profile['cv_transition_type'] = (
                            transition_type if transition_type != 'auto'
                            else _ref_cv.get('transition_types', {}).get('dominant', 'hard_cut')
                        )
                        prefetched_style_profile['cv_color_profile']    = _ref_cv.get('color_profile', {})
                        prefetched_style_profile['cv_avg_shot_duration'] = _ref_cv.get('avg_shot_duration_s', 0)
                        _cleanup([_ref_video])
                print(f"   Profile: {prefetched_style_profile.get('_summary', 'N/A')}")
                _cleanup([_ref_audio])
            else:
                print(f"   ❌ Reference download returned None/empty path")
                print(f"      Clips will use style preset '{style_preset}' instead")
        except Exception as _ref_err:
            import traceback
            print(f"   ❌ Reference pre-fetch EXCEPTION: {_ref_err}")
            print(traceback.format_exc())
            print(f"      Clips will use style preset '{style_preset}' instead")

    # ── Step 1: Launch all clip pipelines in parallel ─────────────────────
    # Use a fresh executor sized to the number of clips so they all run
    # simultaneously (the main executor has max_workers=2 which would queue).
    clip_futures = {}
    parallel_executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="clip")

    for clip_id in clip_job_ids:
        clip_job   = job_store[clip_id]
        input_path = clip_job["input_video_path"]
        safe_name  = clip_job["filename"]

        future = parallel_executor.submit(
            _run_pipeline,
            clip_id, input_path, safe_name,
            reference_link, style_preset, subtitle_preset, sub_style,
            language, color_grade, cv_analysis, transition_type, transition_duration,
            prefetched_style_profile   # ← pre-fetched once; skips per-clip download
        )
        clip_futures[future] = clip_id

    # ── Step 2: Monitor progress while clips run ──────────────────────────
    all_done = False
    while not all_done:
        _time.sleep(1.5)

        progresses   = []
        failed_clips = []

        for clip_id in clip_job_ids:
            cj = job_store[clip_id]
            progresses.append(cj.get("progress", 0))
            if cj["status"] == "error":
                failed_clips.append((clip_id, cj.get("error", "Unknown")))

        if failed_clips:
            parallel_executor.shutdown(wait=False, cancel_futures=True)
            clip_id, err = failed_clips[0]
            safe_name = job_store[clip_id]["filename"]
            # Clean up all sub-clip uploads so they don't linger
            for cid in clip_job_ids:
                cj = job_store.get(cid, {})
                _cleanup([cj.get('input_video_path')])
                _cleanup([cj.get('output_path')])
                _cleanup([cj.get('intermediate_path')])
                sub_req = cid[:8]
                _cleanup_pattern(f"temp_audio_{sub_req}*.wav")
                _cleanup_pattern(f"temp_clip_{sub_req}_*.mp4")
                _cleanup_pattern(f"_intermediate_{sub_req}_*")
            job_store[master_id].update({
                "status":     "error",
                "step_label": "A clip failed",
                "error":      f"Clip '{safe_name}' failed: {err}",
                "elapsed":    _time.time() - t0,
            })
            return

        # Average progress across clips, scaled to 5–80% of master progress
        avg = sum(progresses) / len(progresses)
        master_pct = int(5 + avg * 0.75)

        # Count how many clips are still running
        done_count = sum(1 for cj_id in clip_job_ids
                         if job_store[cj_id]["status"] == "done")

        job_store[master_id].update({
            "step_label": f"Editing clips in parallel ({done_count}/{n} done)",
            "progress":   master_pct,
        })

        all_done = done_count == n

    parallel_executor.shutdown(wait=False)
    print(f"\n   ✅ All {n} clips done — starting combine")

    # ── Step 3: Collect outputs in original upload order ──────────────────
    clip_paths = []
    for clip_id in clip_job_ids:
        output = job_store[clip_id].get("output_path", "")
        if not output or not os.path.exists(output):
            job_store[master_id].update({
                "status":     "error",
                "step_label": "Missing clip output",
                "error":      f"Output file missing for clip job {clip_id}",
                "elapsed":    _time.time() - t0,
            })
            return
        clip_paths.append(output)

    # ── Step 4: Combine all clips into final video ────────────────────────
    job_store[master_id].update({
        "step":       2,
        "step_label": f"Combining {n} clips",
        "progress":   82,
    })

    style_profile_ref    = job_store[clip_job_ids[0]].get("style_profile", {})
    resolved_transition  = resolve_transition_type(style_profile_ref) \
                           if transition_type == "auto" else transition_type

    final_output = f"combined_{master_id[:8]}_output.mp4"

    try:
        combine_clips(
            clip_paths          = clip_paths,
            transition_type     = resolved_transition,
            transition_duration = transition_duration,
            output_filename     = final_output,
            job_prefix          = master_id[:8],
        )
    except Exception as e:
        _cleanup_pattern(f"_norm_{master_id[:8]}_*.mp4")
        _cleanup_pattern(f"_faded_{master_id[:8]}_*.mp4")
        _cleanup_pattern(f"_concat_list_{master_id[:8]}.txt")
        job_store[master_id].update({
            "status":     "error",
            "step_label": "Combine failed",
            "error":      str(e),
            "elapsed":    _time.time() - t0,
        })
        return

    elapsed = _time.time() - t0

    # ── Aggregate sub-clip timeline data into master job ──────────────────
    # Each sub-clip pipeline stores keep_segments, filler_cuts, decisions,
    # filmstrip etc. in its own job_store entry.  The master job has none of
    # this because _run_multi_clip_pipeline only does the combine step.
    # We stitch all clip timelines together here — with time offsets — so
    # GET /timeline/{master_id} returns a properly populated combined timeline.
    combined_keep_segments  = []
    combined_filler_cuts    = []
    combined_decisions      = []
    combined_filmstrip      = []
    combined_orig_duration  = 0.0
    combined_edit_duration  = 0.0
    time_offset             = 0.0   # running offset in original-video time

    for clip_id in clip_job_ids:
        cj  = job_store.get(clip_id, {})
        dur = cj.get('original_duration', 0.0)

        for s, e in cj.get('keep_segments', []):
            combined_keep_segments.append((s + time_offset, e + time_offset))

        for s, e in cj.get('filler_cuts', []):
            combined_filler_cuts.append((s + time_offset, e + time_offset))

        for d in cj.get('decisions', []):
            shifted = dict(d)
            shifted['start_time'] = d.get('start_time', 0) + time_offset
            shifted['end_time']   = d.get('end_time',   0) + time_offset
            combined_decisions.append(shifted)

        # Filmstrip frames: convert local position_pct → absolute timestamp.
        # We store _abs_ts now and recompute position_pct once we know
        # the total combined duration (after the loop finishes).
        clip_filmstrip = cj.get('filmstrip', [])
        for frame in clip_filmstrip:
            local_pos = frame.get('position_pct', 0) / 100.0 * dur
            abs_ts    = combined_orig_duration + local_pos
            combined_filmstrip.append({**frame, '_abs_ts': abs_ts})

        combined_orig_duration += dur
        combined_edit_duration += cj.get('edited_duration', 0.0)
        time_offset            += dur

    # Finalise filmstrip position_pct now that combined_orig_duration is known
    if combined_orig_duration > 0:
        combined_filmstrip.sort(key=lambda f: f['_abs_ts'])
        for frame in combined_filmstrip:
            frame['position_pct'] = round(frame['_abs_ts'] / combined_orig_duration * 100, 2)
            frame['timestamp']    = round(frame['_abs_ts'], 2)
            del frame['_abs_ts']

    # ── Aggregate evaluation from sub-clips ─────────────────────────────
    agg_time_saved_s      = 0.0
    agg_fillers_removed   = 0
    agg_pauses_cut        = 0
    agg_pauses_compressed = 0
    agg_scores            = []
    agg_wpm_original      = []
    agg_wpm_edited        = []
    agg_silence_orig      = []
    agg_silence_edit      = []

    for clip_id in clip_job_ids:
        cj = job_store.get(clip_id, {})
        ev = cj.get('evaluation', {})
        if not ev:
            continue
        ts = ev.get('time_savings',  {})
        qs = ev.get('quality_score', {})
        sp = ev.get('speech',        {})
        fi = ev.get('fillers',       {})
        pa = ev.get('pauses',        {})

        agg_time_saved_s      += ts.get('time_saved_s',                0)
        agg_fillers_removed   += fi.get('total_filler_words_detected', 0)
        agg_pauses_cut        += pa.get('cut',                         0)
        agg_pauses_compressed += pa.get('compressed',                  0)
        if qs.get('overall') is not None:
            agg_scores.append(qs['overall'])
        if sp.get('wpm_original'):
            agg_wpm_original.append(sp['wpm_original'])
        if sp.get('wpm_edited'):
            agg_wpm_edited.append(sp['wpm_edited'])
        if pa.get('silence_ratio_original_pct') is not None:
            agg_silence_orig.append(pa['silence_ratio_original_pct'])
        if pa.get('silence_ratio_edited_pct') is not None:
            agg_silence_edit.append(pa['silence_ratio_edited_pct'])

    def _avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else None

    def _score_to_grade(s):
        if s is None: return None
        if s >= 90: return 'A'
        if s >= 80: return 'B'
        if s >= 70: return 'C'
        if s >= 60: return 'D'
        return 'F'

    avg_score    = _avg(agg_scores)
    avg_wpm_orig = _avg(agg_wpm_original)
    avg_wpm_edit = _avg(agg_wpm_edited)

    time_saved_pct = round(agg_time_saved_s / combined_orig_duration * 100, 1) \
                     if combined_orig_duration > 0 else 0
    wpm_increase_pct = round((avg_wpm_edit - avg_wpm_orig) / avg_wpm_orig * 100, 1) \
                       if avg_wpm_orig and avg_wpm_edit and avg_wpm_orig > 0 else None

    combined_evaluation = {
        "quality_score": {"overall": avg_score, "grade": _score_to_grade(avg_score)},
        "time_savings":  {
            "time_saved_s":        round(agg_time_saved_s, 2),
            "time_saved_pct":      time_saved_pct,
            "original_duration_s": round(combined_orig_duration, 2),
            "edited_duration_s":   round(combined_edit_duration,  2),
        },
        "speech":  {"wpm_original": avg_wpm_orig, "wpm_edited": avg_wpm_edit,
                    "wpm_increase_pct": wpm_increase_pct},
        "fillers": {"total_filler_words_detected": agg_fillers_removed},
        "pauses":  {
            "cut":                        agg_pauses_cut,
            "compressed":                 agg_pauses_compressed,
            "silence_ratio_original_pct": _avg(agg_silence_orig),
            "silence_ratio_edited_pct":   _avg(agg_silence_edit),
        },
    } if agg_scores else {}

    job_store[master_id].update({
        "status":            "done",
        "step":              3,
        "step_label":        "Complete",
        "progress":          100,
        "output_path":       final_output,
        "elapsed":           elapsed,
        # Aggregated evaluation — for eval strip and /status/ endpoint
        "evaluation":        combined_evaluation,
        # Aggregated timeline data — for /timeline/ endpoint
        "keep_segments":     combined_keep_segments,
        "filler_cuts":       combined_filler_cuts,
        "decisions":         combined_decisions,
        "filmstrip":         combined_filmstrip,
        "original_duration": combined_orig_duration,
        "edited_duration":   combined_edit_duration,
        "language":          job_store.get(clip_job_ids[0], {}).get('language', 'unknown'),
        # Reference video — propagate from first sub-clip (all clips share the same reference)
        "reference_used":    job_store.get(clip_job_ids[0], {}).get('reference_used', False),
        "style_profile":     job_store.get(clip_job_ids[0], {}).get('style_profile', {}),
    })

    # Clean up individual clip outputs now that they've been merged —
    # the combined video is the only output the user needs.
    # Raw uploads and intermediates for each clip are also no longer needed.
    print(f"\n   🧹 Cleaning up {n} sub-clip files...")
    for clip_id in clip_job_ids:
        clip_job = job_store.get(clip_id, {})
        _cleanup([clip_job.get('output_path')])
        _cleanup([clip_job.get('input_video_path')])
        _cleanup([clip_job.get('intermediate_path')])
        _cleanup([clip_job.get('remapped_srt_path')])
        # Sweep pattern-named temps for this sub-clip
        sub_req = clip_id[:8]
        _cleanup_pattern(f"temp_audio_{sub_req}*.wav")
        _cleanup_pattern(f"subtitles_{sub_req}*.srt")
        _cleanup_pattern(f"ref_audio_{sub_req}*.wav")
        _cleanup_pattern(f"graded_{sub_req}_*")
        _cleanup_pattern(f"temp_clip_{sub_req}_*.mp4")
        _cleanup_pattern(f"_intermediate_{sub_req}_*")
        _cleanup_pattern(f"subs_{sub_req}_working_copy.srt")
        _cleanup_pattern(f"concat_list_{sub_req}.txt")

    print(f"\n✅ MULTI-CLIP MASTER {master_id} COMPLETE in {elapsed:.1f}s → {final_output}")



@app.post("/edit-video/", summary="Submit a video for AI editing")
async def submit_video(
    video_file:        List[UploadFile] = File(...),
    reference_link:    str        = Form(""),
    style_preset:      str        = Form("balanced"),
    subtitle_preset:   str        = Form("default"),
    subtitle_font:     str        = Form("Arial"),
    subtitle_size:     int        = Form(18),
    subtitle_colour:   str        = Form("&H00FFFFFF"),
    subtitle_position: int        = Form(2),
    subtitle_margin_v: int        = Form(30),
    language:          str        = Form("auto"),
    color_grade:       str        = Form("none"),
    cv_analysis:       str        = Form("1"),
    transition_type:   str        = Form("auto"),
    transition_duration: float    = Form(0.5),
    user:               dict      = Depends(auth.get_current_user),
):
    """
    Accepts one or multiple video uploads. Single clip goes straight through
    the pipeline. Multiple clips are each processed independently then
    automatically combined into one final video in the original upload order.
    Returns a job_id to poll — status/download work the same for both cases.

    Requires login — jobs are tagged with the submitting user's id so only
    they can view/download/manage them (see _require_owner below).
    """
    files = video_file if isinstance(video_file, list) else [video_file]

    # Build subtitle style once — shared across all clip jobs
    if subtitle_preset in SUBTITLE_PRESETS:
        sub_style = SUBTITLE_PRESETS[subtitle_preset]
        print(f"   Subtitle preset: {subtitle_preset}")
    else:
        sub_style = SubtitleStyle(
            font_name      = subtitle_font,
            font_size      = subtitle_size,
            primary_colour = subtitle_colour,
            alignment      = subtitle_position,
            margin_v       = subtitle_margin_v,
        )

    CHUNK = 1024 * 1024  # 1 MB

    async def _save_upload(vf: UploadFile, dest_path: str):
        """Stream one UploadFile to disk, enforcing MAX_UPLOAD_BYTES."""
        written = 0
        try:
            with open(dest_path, "wb") as buf:
                while True:
                    chunk = await vf.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        buf.close()
                        _cleanup([dest_path])
                        raise HTTPException(status_code=413,
                            detail=f"File too large. Max {MAX_UPLOAD_BYTES//1024//1024}MB.")
                    buf.write(chunk)
        except HTTPException:
            raise
        except Exception as e:
            _cleanup([dest_path])
            raise HTTPException(status_code=500, detail=f"Upload failed: {e}")
        return written

    # ── Single clip — fast path ───────────────────────────────────────────
    if len(files) == 1:
        vf        = files[0]
        job_id    = uuid.uuid4().hex
        safe_name = os.path.basename(vf.filename)
        input_video_path = f"raw_{job_id[:8]}_{safe_name}"

        print(f"\n📥 Single clip — job {job_id}")
        written = await _save_upload(vf, input_video_path)
        print(f"   ✅ Saved {input_video_path} ({written/1024/1024:.1f}MB)")

        job_store[job_id] = {
            "status": "queued", "step": 0, "step_label": "Queued",
            "progress": 0, "output_path": None,
            "input_video_path": input_video_path,
            "error": None, "elapsed": None, "filename": safe_name,
            "owner_id": user["id"],
        }

        _get_executor().submit(
            _run_pipeline,
            job_id, input_video_path, safe_name,
            reference_link, style_preset, subtitle_preset, sub_style, language, color_grade,
            cv_analysis == "1", transition_type, transition_duration
        )

        return JSONResponse({
            "job_id": job_id, "status": "queued", "clip_count": 1,
            "message": "Job accepted. Poll /status/{job_id} for progress.",
            "poll_url": f"/status/{job_id}",
            "download_url": f"/download/{job_id}",
        })

    # ── Multiple clips — process each then auto-combine ───────────────────
    master_id    = uuid.uuid4().hex
    clip_job_ids = []

    print(f"\n📥 Multi-clip — master {master_id}, {len(files)} clips")

    # Register master job immediately so frontend can poll it
    job_store[master_id] = {
        "status": "queued", "step": 0, "step_label": "Uploading clips",
        "progress": 0, "output_path": None, "input_video_path": None,
        "error": None, "elapsed": None, "filename": "combined_output.mp4",
        "is_combine_master": True, "clip_jobs": [],
        "owner_id": user["id"],
    }

    for i, vf in enumerate(files):
        clip_id   = uuid.uuid4().hex
        safe_name = os.path.basename(vf.filename)
        dest      = f"raw_{clip_id[:8]}_{safe_name}"

        written = await _save_upload(vf, dest)
        print(f"   ✅ Clip {i+1}/{len(files)}: {dest} ({written/1024/1024:.1f}MB)")

        job_store[clip_id] = {
            "status": "queued", "step": 0, "step_label": "Queued",
            "progress": 0, "output_path": None,
            "input_video_path": dest,
            "error": None, "elapsed": None, "filename": safe_name,
            "clip_index": i,
            "owner_id": user["id"],
        }
        clip_job_ids.append(clip_id)

    job_store[master_id]["clip_jobs"] = clip_job_ids

    _get_executor().submit(
        _run_multi_clip_pipeline,
        master_id, clip_job_ids,
        reference_link, style_preset, subtitle_preset, sub_style, language, color_grade,
        cv_analysis == "1", transition_type, transition_duration
    )

    return JSONResponse({
        "job_id": master_id, "status": "queued",
        "clip_count": len(files),
        "message": f"{len(files)} clips accepted — processing then combining automatically.",
        "poll_url": f"/status/{master_id}",
        "download_url": f"/download/{master_id}",
    })



@app.get("/", include_in_schema=False)
async def serve_welcome():
    return FileResponse("welcome.html")

@app.get("/welcome", include_in_schema=False)
@app.get("/welcome.html", include_in_schema=False)
async def serve_welcome_explicit():
    return FileResponse("welcome.html")

@app.get("/login", include_in_schema=False)
@app.get("/login.html", include_in_schema=False)
async def serve_login():
    return FileResponse("login.html")

@app.get("/signup", include_in_schema=False)
@app.get("/signup.html", include_in_schema=False)
async def serve_signup():
    return FileResponse("signup.html")

@app.get("/profile", include_in_schema=False)
@app.get("/profile.html", include_in_schema=False)
async def serve_profile():
    return FileResponse("profile.html")

@app.get("/editor", include_in_schema=False)
@app.get("/editor.html", include_in_schema=False)
async def serve_editor():
    return FileResponse("editor.html")

@app.get("/splash", include_in_schema=False)
@app.get("/splash.html", include_in_schema=False)
async def serve_splash():
    return FileResponse("splash.html")

@app.get("/forgot-password", include_in_schema=False)
@app.get("/forgot_password.html", include_in_schema=False)
async def serve_forgot_password():
    return FileResponse("forgot_password.html")

@app.get("/reset-password", include_in_schema=False)
@app.get("/reset_password.html", include_in_schema=False)
async def serve_reset_password():
    return FileResponse("reset_password.html")



@app.get("/version", summary="Server version fingerprint")
async def get_version():
    """Returns a build fingerprint so you can confirm the server is running the latest code."""
    return JSONResponse({
        "version":   "CutSmart-2025-build-v4",
        "features":  ["reference_prefetch", "timeline_aggregation", "eval_aggregation"],
        "yt_dlp":    __import__('subprocess').getoutput('yt-dlp --version').strip(),
    })


@app.get("/debug/{job_id}", summary="Dump raw job_store entry for debugging")
async def debug_job(job_id: str):
    """Returns the raw job_store dict for a job — useful for debugging missing timeline/reference data."""
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(job_store[job_id])
    # Summarise large fields so the response isn't huge
    if 'filmstrip' in job:
        job['filmstrip'] = f"[{len(job['filmstrip'])} frames]"
    if 'decisions' in job:
        job['decisions'] = f"[{len(job['decisions'])} decisions]"
    if 'keep_segments' in job:
        job['keep_segments_count'] = len(job['keep_segments'])
        job['keep_segments_sample'] = job['keep_segments'][:3]
        del job['keep_segments']
    if 'filler_cuts' in job:
        job['filler_cuts_count'] = len(job['filler_cuts'])
        del job['filler_cuts']
    return JSONResponse(job)


@app.get("/status/{job_id}", summary="Get editing job progress")
async def get_status(job_id: str, user: dict = Depends(auth.get_current_user)):
    """
    Returns the current status of an editing job.

    status values:
      queued      — waiting to start
      processing  — pipeline is running
      done        — video is ready at GET /download/{job_id}
      error       — something went wrong (check 'error' field)
    """
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job = job_store[job_id]
    _require_owner(job_id, job, user)
    evaluation = job.get('evaluation', {})
    eval_summary = None
    if evaluation:
        qs = evaluation.get('quality_score', {})
        ts = evaluation.get('time_savings',  {})
        sp = evaluation.get('speech',        {})
        fi = evaluation.get('fillers',       {})
        pa = evaluation.get('pauses',        {})
        eval_summary = {
            'overall_score':       qs.get('overall'),
            'grade':               qs.get('grade'),
            'time_saved_s':        ts.get('time_saved_s'),
            'time_saved_pct':      ts.get('time_saved_pct'),
            'wpm_original':        sp.get('wpm_original'),
            'wpm_edited':          sp.get('wpm_edited'),
            'wpm_increase_pct':    sp.get('wpm_increase_pct'),
            'fillers_removed':     fi.get('total_filler_words_detected'),
            'pauses_cut':          pa.get('cut'),
            'silence_reduced_pct': round(
                pa.get('silence_ratio_original_pct', 0) -
                pa.get('silence_ratio_edited_pct',   0), 1
            ),
        }

    # Build style profile summary for the Reference Analysis panel
    style_profile    = job.get('style_profile', {})
    reference_used   = job.get('reference_used', False)
    style_summary    = None
    if style_profile and reference_used:
        style_summary = {
            'max_duration':       style_profile.get('max_duration'),
            'compress_duration':  style_profile.get('compress_duration'),
            'speaking_rate':      style_profile.get('speaking_rate'),
            'boundary_ratio':     style_profile.get('boundary_ratio'),
            'cuts_per_minute':    style_profile.get('cuts_per_minute'),
            'summary':            style_profile.get('_summary', ''),
            # Real visual style-match score (cut rhythm/shot length/color vs
            # the reference video) — None when CV analysis was disabled or
            # the reference video's frames couldn't be downloaded, in which
            # case the UI should show "—" rather than an unrelated number.
            'style_match_pct':    style_profile.get('cv_style_match_pct'),
            'style_gap':          style_profile.get('cv_style_gap'),
        }

    return JSONResponse({
        "job_id":           job_id,
        "status":           job['status'],
        "step":             job['step'],
        "step_label":       job['step_label'],
        "progress":         job['progress'],
        "error":            job.get('error'),
        "elapsed":          job.get('elapsed'),
        "evaluation":       eval_summary,
        "reference_used":   reference_used,
        "style_profile":    style_summary,
        "language":         job.get('language', 'unknown'),
        "is_combine_master": job.get('is_combine_master', False),
        "clip_jobs":        job.get('clip_jobs', []),
        "cv_analysis":      {
            k: v for k, v in job.get("raw_scene_analysis", {}).items()
            if k in ["scene_count", "cut_frequency_per_min",
                     "visual_pacing", "avg_shot_duration_s",
                     "color_profile", "transition_types"]
        },
    })


@app.get("/download/{job_id}", summary="Download the edited video")
async def download_video(job_id: str, user: dict = Depends(auth.get_current_user)):
    """
    Returns the edited video file when the job is complete.
    Call GET /status/{job_id} first to confirm status == 'done'.
    """
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job = job_store[job_id]
    _require_owner(job_id, job, user)

    if job['status'] == 'processing' or job['status'] == 'queued':
        raise HTTPException(status_code=202,
                            detail=f"Job is still {job['status']} ({job['progress']}%). Try again soon.")

    if job['status'] == 'error':
        raise HTTPException(status_code=500,
                            detail=f"Job failed: {job.get('error')}")

    output_path = job.get('output_path')
    if not output_path or not os.path.exists(output_path):
        raise HTTPException(status_code=404,
                            detail="Output file not found. It may have been cleaned up.")

    safe_name = job.get('filename', 'edited_video.mp4')

    def _cleanup_after_download():
        """Delete ALL files associated with this job after download completes."""
        req_id = job_id[:8]

        # Primary output + input upload
        _cleanup([output_path])
        _cleanup([job.get('input_video_path')])

        # Intermediate + SRT + animated ASS (kept for /restyle/ but no longer needed after download)
        _cleanup([job.get('intermediate_path')])
        _cleanup([job.get('remapped_srt_path')])
        _cleanup([job.get('remapped_ass_path')])

        # For multi-clip master jobs: clean each sub-clip's files too
        for clip_id in job.get('clip_jobs', []):
            clip_job = job_store.get(clip_id, {})
            _cleanup([clip_job.get('output_path')])
            _cleanup([clip_job.get('input_video_path')])
            _cleanup([clip_job.get('intermediate_path')])
            _cleanup([clip_job.get('remapped_srt_path')])
            job_store.pop(clip_id, None)

        # Sweep any leftover pattern-named temp files for this job prefix
        # (catches anything not tracked in job_store e.g. if pipeline crashed mid-way)
        _cleanup_pattern(f"temp_audio_{req_id}*.wav")
        _cleanup_pattern(f"subtitles_{req_id}*.srt")
        _cleanup_pattern(f"subtitles_{req_id}*.ass")
        _cleanup_pattern(f"subs_{req_id}_anim.ass")
        _cleanup_pattern(f"ref_audio_{req_id}*.wav")
        _cleanup_pattern(f"graded_{req_id}_*")
        _cleanup_pattern(f"_norm_{req_id}_*.mp4")
        _cleanup_pattern(f"_faded_{req_id}_*.mp4")
        _cleanup_pattern(f"_concat_list_{req_id}.txt")
        _cleanup_pattern(f"concat_list_{req_id}.txt")
        _cleanup_pattern(f"subs_{req_id}_working_copy.srt")
        _cleanup_pattern(f"temp_clip_{req_id}_*.mp4")
        _cleanup_pattern(f"_intermediate_{req_id}_*")

        job_store.pop(job_id, None)
        print(f"   🧹 Job {job_id} fully cleaned up")

    from starlette.background import BackgroundTask
    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=f"edited_{safe_name}",
        background=BackgroundTask(_cleanup_after_download)
    )


@app.post("/feedback/", summary="Apply a timestamp correction to an edited video")
async def submit_feedback(
    job_id:          str   = Form(...),
    timestamp_start: float = Form(...),
    timestamp_end:   float = Form(...),
    correction:      str   = Form(...),   # "CUT" or "KEEP"
    user:            dict  = Depends(auth.get_current_user),
):
    """
    Applies a user correction to an already-edited video.

    - correction="CUT"  → removes the specified segment from the edited video
    - correction="KEEP" → restores the specified segment from the original video

    Timestamps refer to regions on the ORIGINAL timeline, as returned by
    GET /timeline/{job_id}. The backend maps a restored region to the correct
    insertion point on the edited timeline.

    Returns a new job_id for the corrected video (poll /status as usual).
    """
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    _require_owner(job_id, job_store[job_id], user)

    try:
        corrected_path = apply_feedback(
            job_id          = job_id,
            timestamp_start = timestamp_start,
            timestamp_end   = timestamp_end,
            correction      = correction,
            job_store       = job_store,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Register the corrected video as a new completed job
    new_job_id = uuid.uuid4().hex
    original_job = job_store[job_id]

    job_store[new_job_id] = {
        'status':           'done',
        'step':             8,
        'step_label':       'Correction applied',
        'progress':         100,
        'output_path':      corrected_path,
        'input_video_path': original_job.get('input_video_path'),
        'error':            None,
        'elapsed':          None,
        'filename':         original_job.get('filename', 'corrected.mp4'),
        'keep_segments':     original_job.get('keep_segments', []),
        'filler_cuts':       original_job.get('filler_cuts', []),
        'decisions':         original_job.get('decisions', []),
        'original_duration': original_job.get('original_duration', 0),
        'edited_duration':   original_job.get('edited_duration', 0),
        'language':          original_job.get('language', 'unknown'),
        'style_profile':     original_job.get('style_profile', {}),
        'reference_used':    original_job.get('reference_used', False),
        'raw_scene_analysis': original_job.get('raw_scene_analysis', {}),
        'filmstrip':         original_job.get('filmstrip', []),
        'owner_id':          user['id'],
    }

    return JSONResponse({
        "job_id":       new_job_id,
        "status":       "done",
        "message":      f"Correction applied. Download at /download/{new_job_id}",
        "download_url": f"/download/{new_job_id}"
    })


@app.get("/report/{job_id}", summary="Get full evaluation report for a completed job")
async def get_report(job_id: str, user: dict = Depends(auth.get_current_user)):
    """
    Returns the complete evaluation report for a finished job.

    Includes all metrics:
      - time_savings    : original vs edited duration, size reduction
      - pauses          : CUT/COMPRESS/KEEP counts, silence ratios, avg durations
      - fillers         : words removed, time saved, per-word breakdown
      - speech          : WPM before/after, transcription quality
      - quality_score   : overall 0-100 score + A-F grade + sub-scores
      - decision_breakdown: what drove each AI decision
      - performance     : processing time, realtime factor
      - input_file      : original video metadata (resolution, fps, codec)
      - output_file     : edited video metadata

    Only available when status == 'done'.
    """
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job = job_store[job_id]
    _require_owner(job_id, job, user)

    if job['status'] != 'done':
        raise HTTPException(
            status_code=400,
            detail=f"Report not available yet — job status is '{job['status']}'."
        )

    evaluation = job.get('evaluation')
    if not evaluation:
        raise HTTPException(
            status_code=404,
            detail="Evaluation data not available for this job."
        )

    return JSONResponse({"job_id": job_id, "report": evaluation})



@app.get("/timeline/{job_id}", summary="Get structured timeline data for dual-timeline UI")
async def get_timeline(job_id: str, user: dict = Depends(auth.get_current_user)):
    """
    Returns all data needed to draw the dual-timeline (original vs edited).

    The frontend uses this to render:
      - Original timeline: all segments coloured by type
        GREEN  = speech kept
        RED    = pause cut
        ORANGE = filler word removed
        YELLOW = pause compressed
      - Edited timeline: only the kept segments, proportionally scaled

    Each segment includes start, end, duration, type, and reason so the
    frontend can show a tooltip on hover explaining why it was cut/kept.

    Also returns the SRT subtitle URL for the HTML5 track element so
    the browser can overlay subtitles natively without FFmpeg.
    """
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job = job_store[job_id]
    _require_owner(job_id, job, user)

    if job['status'] != 'done':
        raise HTTPException(
            status_code=400,
            detail=f"Timeline not available yet — job status is '{job['status']}'."
        )

    original_duration = job.get('original_duration', 0)
    edited_duration   = job.get('edited_duration',   0)
    keep_segments     = job.get('keep_segments',     [])
    filler_cuts       = job.get('filler_cuts',       [])
    decisions         = job.get('decisions',         [])

    # Build lookup dicts for fast type resolution
    cut_regions      = {(round(d['start_time'],3), round(d['end_time'],3)): d
                        for d in decisions if d.get('decision') == 'CUT'}
    compress_regions = {(round(d['start_time'],3), round(d['end_time'],3)): d
                        for d in decisions if d.get('decision') == 'COMPRESS'}
    filler_set       = [(round(s,3), round(e,3)) for s,e in filler_cuts]

    def resolve_region_type(start, end):
        s, e = round(start, 3), round(end, 3)
        for fs, fe in filler_set:
            if abs(fs - s) < 0.05 and abs(fe - e) < 0.05:
                return 'filler_cut', 'Filler word removed'
        for (ds, de), d in cut_regions.items():
            if abs(ds - s) < 0.05 and abs(de - e) < 0.05:
                return 'pause_cut', d.get('reason', 'Pause removed')
        for (ds, de), d in compress_regions.items():
            if abs(ds - s) < 0.05 and abs(de - e) < 0.05:
                return 'pause_compressed', d.get('reason', 'Pause compressed')
        return 'speech_kept', 'Speech kept'

    # Build original timeline by walking keep_segments and identifying gaps
    original_segments = []
    cursor = 0.0

    for seg_start, seg_end in keep_segments:
        if cursor < seg_start - 0.05:
            rtype, reason = resolve_region_type(cursor, seg_start)
            original_segments.append({
                'start':    round(cursor,    3),
                'end':      round(seg_start, 3),
                'duration': round(seg_start - cursor, 3),
                'type':     rtype,
                'reason':   reason,
                'in_edit':  False,
            })
        original_segments.append({
            'start':    round(seg_start, 3),
            'end':      round(seg_end,   3),
            'duration': round(seg_end - seg_start, 3),
            'type':     'speech_kept',
            'reason':   'Speech kept',
            'in_edit':  True,
        })
        cursor = seg_end

    if cursor < original_duration - 0.05:
        rtype, reason = resolve_region_type(cursor, original_duration)
        original_segments.append({
            'start':    round(cursor,            3),
            'end':      round(original_duration, 3),
            'duration': round(original_duration - cursor, 3),
            'type':     rtype,
            'reason':   reason,
            'in_edit':  False,
        })

    # Build edited timeline — kept segments mapped to their new positions
    edited_segments = []
    edited_cursor   = 0.0

    for seg_start, seg_end in keep_segments:
        duration = seg_end - seg_start
        edited_segments.append({
            'original_start': round(seg_start,     3),
            'original_end':   round(seg_end,       3),
            'edited_start':   round(edited_cursor, 3),
            'edited_end':     round(edited_cursor + duration, 3),
            'duration':       round(duration,      3),
            'type':           'speech_kept',
        })
        edited_cursor += duration

    colour_map = {
        'speech_kept':      '#3ecf8e',
        'pause_cut':        '#f05252',
        'pause_compressed': '#f5a623',
        'filler_cut':       '#ff9500',
    }

    srt_path      = job.get('remapped_srt_path', '')
    srt_available = bool(srt_path and os.path.exists(srt_path))

    # Filmstrip thumbnails — base64 JPEG frames evenly spaced along original
    filmstrip = job.get('filmstrip', [])

    return JSONResponse({
        "job_id":            job_id,
        "original_duration": round(original_duration, 3),
        "edited_duration":   round(edited_duration,   3),
        "total_segments":    len(keep_segments),
        "original_timeline": original_segments,
        "edited_timeline":   edited_segments,
        "colour_map":        colour_map,
        "srt_available":     srt_available,
        "srt_url":           f"/srt/{job_id}" if srt_available else None,
        "decisions":         decisions,
        "filler_cuts":       filler_cuts,
        "filmstrip":         filmstrip,
        "stats": {
            "speech_kept_count":      sum(1 for s in original_segments if s['type'] == 'speech_kept'),
            "pause_cut_count":        sum(1 for s in original_segments if s['type'] == 'pause_cut'),
            "pause_compressed_count": sum(1 for s in original_segments if s['type'] == 'pause_compressed'),
            "filler_cut_count":       sum(1 for s in original_segments if s['type'] == 'filler_cut'),
            "total_removed_s":        round(original_duration - edited_duration, 2),
            "filmstrip_count":        len(filmstrip),
        }
    })


@app.get("/srt/{job_id}", summary="Serve the remapped SRT subtitle file")
async def get_srt(job_id: str, user: dict = Depends(auth.get_current_user)):
    """
    Serves the remapped SRT file so the frontend HTML5 video player can
    overlay subtitles natively using the <track> element — no FFmpeg needed.

    Usage in the frontend:
      <video>
        <track src="/srt/{job_id}" kind="subtitles" srclang="en" default/>
      </video>

    Timestamps match the edited video timeline exactly because
    create_remapped_srt() already adjusted them during processing.
    """
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    _require_owner(job_id, job_store[job_id], user)

    srt_path = job_store[job_id].get('remapped_srt_path', '')
    if not srt_path or not os.path.exists(srt_path):
        raise HTTPException(
            status_code=404,
            detail="SRT file not available. It may have been cleaned up after download."
        )

    return FileResponse(
        srt_path,
        media_type="text/plain",
        filename=f"subtitles_{job_id}.srt"
    )


@app.post("/restyle/{job_id}", summary="Re-burn subtitles with new style (Pass 2 only)")
async def restyle_video(
    job_id:            str,              # path parameter — NOT Form
    subtitle_preset:   str = Form("default"),
    subtitle_font:     str = Form("Arial"),
    subtitle_size:     int = Form(18),
    subtitle_colour:   str = Form("&H00FFFFFF"),
    subtitle_position: int = Form(2),
    subtitle_margin_v: int = Form(30),
    user:              dict = Depends(auth.get_current_user),
):
    """
    Re-runs only FFmpeg Pass 2 (subtitle burn) with new style settings.
    The heavy pipeline is already done — this only takes 10-30 seconds.

    Returns a new job_id immediately. Poll /status/{job_id} for progress.
    The original job is preserved so the user can restyle multiple times.

    Note: requires the intermediate (no-subtitle) video to still exist on
    disk under job['intermediate_path']. If it has been cleaned up, returns
    409 and the user must re-submit the full job.
    """
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job = job_store[job_id]
    _require_owner(job_id, job, user)

    if job['status'] != 'done':
        raise HTTPException(
            status_code=400,
            detail=f"Job must be complete to restyle. Status: {job['status']}"
        )

    intermediate_path = job.get('intermediate_path', '')
    if not intermediate_path or not os.path.exists(intermediate_path):
        raise HTTPException(
            status_code=409,
            detail=(
                "Intermediate video not available for restyling — "
                "it was cleaned up after the initial render. "
                "Submit a new job with the desired subtitle settings instead."
            )
        )

    srt_path = job.get('remapped_srt_path', '')
    if not srt_path or not os.path.exists(srt_path):
        raise HTTPException(status_code=409, detail="SRT file not available for restyling.")

    if subtitle_preset in SUBTITLE_PRESETS:
        new_style = SUBTITLE_PRESETS[subtitle_preset]
    else:
        new_style = SubtitleStyle(
            font_name      = subtitle_font,
            font_size      = subtitle_size,
            primary_colour = subtitle_colour,
            alignment      = subtitle_position,
            margin_v       = subtitle_margin_v,
        )

    new_job_id  = uuid.uuid4().hex
    safe_name   = job.get('filename', 'video.mp4')
    output_path = f"restyled_{new_job_id[:8]}_{safe_name}"
    if not output_path.lower().endswith('.mp4'):
        output_path = os.path.splitext(output_path)[0] + '.mp4'

    job_store[new_job_id] = {
        'status':            'processing',
        'step':              7,
        'step_label':        'Burning subtitles',
        'progress':          70,
        'output_path':       None,
        'input_video_path':  job.get('input_video_path'),
        'error':             None,
        'elapsed':           None,
        'filename':          safe_name,
        'keep_segments':     job.get('keep_segments',     []),
        'filler_cuts':       job.get('filler_cuts',       []),
        'decisions':         job.get('decisions',         []),
        'original_duration': job.get('original_duration', 0),
        'edited_duration':   job.get('edited_duration',   0),
        'remapped_srt_path': srt_path,
        'evaluation':        job.get('evaluation',        {}),
        'owner_id':          user['id'],
    }

    def _run_restyle():
        import subprocess
        from render_engine import _make_safe_srt
        try:
            safe_srt   = _make_safe_srt(srt_path, job_prefix=new_job_id[:8])
            style      = new_style
            force      = style.to_force_style()
            sub_filter = f"subtitles={safe_srt}:force_style='{force}'"

            burn_cmd = [
                "ffmpeg", "-y",
                "-i", intermediate_path,
                "-vf", sub_filter,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy",
                output_path
            ]
            subprocess.run(burn_cmd, check=True, capture_output=True, text=True)

            if os.path.exists(safe_srt):
                os.remove(safe_srt)

            job_store[new_job_id].update({
                'status':      'done',
                'step':        8,
                'step_label':  'Restyle complete',
                'progress':    100,
                'output_path': output_path,
            })
            print(f"   ✅ Restyle complete: {output_path}")

        except Exception as exc:
            job_store[new_job_id].update({
                'status':     'error',
                'step_label': 'Restyle failed',
                'error':      str(exc),
            })
            print(f"   ❌ Restyle failed: {exc}")

    _get_executor().submit(_run_restyle)

    return JSONResponse({
        "job_id":       new_job_id,
        "status":       "processing",
        "message":      "Subtitle re-burn started. Poll /status/{job_id} for progress.",
        "poll_url":     f"/status/{new_job_id}",
        "download_url": f"/download/{new_job_id}"
    })



@app.post("/combine-clips/", summary="Combine multiple edited clips into one video (Steps 3 & 4)")
async def combine_edited_clips(
    job_ids:              str   = Form(...),   # comma-separated list of completed job_ids
    transition_type:      str   = Form("auto"),  # auto|hard_cut|crossfade|fade
    transition_duration:  float = Form(0.5),     # seconds (ignored for hard_cut)
    output_name:          str   = Form("combined_output.mp4"),
    user:                 dict  = Depends(auth.get_current_user),
):
    """
    Step 3 & 4 — Multi-clip combiner with transitions.

    Takes a comma-separated list of completed job_ids (each must have
    status='done'), retrieves their edited output files, and stitches
    them into a single combined video.

    Transition options:
      auto       — uses cv_transition_type from the first job's style profile
                   (set when a reference video was provided). Falls back to hard_cut.
      hard_cut   — direct cut between clips, no effect (fastest)
      crossfade  — smooth blend between clips (xfade filter)
      fade       — fade to black then in at each clip boundary

    transition_duration: length of the effect in seconds (default 0.5).
                         Only applies to crossfade and fade. Recommended 0.3–1.0.

    Returns a new job_id. Poll GET /status/{job_id} then GET /download/{job_id}.
    """
    # ── Validate job_ids ──────────────────────────────────────────────────
    ids = [jid.strip() for jid in job_ids.split(",") if jid.strip()]
    if len(ids) < 2:
        raise HTTPException(
            status_code=400,
            detail="At least 2 job_ids are required to combine clips."
        )

    clip_paths   = []
    style_profile_ref = None   # take style profile from first job that has one

    for jid in ids:
        if jid not in job_store:
            raise HTTPException(
                status_code=404,
                detail=f"Job '{jid}' not found. Submit and complete it first."
            )
        job = job_store[jid]
        _require_owner(jid, job, user)
        if job["status"] != "done":
            raise HTTPException(
                status_code=400,
                detail=f"Job '{jid}' is not done yet (status: {job['status']})."
            )
        output_path = job.get("output_path", "")
        if not output_path or not os.path.exists(output_path):
            raise HTTPException(
                status_code=404,
                detail=f"Output file for job '{jid}' not found. It may have been downloaded already."
            )
        clip_paths.append(output_path)

        if style_profile_ref is None and job.get("style_profile"):
            style_profile_ref = job["style_profile"]

    # ── Resolve transition type ───────────────────────────────────────────
    if transition_type == "auto":
        resolved_transition = resolve_transition_type(
            style_profile_ref or {},
            fallback="hard_cut"
        )
    else:
        resolved_transition = transition_type

    # ── Register new combine job ──────────────────────────────────────────
    combine_job_id = uuid.uuid4().hex
    safe_output    = os.path.basename(output_name)
    if not safe_output.lower().endswith(".mp4"):
        safe_output = os.path.splitext(safe_output)[0] + ".mp4"
    final_output = f"combined_{combine_job_id[:8]}_{safe_output}"

    job_store[combine_job_id] = {
        "status":     "queued",
        "step":       0,
        "step_label": "Queued",
        "progress":   0,
        "output_path":      None,
        "input_video_path": None,
        "error":      None,
        "elapsed":    None,
        "filename":   safe_output,
        "owner_id":   user["id"],
    }

    # ── Run in background thread ──────────────────────────────────────────
    def _run_combine():
        import time as _time
        t0 = _time.time()
        try:
            job_store[combine_job_id].update({
                "status":     "processing",
                "step":       3,
                "step_label": "Combining clips",
                "progress":   20,
            })
            print(f"\n{'='*70}")
            print(f"🎬 COMBINE JOB {combine_job_id} STARTED")
            print(f"   Clips  : {len(clip_paths)}")
            print(f"   Transition: {resolved_transition} ({transition_duration}s)")
            print(f"{'='*70}")

            # Bug 4 fix: re-check all source files still exist before starting.
            # A user may have called GET /download/ on a source job between
            # submission and execution, which deletes the output file.
            missing_now = [p for p in clip_paths if not os.path.exists(p)]
            if missing_now:
                raise RuntimeError(
                    f"Source clip(s) were deleted before combining could start "
                    f"(likely downloaded already): {missing_now}. "
                    f"Re-edit those clips and submit a new combine job."
                )

            job_store[combine_job_id].update({
                "step_label": f"Applying {resolved_transition} transitions",
                "progress":   50,
            })

            output = combine_clips(
                clip_paths           = clip_paths,
                transition_type      = resolved_transition,
                transition_duration  = transition_duration,
                output_filename      = final_output,
                job_prefix           = combine_job_id[:8],   # Bug 3 fix: unique temp file prefix
            )

            elapsed = _time.time() - t0
            job_store[combine_job_id].update({
                "status":      "done",
                "step":        4,
                "step_label":  "Complete",
                "progress":    100,
                "output_path": output,
                "elapsed":     elapsed,
            })
            print(f"✅ COMBINE JOB {combine_job_id} DONE in {elapsed:.1f}s")

        except Exception as exc:
            elapsed = _time.time() - t0
            error_msg = str(exc)
            print(f"❌ COMBINE JOB {combine_job_id} FAILED: {error_msg}")
            job_store[combine_job_id].update({
                "status":     "error",
                "step_label": "Failed",
                "error":      error_msg,
                "elapsed":    elapsed,
            })

    _get_executor().submit(_run_combine)

    return JSONResponse({
        "job_id":            combine_job_id,
        "status":            "queued",
        "clips_to_combine":  len(clip_paths),
        "transition":        resolved_transition,
        "transition_duration": transition_duration,
        "message":           "Combine job started. Poll /status/{job_id} for progress.",
        "poll_url":          f"/status/{combine_job_id}",
        "download_url":      f"/download/{combine_job_id}",
    })


@app.get("/jobs/", summary="List your active jobs")
async def list_jobs(user: dict = Depends(auth.get_current_user)):
    """Returns a summary of the current user's jobs currently in memory."""
    return JSONResponse({
        job_id: {
            "status":     job['status'],
            "progress":   job['progress'],
            "step_label": job['step_label'],
            "filename":   job.get('filename'),
        }
        for job_id, job in job_store.items()
        if job.get('owner_id') == user['id']
    })
