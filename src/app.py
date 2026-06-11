from fastapi import FastAPI, UploadFile, File, Form, HTTPException, status, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import uuid
import time
import threading

from transcription_service import extract_audio, transcribe_audio
from feature_extractor      import extract_pause_features
from filler_remover         import detect_fillers
from style_extractor        import download_youtube_audio, calculate_style_profile
from decision_engine        import classify_pauses
from render_engine          import get_keep_segments, render_final_video, SubtitleStyle, SUBTITLE_PRESETS
from subtitle_service       import create_srt, create_remapped_srt
from feedback_service       import apply_feedback

app = FastAPI(title="AI Video Editor", version="1.0")

# ---------------------------------------------------------------------------
# CORS — allows the frontend (any localhost port) to call this API
# In production, replace ["*"] with your actual frontend domain
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # dev: allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def _cleanup(paths: list):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


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
                  reference_link: str, style_preset: str,
                  subtitle_style: SubtitleStyle = None):
    """
    Full editing pipeline. Runs in a background thread so POST /edit-video/
    can return a job_id immediately without waiting 2-10 minutes.
    """
    start_time = time.time()
    req_id     = job_id[:8]

    audio_path        = f"temp_audio_{req_id}.wav"
    srt_path          = f"subtitles_{req_id}.srt"
    remapped_srt_path = f"subtitles_remapped_{req_id}.srt"
    ref_audio_path    = f"ref_audio_{req_id}.wav"
    output_filename   = f"edited_{req_id}_{safe_name}"
    if not output_filename.lower().endswith('.mp4'):
        output_filename = os.path.splitext(output_filename)[0] + '.mp4'

    temp_files = [audio_path, srt_path, remapped_srt_path, ref_audio_path]

    print(f"\n{'='*70}")
    print(f"🚀 JOB {job_id} STARTED — {safe_name}")
    print(f"{'='*70}")

    try:
        # ── Step 1: Audio extraction ──────────────────────────────────────
        _update_job(job_id, 1, "Extracting audio", 5)
        success = extract_audio(input_video_path, audio_path)
        if not success:
            raise RuntimeError("Audio extraction failed. Check FFmpeg is installed.")

        # ── Step 2: Transcription ─────────────────────────────────────────
        _update_job(job_id, 2, "Transcribing speech", 15)
        transcript_data = transcribe_audio(audio_path)
        num_words = sum(len(s.get('words', [])) for s in transcript_data.get('segments', []))
        print(f"   ✅ {num_words} words transcribed")

        # ── Step 3: Subtitle generation ───────────────────────────────────
        _update_job(job_id, 3, "Generating subtitles", 25)
        create_srt(transcript_data, srt_path)

        # ── Step 4: Pause analysis ────────────────────────────────────────
        _update_job(job_id, 4, "Analysing pauses", 35)
        pause_features = extract_pause_features(transcript_data, audio_path)
        print(f"   ✅ {len(pause_features)} pauses detected")

        # ── Step 4.5: Filler word detection ───────────────────────────────
        _update_job(job_id, 4, "Detecting filler words", 40)
        filler_cuts = detect_fillers(transcript_data)
        print(f"   ✅ {len(filler_cuts)} filler cut region(s) detected")

        # ── Step 5: Style profile ─────────────────────────────────────────
        _update_job(job_id, 5, "Building style profile", 48)

        if reference_link:
            print(f"   📺 Analysing reference video...")
            try:
                ref_audio = download_youtube_audio(reference_link, ref_audio_path)
                if ref_audio and os.path.exists(ref_audio):
                    ref_transcript = transcribe_audio(ref_audio)
                    ref_pauses     = extract_pause_features(ref_transcript, ref_audio)
                    style_profile  = calculate_style_profile(ref_pauses, ref_transcript)
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

        video_duration = _get_video_duration(transcript_data)

        # Pass filler_cuts into get_keep_segments so they're merged with
        # pause decisions into one unified cut list
        clips_to_keep = get_keep_segments(decisions_df, video_duration,
                                          filler_cuts=filler_cuts)

        create_remapped_srt(transcript_data, clips_to_keep, remapped_srt_path)

        _update_job(job_id, 7, "Encoding clips", 75)
        render_final_video(input_video_path, clips_to_keep,
                           remapped_srt_path, output_filename,
                           subtitle_style=subtitle_style)

        # ── Step 8: Cleanup ───────────────────────────────────────────────
        _update_job(job_id, 8, "Cleaning up", 95)
        _cleanup(temp_files)

        elapsed = time.time() - start_time
        print(f"\n{'='*70}")
        print(f"✅ JOB {job_id} DONE — {elapsed:.1f}s ({elapsed/60:.1f}m)")
        print(f"{'='*70}\n")

        # Mark job as done — keep input_video_path for feedback splicing
        job_store[job_id].update({
            'status':           'done',
            'step':             8,
            'step_label':       'Complete',
            'progress':         100,
            'output_path':      output_filename,
            'input_video_path': input_video_path,
            'elapsed':          elapsed,
        })

    except Exception as e:
        _cleanup(temp_files)
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

