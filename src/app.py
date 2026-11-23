import asyncio
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from collections import Counter
from functools import partial
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Make sure Homebrew's ffmpeg/ffprobe and the venv's CLI tools (yt-dlp) are found
# even if the terminal PATH lacks them (e.g. venv not activated)
for _bin in (os.path.dirname(sys.executable), "/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
    if os.path.isdir(_bin) and _bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("\n❌ FFmpeg/ffprobe NOT FOUND. Run:  brew install ffmpeg   then restart the server.\n")
else:
    print("✅ FFmpeg found:", shutil.which("ffmpeg"))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # DATABASE_URL, JWT_SECRET etc.; read by the imports below

import workspace
workspace.enter()  # job files live in WORK_DIR, never in the source tree

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

import auth
import caption_preview
import cut_list
import edit_render
import font_catalog
import jobs
import media_probe
import pipeline
import project_plan
import reframe
import stale_pages
import templates
from clip_combiner import MAX_TRANSITION_S, TRANSITIONS
from comment_parser import parse_command
from cut_commands import parse_cut
from edit_render import build_style
from filenames import as_mp4, safe_filename
from reference_caption_style import extract_caption_style
from render_engine import burn_subtitles

# ── yt-dlp availability check ───────────────────────────────────────────────
# Warn early if yt-dlp is missing or broken — prevents silent fallback to
# balanced preset when the user provides a YouTube reference URL.
try:
    _ytdlp_ver = subprocess.check_output(['yt-dlp', '--version'], stderr=subprocess.DEVNULL).decode().strip()
    print(f"✅ yt-dlp {_ytdlp_ver} found — reference video analysis available")
except Exception:
    _ytdlp_ver = None
    print("⚠️  yt-dlp NOT found — reference YouTube URLs will be ignored.")
    print("   Install with: pip install yt-dlp")
# ────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(_app):
    workspace.run_periodically(_sweep_expired_jobs)
    yield


app = FastAPI(title="CutSmart", version="1.0", lifespan=_lifespan)

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
    secret_key=auth.JWT_SECRET,
)
app.add_middleware(stale_pages.EvictStalePages, secure=auth.SECURE_COOKIES)
auth.init_db()
templates.init_db()
app.include_router(auth.router)

# Max upload size: 1GB. Larger files are rejected before reading into memory.
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024

job_store = jobs.store      # see jobs.Job for every key

MAX_CLIPS = 10
MAX_PAUSE_S = (0.2, 5.0)        # the Advanced sliders' ranges
COMPRESS_PAUSE_S = (0.1, 3.0)

_export_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="export")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upright(path: str, degrees: int, job_id: str) -> str:
    """The upload rotated upright, as a file of ``job_id``'s, or ``path`` if no rotation.

    The upload itself stays, so the clip can be turned again without sending it again."""
    if int(degrees) % 360 not in (90, 180, 270):
        return path
    out = f"upright_{job_id[:8]}.mp4"
    try:
        return edit_render.rotate(path, degrees, out)
    except subprocess.CalledProcessError:
        jobs.discard([out])
        raise HTTPException(status_code=422, detail="Couldn't rotate this video.")


def _reject_unusable(uploads: list):
    """422 with a clear reason (and the saved files removed) if any upload can't be edited."""
    for path, name in uploads:
        problem = media_probe.upload_problem(path, name)
        if problem:
            jobs.discard([p for p, _ in uploads])
            raise HTTPException(status_code=422, detail=problem)


JOB_NOT_FOUND = "This video wasn't found. It may have expired; generate it again."