@app.post("/edit-video/", summary="Submit a video for AI editing")
async def submit_video(
    video_file:        UploadFile = File(...),
    reference_link:    str        = Form(""),
    style_preset:      str        = Form("balanced"),
    subtitle_preset:   str        = Form("default"),
    subtitle_font:     str        = Form("Arial"),
    subtitle_size:     int        = Form(18),
    subtitle_colour:   str        = Form("&H00FFFFFF"),
    subtitle_position: int        = Form(2),
    subtitle_margin_v: int        = Form(30),
):
    """
    Accepts a video upload and starts the editing pipeline in the background.
    Returns a job_id immediately — poll GET /status/{job_id} for progress,
    then GET /download/{job_id} when status is 'done'.

    Subtitle styling options:
      subtitle_preset:   one of default / bold_white / yellow / top / box / minimal
      subtitle_font:     any font installed on the server (Arial, Helvetica, etc.)
      subtitle_size:     font size in points (default 18)
      subtitle_colour:   FFmpeg hex colour &HAABBGGRR (default &H00FFFFFF = white)
      subtitle_position: numpad alignment (2=bottom-center, 8=top-center, 1=bottom-left)
      subtitle_margin_v: vertical margin in pixels from the edge (default 30)

    If subtitle_preset is not 'default', it takes priority and individual fields
    are ignored. Set subtitle_preset='custom' to use individual field values.
    """
    job_id    = uuid.uuid4().hex
    safe_name = os.path.basename(video_file.filename)
    req_id    = job_id[:8]

    input_video_path = f"raw_{req_id}_{safe_name}"

    print(f"\n📥 Saving upload for job {job_id}...")

    # Stream file to disk in 1MB chunks — avoids loading entire video into RAM
    # Also enforces the MAX_UPLOAD_BYTES size limit
    bytes_written = 0
    CHUNK = 1024 * 1024   # 1MB chunks
    try:
        with open(input_video_path, "wb") as buf:
            while True:
                chunk = await video_file.read(CHUNK)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    buf.close()
                    _cleanup([input_video_path])
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Maximum allowed size is {MAX_UPLOAD_BYTES // 1024 // 1024}MB."
                    )
                buf.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        _cleanup([input_video_path])
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    print(f"   ✅ Saved: {input_video_path} ({bytes_written / 1024 / 1024:.1f}MB)")

    # Register job immediately so /status can find it right away
    job_store[job_id] = {
        'status':           'queued',
        'step':             0,
        'step_label':       'Queued',
        'progress':         0,
        'output_path':      None,
        'input_video_path': input_video_path,
        'error':            None,
        'elapsed':          None,
        'filename':         safe_name,
    }

    # Build subtitle style — preset takes priority over individual fields
    if subtitle_preset in SUBTITLE_PRESETS:
        sub_style = SUBTITLE_PRESETS[subtitle_preset]
        print(f"   🎨 Subtitle preset: {subtitle_preset}")
    else:
        sub_style = SubtitleStyle(
            font_name      = subtitle_font,
            font_size      = subtitle_size,
            primary_colour = subtitle_colour,
            alignment      = subtitle_position,
            margin_v       = subtitle_margin_v,
        )
        print(f"   🎨 Custom subtitle style: font={subtitle_font} size={subtitle_size} "
              f"colour={subtitle_colour} align={subtitle_position}")

    # Launch pipeline in background thread — returns immediately
    _get_executor().submit(
        _run_pipeline,
        job_id, input_video_path, safe_name,
        reference_link, style_preset, sub_style
    )

    return JSONResponse({
        "job_id":   job_id,
        "status":   "queued",
        "message":  "Job accepted. Poll /status/{job_id} for progress.",
        "poll_url": f"/status/{job_id}",
        "download_url": f"/download/{job_id}"
    })


@app.get("/status/{job_id}", summary="Check job progress")
async def get_status(job_id: str):
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
    return JSONResponse({
        "job_id":     job_id,
        "status":     job['status'],
        "step":       job['step'],
        "step_label": job['step_label'],
        "progress":   job['progress'],
        "error":      job.get('error'),
        "elapsed":    job.get('elapsed'),
    })


@app.get("/download/{job_id}", summary="Download the edited video")
async def download_video(job_id: str):
    """
    Returns the edited video file when the job is complete.
    Call GET /status/{job_id} first to confirm status == 'done'.
    """
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job = job_store[job_id]

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
        """Delete output file and input video after download completes."""
        _cleanup([output_path])
        input_path = job.get('input_video_path')
        if input_path:
            _cleanup([input_path])
        # Remove job from store
        job_store.pop(job_id, None)

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
    correction:      str   = Form(...)   # "CUT" or "KEEP"
):
    """
    Applies a user correction to an already-edited video.

    - correction="CUT"  → removes the specified segment from the edited video
    - correction="KEEP" → restores the specified segment from the original video

    timestamps refer to positions in the EDITED video timeline.

    Returns a new job_id for the corrected video (poll /status as usual).
    """
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
    }

    return JSONResponse({
        "job_id":       new_job_id,
        "status":       "done",
        "message":      f"Correction applied. Download at /download/{new_job_id}",
        "download_url": f"/download/{new_job_id}"
    })


@app.get("/jobs/", summary="List all active jobs")
async def list_jobs():
    """Returns a summary of all jobs currently in memory."""
    return JSONResponse({
        job_id: {
            "status":     job['status'],
            "progress":   job['progress'],
            "step_label": job['step_label'],
            "filename":   job.get('filename'),
        }
        for job_id, job in job_store.items()
    })