def _find_job(job_id: str, user: dict) -> jobs.Job:
    """The caller's job. 404 if unknown, 403 if it is someone else's (or has no owner recorded)."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=JOB_NOT_FOUND)
    if job.get("owner_id") != user["id"]:
        raise HTTPException(status_code=403, detail="You don't have access to this job")
    return job


def _owned_job(job_id: str, user: dict = Depends(auth.get_current_user)) -> jobs.Job:
    """Dependency for every /.../{job_id} route."""
    return _find_job(job_id, user)


async def _save_upload(upload: UploadFile, dest: str) -> int:
    """Stream an upload to disk within MAX_UPLOAD_BYTES; nothing is left behind if it fails."""
    written = 0
    try:
        with open(dest, "wb") as out:
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413,
                                        detail=f"File too large. Max {MAX_UPLOAD_BYTES//1024//1024}MB.")
                await run_in_threadpool(out.write, chunk)
    except HTTPException:
        jobs.discard([dest])
        raise
    except Exception as exc:
        jobs.discard([dest])
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")
    return written


def _check_transition(kind: str, seconds: float):
    if kind not in TRANSITIONS:
        raise HTTPException(status_code=400, detail="Unknown transition.")
    if not (math.isfinite(seconds) and 0 < seconds <= MAX_TRANSITION_S):
        raise HTTPException(status_code=400,
                            detail=f"A transition must last more than 0 and at most {MAX_TRANSITION_S:g} seconds.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _style_fields(
    subtitle_preset:   str   = Form("default"),
    subtitle_font:     str   = Form("Arial"),
    subtitle_size:     int   = Form(18),
    subtitle_colour:   str   = Form("&H00FFFFFF"),
    subtitle_position: int   = Form(2),
    subtitle_margin_v: int   = Form(30),
    words_per_caption: int   = Form(0),
    emphasis:          int   = Form(0),
    emph_font:         str   = Form("Arial Black"),
    emph_colour:       str   = Form("&H0030EAB4"),
    emph_scale:        float = Form(2.0),
    emph_glow:         int   = Form(1),
    emph_italic:       int   = Form(0),
    emph_look:         str   = Form("classic"),
    emph_every:        int   = Form(2),
    sub_bold:          int   = Form(0),
    sub_italic:        int   = Form(0),
    pos_y:             float = Form(0.53),
    pos_x:             float = Form(0.5),
    sub_align:         str   = Form("c"),
    caption_lines:     int   = Form(2),
    letter_spacing:    float = Form(0.0),
    line_spacing:      float = Form(1.0),
    offset_y:          float = Form(0.0),
) -> dict:
    """The caption look, as every endpoint that draws captions receives it."""
    return dict(locals())


def _threshold(value: float | None, low: float, high: float, what: str) -> float | None:
    if value is not None and not (math.isfinite(value) and low <= value <= high):
        raise HTTPException(status_code=400, detail=f"{what} must be between {low:g} and {high:g} seconds.")
    return value


def _frame_of(setting: dict) -> dict | None:
    return reframe.normalize_frame(str(setting.get("format") or ""), setting.get("mode", "auto"),
                                   setting.get("cx", 0.5), setting.get("cy", 0.5), setting.get("zoom", 1.0))


def _plan_failure(error: project_plan.PlanError) -> HTTPException:
    return HTTPException(status_code=409 if isinstance(error, project_plan.Expired) else 400, detail=str(error))


def _start(submission: project_plan.Submission) -> dict:
    if submission.work:
        work, *args = submission.work
        jobs.in_background(submission.job_id, work, *args)
    else:
        jobs.touch(submission.job_id)
    return {"job_id": submission.job_id, "poll_url": f"/status/{submission.job_id}",
            "status": job_store[submission.job_id]["status"]}


@app.post("/edit-video/", summary="Edit a project: new uploads, or changes to an edit")
async def submit_video(
    video_file:        List[UploadFile] = File([]),
    clips:             str        = Form(""),      # JSON, see project_plan.Clip; none = every upload is a clip
    base_job:          str        = Form(""),      # the edit being changed, for what its join can reuse
    reference_link:    str        = Form(""),
    require_reference: int        = Form(0),       # 1 = fail rather than fall back to the preset
    style_preset:      str        = Form("balanced"),
    max_pause:         float | None = Form(None),  # overrides the preset's thresholds
    compress_pause:    float | None = Form(None),
    language:          str        = Form("auto"),
    color_grade:       str        = Form("none"),
    cv_analysis:       str        = Form("1"),
    transition_type:   str        = Form("auto"),
    transition_duration: float    = Form(0.5),
    frame_format:      str        = Form(""),      # "vertical" | "landscape" | "square" | "" (leave as uploaded)
    frame_mode:        str        = Form("auto"),  # "auto" (follow the speaker) | "custom" (user-dragged frame)
    frame_cx:          float      = Form(0.5),
    frame_cy:          float      = Form(0.5),
    frame_zoom:        float      = Form(1.0),
    copy_caption_style: int       = Form(0),       # 1 = also read the reference's on-screen caption look
    rotate:            int        = Form(0),       # clockwise degrees (0/90/180/270) for sideways footage
    user:               dict      = Depends(auth.get_current_user),
):
    """
    A project is an ordered list of clips. Each is a file sent now or a finished clip
    of the caller's; only clips whose settings changed are made again.
    Returns a job_id to poll at /status/{job_id}.
    """
    if style_preset not in pipeline.PACING_PRESETS:
        raise HTTPException(status_code=400, detail="Unknown silence-trimming preset.")
    _check_transition(transition_type, transition_duration)
    max_pause = _threshold(max_pause, *MAX_PAUSE_S, "The longest pause to keep")
    compress_pause = _threshold(compress_pause, *COMPRESS_PAUSE_S, "The pause to shorten")
    preset = pipeline.PACING_PRESETS[style_preset]
    if (max_pause is not None or compress_pause is not None) and (
            (compress_pause or preset["compress_duration"]) > (max_pause or preset["max_duration"])):
        raise HTTPException(status_code=400, detail="Pauses can't be shortened later than they are cut.")
    if len(video_file) > MAX_CLIPS:
        raise HTTPException(status_code=400, detail=f"Upload at most {MAX_CLIPS} clips at a time.")

    defaults = {"rotate": rotate, "transition": transition_type,
                "frame": {"format": frame_format, "mode": frame_mode, "cx": frame_cx, "cy": frame_cy, "zoom": frame_zoom}}
    try:
        wanted = project_plan.parse_clips(clips, len(video_file), defaults)
        if len(wanted) > MAX_CLIPS:
            raise HTTPException(status_code=400, detail=f"A project holds at most {MAX_CLIPS} clips.")
        for clip in wanted:
            _check_transition(clip.transition, transition_duration)
        base = _find_job(base_job, user) if base_job and base_job in job_store else None
        project_plan.attach_previous(wanted, lambda job_id: _find_job(job_id, user))
    except project_plan.PlanError as error:
        raise _plan_failure(error)

    # ── Receive and check every file first: nothing is registered until all of them are usable
    written = []
    try:
        fresh = [clip for clip in wanted if clip.upload is not None]
        for clip in wanted:
            clip.job_id = uuid.uuid4().hex
        for clip in fresh:
            upload = video_file[clip.upload]
            clip.name = safe_filename(upload.filename)
            clip.source_path = f"raw_{clip.job_id[:8]}_{clip.name}"
            written.append(clip.source_path)
            size = await _save_upload(upload, clip.source_path)
            print(f"   📥 Saved {clip.source_path} ({size/1024/1024:.1f}MB)")
        # ffprobe and the rotate re-encode are slow: never on the event loop
        await run_in_threadpool(_reject_unusable, [(clip.source_path, clip.name) for clip in fresh])
        for clip in wanted:
            if not clip.input_path:
                clip.input_path = await run_in_threadpool(_upright, clip.source_path, clip.rotate, clip.job_id)
                if clip.input_path != clip.source_path:
                    written.append(clip.input_path)

        options = pipeline.EditOptions(
            style_preset=style_preset, language=language, color_grade=color_grade, cv_analysis=cv_analysis == "1",
            transition_type=transition_type, max_pause=max_pause, compress_pause=compress_pause,
            reference_link=reference_link.strip(), require_reference=bool(require_reference),
            copy_caption_style=bool(copy_caption_style and reference_link.strip()),
        )
        submission = await run_in_threadpool(
            project_plan.submit_edit, user["id"], wanted, options, [_frame_of(clip.frame) for clip in wanted],
            transition_duration, base)
    except BaseException:       # refused, failed, or the client went away
        jobs.discard(written)
        raise
    return JSONResponse({**_start(submission), "clip_count": len(wanted)})


def _page(name: str) -> FileResponse:
    """An HTML page; browsers revalidate it so an update is never hidden by a stale copy."""
    return FileResponse(workspace.page(name), headers={"Cache-Control": "no-cache"})


PAGES = {
    "/": "welcome.html", "/welcome": "welcome.html", "/welcome.html": "welcome.html",
    "/studio": "studio.html", "/studio.html": "studio.html",
    "/home": "home.html", "/home.html": "home.html",
    "/login": "login.html", "/login.html": "login.html",
    "/signup": "signup.html", "/signup.html": "signup.html",
    "/profile": "profile.html", "/profile.html": "profile.html",
    "/splash": "splash.html", "/splash.html": "splash.html",
    "/forgot-password": "forgot_password.html", "/forgot_password.html": "forgot_password.html",
    "/reset-password": "reset_password.html", "/reset_password.html": "reset_password.html",
}
for _path, _file in PAGES.items():
    app.add_api_route(_path, partial(_page, _file), include_in_schema=False)

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
os.makedirs(FONTS_DIR, exist_ok=True)
app.mount("/fonts", StaticFiles(directory=FONTS_DIR), name="fonts")
IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
os.makedirs(IMAGES_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")


class _Assets(StaticFiles):
    """Shared front-end code; revalidated like the pages, so a fix is never hidden by a cached script."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/assets", _Assets(directory=workspace.page("assets")), name="assets")


@app.get("/font-catalog", include_in_schema=False)
def get_font_catalog():
    """Font family -> file URLs, so the browser's libass loads the same fonts the export uses."""
    return JSONResponse(font_catalog.catalog(), headers={"Cache-Control": "max-age=3600"})


@app.get("/system-fonts/{filename}", include_in_schema=False)
def get_system_font(filename: str):
    path = font_catalog.system_font_path(filename)  # allowlisted template fonts only
    if not path:
        raise HTTPException(status_code=404, detail="Font not found")
    return FileResponse(path, media_type="font/ttf", headers={"Cache-Control": "max-age=86400"})

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(workspace.page("assets/favicon.svg"), media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/version", summary="Server version")
def get_version():
    """Which build is running. Everything here was worked out at startup."""
    return JSONResponse({"version": app.version, "yt_dlp": _ytdlp_ver})


@app.get("/debug/{job_id}", include_in_schema=False)
def debug_job(job_id: str, user: dict = Depends(auth.get_current_user)):
    """Returns the raw job_store dict for one of your jobs. Development only (DEV_MODE=1)."""
    if os.environ.get("DEV_MODE") != "1":
        raise HTTPException(status_code=404, detail="Not Found")
    job = {k: v for k, v in list(_find_job(job_id, user).items()) if k != 'transcript_data'}
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
    return JSONResponse(json.loads(json.dumps(job, default=str)))  # styles, numpy values -> text


@app.get("/status/{job_id}", summary="Get editing job progress")
async def get_status(job_id: str, job: dict = Depends(_owned_job)):
    """
    Returns the current status of an editing job.

    status values:
      queued      — waiting to start
      processing  — pipeline is running
      done        — the edit is ready at GET /intermediate/{job_id}
      error       — something went wrong (check 'error' field)
    """
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


_EXPORTS = {}
_exports_lock = threading.Lock()


def _sweep_expired_jobs():
    """Drop jobs and exports past their time, then any file or job folder nothing points at."""
    now = time.time()
    jobs.expire(now)
    for rid, export in list(_EXPORTS.items()):
        age = now - export['created_at']
        busy = export['status'] == 'processing'
        if ((busy and age > max(workspace.TTL_SECONDS, jobs.STUCK_SECONDS))
                or (not busy and (workspace.is_expired(export['created_at'], now)
                                  or not os.path.exists(export['path'])))):
            _EXPORTS.pop(rid, None)
    live = {jid[:8] for jid in list(job_store)} | {rid[:8] for rid in list(_EXPORTS)}  # never orphans
    used = jobs.referenced_files()
    removed = workspace.sweep_files(now, keep=lambda name: name in used or workspace.job_prefix_of(name) in live)
    if removed:
        print(f"   🧹 Swept {len(removed)} expired workspace file(s)")


@app.post("/feedback/", summary="Apply a timestamp correction to an edit")
def submit_feedback(
    job_id:          str   = Form(...),
    timestamp_start: float = Form(...),
    timestamp_end:   float = Form(...),
    correction:      str   = Form(...),   # "CUT" or "KEEP"
    user:            dict  = Depends(auth.get_current_user),
):
    """
    Step 6 (Feedback & Refinement): put a removed range back (KEEP) or take a
    kept range out (CUT). Timestamps are on the project's ORIGINAL timeline, as
    returned by GET /timeline/{job_id}, so a range may be in any of its clips.

    Only the changed segments are encoded again. The edit corrected stays as it
    was, so corrections chain and can be undone.
    Returns a new job_id; poll /status/{job_id} as usual.
    """
    job = _find_job(job_id, user)       # a form field here, not a path
    if job.get('status') != 'done':
        raise HTTPException(status_code=400, detail="This video is still processing.")
    try:
        if not (math.isfinite(timestamp_start) and math.isfinite(timestamp_end)):
            raise project_plan.PlanError("Timestamps must be numbers of seconds.")
        submission = project_plan.submit_correction(
            user['id'], job_id, job, timestamp_start, timestamp_end, correction)
    except project_plan.PlanError as error:
        raise _plan_failure(error)
    return JSONResponse(_start(submission))


@app.get("/report/{job_id}", summary="Get full evaluation report for a completed job")
async def get_report(job_id: str, job: dict = Depends(_owned_job)):
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



COLOUR_MAP = {
    'speech_kept':      '#3ecf8e',
    'pause_cut':        '#f05252',
    'pause_compressed': '#f5a623',
    'filler_cut':       '#ff9500',
    'silence_cut':      '#9b8afb',
    'user_cut':         '#8b95a5',
}
PAUSE_KEPT_COLOUR = '#2f8f9d'


def _cut_reasons(job: dict) -> list[dict]:
    """Why each range left the edit, for labelling the timeline (later entries win ties)."""
    reasons = []
    for s_, e_ in job.get('edge_cuts', []):
        reasons.append({'start': s_, 'end': e_, 'kind': 'silence_cut',
                        'reason': 'Silence at the start' if s_ <= 0.01 else 'Silence at the end'})
    for d in job.get('decisions', []):
        if d.get('decision') in ('CUT', 'COMPRESS'):
            reasons.append({'start': d['start_time'], 'end': d['end_time'],
                            'kind': 'pause_cut' if d['decision'] == 'CUT' else 'pause_compressed',
                            'reason': d.get('reason') or 'Pause removed'})
    for s_, e_ in job.get('filler_cuts', []):
        reasons.append({'start': s_, 'end': e_, 'kind': 'filler_cut', 'reason': 'Filler word removed'})
    for c in job.get('corrections', []):
        if c['correction'] == 'CUT':
            reasons.append({'start': c['start'], 'end': c['end'], 'kind': 'user_cut', 'reason': 'Removed by you'})
    return reasons


def _reference_analysis(job: dict, members: list) -> dict | None:
    """What was measured from the reference video this edit follows (Step 4: Style Adaptation)."""
    if not job.get('reference_used'):
        return None
    profiles = [clip.get('style_profile', {}) for _, _, clip in members]
    matches = [p['cv_style_match_pct'] for p in profiles if p.get('cv_style_match_pct') is not None]
    wanted = ('speaking_rate', 'cuts_per_minute', 'boundary_ratio', 'max_duration', 'compress_duration')
    return {**{key: profiles[0].get(key) for key in wanted},
            'style_match_pct': sum(matches) / len(matches) if matches else None}


@app.get("/timeline/{job_id}", summary="What the timeline draws: clips, kept and removed ranges, frames")
def get_timeline(job_id: str, job: dict = Depends(_owned_job)):
    """
    Both timelines of an edit, across all of its clips.

      original_timeline  every range of the uploads laid end to end: kept, or removed and why
      edited_timeline    where each kept range plays in the edit
      clips              each clip's span on both, with its key and job
    """
    if job['status'] != 'done':
        raise HTTPException(
            status_code=400,
            detail=f"Timeline not available yet — job status is '{job['status']}'."
        )

    members = project_plan.clips_of(job_id, job)
    starts = job.get('clip_starts') or [0.0]
    used = job.get('transitions_used', [])
    clips, original, edited, film, pauses = [], [], [], [], []
    offset = 0.0
    for index, ((key, clip_id, clip), start) in enumerate(zip(members, starts)):
        length = clip.get('original_duration', 0)
        keep = clip.get('keep_segments', [])
        lengths = clip.get('real_durations') or [e - s for s, e in keep]
        for segment in cut_list.label_timeline(keep, length, _cut_reasons(clip)):
            original.append({**segment, 'start': round(segment['start'] + offset, 3),
                             'end': round(segment['end'] + offset, 3), 'clip': index})
        at = start
        for (first, last), on_screen in zip(keep, lengths):
            if on_screen <= 0:
                continue
            edited.append({'original_start': round(first + offset, 3), 'original_end': round(last + offset, 3),
                           'edited_start': round(at, 3), 'edited_end': round(at + on_screen, 3),
                           'clip': index, 'type': 'speech_kept'})
            at += on_screen
        pauses += [{'start': round(d['start_time'] + offset, 3), 'end': round(d['end_time'] + offset, 3),
                    'decision': d['decision'], 'reason': d.get('reason', ''), 'clip': index}
                   for d in clip.get('decisions', [])]
        film += [{**frame, 'timestamp': round(frame['timestamp'] + offset, 2), 'clip': index}
                 for frame in clip.get('filmstrip', [])]
        clips.append({
            'key': key, 'job_id': clip_id, 'name': clip.get('filename'), 'rotate': clip.get('rotate', 0),
            'original_start': round(offset, 3), 'original_end': round(offset + length, 3),
            'edited_start': round(start, 3), 'edited_end': round(at, 3),
            'transition': used[index] if index < len(used) else None,
            'burned_captions': clip.get('burned_captions', False),
        })
        offset += length

    edited_duration = job.get('edited_duration', 0)
    counts = Counter(segment['type'] for segment in original)
    return JSONResponse({
        "job_id":            job_id,
        "original_duration": round(offset, 3),
        "edited_duration":   round(edited_duration, 3),
        "clips":             clips,
        "original_timeline": original,
        "edited_timeline":   edited,
        "colour_map":        {**COLOUR_MAP, "pause_kept": PAUSE_KEPT_COLOUR},
        "pauses":            pauses,
        "filmstrip":         film,
        "reference_used":    job.get('reference_used', False),
        "reference":         _reference_analysis(job, members),
        "caption_look":      job.get('caption_look'),
        "stats": {
            **{f"{kind}_count": counts[kind] for kind in COLOUR_MAP},
            "total_removed_s": round(offset - edited_duration, 2),
        },
    })


def _caption_fields(
    captions: str  = Form("[]"),   # JSON [{"s","e","t"}] on the EDITED timeline (used when edited=1)
    style:    dict = Depends(_style_fields),
    edited:   int  = Form(0),      # 1 = the user changed caption text/timing: render the list as given
    respect:  int  = Form(0),      # 1 = keep each listed caption as one unit (no re-grouping)
    hidden_clips: str = Form(""),  # "0,2": clips (by position) that get no captions in the picture
) -> dict:
    """Caption look + caption list, shared by the live preview and the export."""
    try:
        hidden = {int(index) for index in hidden_clips.split(",") if index.strip()}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid clip list.")
    return {**style, "captions": captions, "edited": edited, "respect": respect, "hidden": hidden}


def _editable_job(job: dict = Depends(_owned_job)) -> tuple:
    """(job, intermediate_path) for a finished job whose caption-free cut is still on disk."""
    inter = job.get('intermediate_path') or ''
    if job.get('status') != 'done' or not os.path.exists(inter):
        raise HTTPException(status_code=409, detail="This edit has expired. Generate it again.")
    if not job.get('intermediate_duration'):
        job['intermediate_duration'] = media_probe.duration(inter, job.get('edited_duration') or 0)
    return job, inter


def _parse_captions(raw: str) -> list[dict]:
    """The caption list clients send: [{"s": seconds, "e": seconds, "t": text}]."""
    invalid = HTTPException(status_code=400, detail="Invalid captions payload.")
    try:
        items = json.loads(raw or "[]")
        if not isinstance(items, list):
            raise invalid
        caps = [{"s": float(c["s"]), "e": float(c["e"]), "t": str(c.get("t", ""))} for c in items]
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise invalid
    if not all(math.isfinite(c["s"]) and math.isfinite(c["e"]) for c in caps):
        raise invalid
    return caps


def _caption_source(job: dict, fields: dict):
    """(transcript, keep_segments, real_durations) the requested captions are built from."""
    caps = _parse_captions(fields['captions'])
    if fields['edited'] and caps:  # user-edited list: already on the edited timeline
        return (edit_render.transcript_from_captions(caps, fields['respect']),
                [(0.0, job['intermediate_duration'] + 1.0)], None)
    return job['transcript_data'], job['clips_to_keep'], job.get('real_durations')


@app.get("/intermediate/{job_id}", summary="The cut, graded, caption-free video (captions are drawn live on top)")
def get_intermediate(editable: tuple = Depends(_editable_job)):
    _, inter = editable
    return FileResponse(inter, media_type="video/mp4")


@app.post("/captions/{job_id}", summary="Caption track for a look: ASS for the live preview + SRT caption list")
def caption_track(fields: dict = Depends(_caption_fields), editable: tuple = Depends(_editable_job)):
    job, inter = editable
    style, preset = build_style(fields)
    transcript, keep, real = _caption_source(job, fields)
    return edit_render.caption_track(transcript, keep, style, preset, inter, real,
                                      job.get('clip_starts'), fields['hidden'])


@app.post("/export/{job_id}", summary="Burn the current captions into the video for download")
def export_video(job_id: str, fields: dict = Depends(_caption_fields), editable: tuple = Depends(_editable_job),
                 user: dict = Depends(auth.get_current_user)):
    job, inter = editable
    style, preset = build_style(fields)
    transcript, keep, real = _caption_source(job, fields)
    track = edit_render.caption_track(transcript, keep, style, preset, inter, real,
                                      job.get('clip_starts'), fields['hidden'])
    key = hashlib.sha256(f"{job_id}\n{track['ass']}".encode("utf-8")).hexdigest()
    with _exports_lock:
        for running_id, running in list(_EXPORTS.items()):
            if (running.get('key') == key and running['status'] == 'processing'
                    and running['owner_id'] == user['id']):
                return {"rid": running_id}      # pressed twice: the same encode
        rid = uuid.uuid4().hex
        tag = rid[:8]
        name = as_mp4(job.get('filename'))
        out_p, ass_p = f"export_{tag}_{name}", f"export_{tag}.ass"
        with open(ass_p, "w", encoding="utf-8") as f:
            f.write(track['ass'])
        entry = _EXPORTS[rid] = {'status': 'processing', 'progress': 2, 'path': out_p, 'ass': ass_p, 'key': key,
                                 'error': None, 'owner_id': user['id'], 'name': name, 'created_at': time.time()}
    has_cues = "Dialogue:" in track['ass']

    def _work():
        st_ = entry
        try:
            if has_cues:
                burn_subtitles(inter, out_p, ass_path=ass_p, job_prefix=tag,
                               progress_cb=lambda p: st_.update(progress=max(2, p)),
                               duration=job['intermediate_duration'])
            else:  # no captions: the cut video is the export
                shutil.copyfile(inter, out_p)
            st_.update(status='done', progress=100)
        except Exception as e:
            st_.update(status='error', error=str(e))
    _export_pool.submit(_work)
    return {"rid": rid}


@app.get("/export-status/{rid}", summary="Progress of an export")
def export_status(rid: str, user: dict = Depends(auth.get_current_user)):
    r = _EXPORTS.get(rid)
    if not r or r['owner_id'] != user['id']:
        raise HTTPException(status_code=404, detail="Not found")
    return {"status": r['status'], "progress": r['progress'], "error": r['error']}


@app.get("/export-result/{rid}", summary="Download a finished export")
def export_result(rid: str, name: str = "", user: dict = Depends(auth.get_current_user)):
    r = _EXPORTS.get(rid)
    if not r or r['owner_id'] != user['id'] or r['status'] != 'done' or not os.path.exists(r['path']):
        raise HTTPException(status_code=404, detail="Not ready")
    # `name` is the project's name: Content-Disposition beats the link's download attribute
    filename = as_mp4(safe_filename(name)) if name else f"edited_{r['name']}"
    return FileResponse(r['path'], media_type="video/mp4", filename=filename)


@app.post("/caption-command/{job_id}", summary="Comments: one line into a caption edit or a timed cut request")
def caption_command(
    job_id:   str,
    comment:  str = Form(...),
    captions: str = Form(...),      # JSON: [{"s":sec,"e":sec,"t":"text"}, ...] — the current on-screen captions
    job:      dict = Depends(_owned_job),
):
    """
    Step 6 (Feedback & Refinement) — free-text version.

    A timed cut request ("cut 0:05 to 0:08") is answered as that request, for the
    caller to resolve against its timeline and send to /feedback/.

    The user writes one line describing a fix ("fix spelling of 'recieve' to
    'receive'", "delete caption 5", "change caption 3 to ..."). comment_parser
    matches it against the current caption list with regex + fuzzy text
    comparison (no external service, no model download) and returns EITHER:
      - a precise edit touching exactly one caption, with every other
        caption byte-for-byte unchanged, or
      - a message explaining what was ambiguous / not understood, plus
        candidate captions to disambiguate when there's more than one match.

    This endpoint only figures out the edit — it does not render anything.
    The caller applies the returned `captions` list the same way a manual
    caption edit is applied: via POST /captions/{job_id} with edited=1.
    """
    caps = _parse_captions(captions)
    timed = parse_cut(comment)
    if timed:
        return timed
    if not caps:
        raise HTTPException(status_code=400, detail="No captions to edit.")
    return parse_command(comment, caps)


# ---------------------------------------------------------------------------
# Caption style from a reference video (Step: "style from reference video")
# Pure OpenCV + Tesseract OCR analysis of the reference clip's own burned-in
# captions -- no AI/LLM/vision model or external API call anywhere in this
# path. See reference_caption_style.py.
# ---------------------------------------------------------------------------
@app.get("/custom-templates", summary="List caption templates created from reference videos")
def list_custom_templates(user: dict = Depends(auth.get_current_user)):
    """The caller's own templates (each user's reference-derived looks stay private)."""
    return templates.list_for(user['id'])


@app.delete("/custom-templates/{key}", summary="Delete one of your reference-video templates")
def delete_custom_template(key: str, user: dict = Depends(auth.get_current_user)):
    if not templates.delete(key, user['id']):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"deleted": key}


@app.post("/extract-reference-style", summary="Analyze a reference video's on-screen captions and create a new template from them")
async def extract_reference_style(
    file: UploadFile = File(...),
    name: str = Form(""),
    user: dict = Depends(auth.get_current_user),
):
    """
    Step: "style from reference video". Upload a clip that already has
    burned-in captions; this scans sampled frames with Tesseract OCR and
    measures the caption's font size, color, vertical position, and how
    many words appear per on-screen caption -- then saves that as a new
    selectable template (font family isn't detected; OCR can't reliably
    tell fonts apart, so it stays Arial/Bold until the user picks one).
    """
    tag = uuid.uuid4().hex[:8]
    tmp_path = f"_refstyle_{tag}{os.path.splitext(safe_filename(file.filename))[1]}"
    try:
        await _save_upload(file, tmp_path)
        result = await asyncio.get_running_loop().run_in_executor(jobs.executor, extract_caption_style, tmp_path)
    finally:
        jobs.discard([tmp_path])

    if not result or result.get("samples", 0) < 3:
        raise HTTPException(
            status_code=422,
            detail="Couldn't reliably read on-screen captions in that video. "
                   "Try a clip where the captions are large, high-contrast, and "
                   "on screen for a few seconds at a time.",
        )

    key = f"ref_{uuid.uuid4().hex[:16]}"
    label = name or os.path.splitext(file.filename or "")[0]
    label = "".join(c for c in label if c.isprintable() and c not in "<>\"'&").strip()[:40].strip() or "Reference Style"

    try:
        await run_in_threadpool(templates.create, key, user["id"], label, result["config"])
    except templates.LimitReached:
        raise HTTPException(status_code=409,
                            detail=f"You have {templates.MAX_PER_USER} templates. Delete one to add another.")

    return {"key": key, "label": label, "config": result["config"], "debug": result["debug"]}


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
        for job_id, job in list(job_store.items())     # a snapshot: workers add and remove jobs
        if job.get('owner_id') == user['id']
    })


@app.post("/caption-preview", summary="Render the real caption look (same engine as the export) as a transparent PNG")
async def caption_preview_png(request: Request, user: dict = Depends(auth.get_current_user)):
    form = await request.form()
    fields = {k: str(v) for k, v in form.items()}
    try:
        png = await run_in_threadpool(caption_preview.render, fields)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview failed: {e}")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})
