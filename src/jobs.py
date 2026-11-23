"""Job state: the in-memory store, the workers that run jobs, and the files jobs own.

Nothing here knows about HTTP, or about how a video is edited.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, TypedDict

import workspace

if TYPE_CHECKING:
    from clip_combiner import Joined
    from edit_render import Cut
    from pipeline import Analysis, EditOptions


class Job(TypedDict, total=False):
    """Every key a job can have. Times are seconds; ranges are (start, end) on the original timeline."""

    # set by register()
    status: str                     # queued | processing | done | error
    step: int                       # 0-8
    step_label: str
    progress: int                   # 0-100
    error: str | None
    elapsed: float | None
    filename: str                   # the upload's safe name
    owner_id: str
    created_at: float
    cancelled: bool                 # the worker stops at its next progress update

    # files in the work directory (see files_of)
    source_path: str                # as uploaded
    input_video_path: str | None    # upright; shared with every change to the clip
    intermediate_path: str          # the edit: cut, framed, graded, caption-free
    piece_paths: list[str]          # the encoded pieces it is made of, or may reuse

    # what a clip's edit was made from
    rotate: int
    options: "EditOptions"
    analysis: "Analysis"
    transcript_data: dict
    language: str                   # ISO 639-1
    style_profile: dict
    reference_used: bool
    caption_look: dict | None       # read off a reference, for Studio to apply
    burned_captions: bool           # the upload came with captions in its picture
    raw_scene_analysis: dict
    filler_cuts: list
    edge_cuts: list
    decisions: list                 # one record per pause
    corrections: list               # the user's CUT/KEEP, in order

    # the result
    cut: "Cut"
    keep_segments: list
    clips_to_keep: list             # what captions map through (a joined video: its whole length)
    real_durations: list | None     # seconds on screen of each kept range
    original_duration: float
    edited_duration: float
    intermediate_duration: float
    evaluation: dict
    filmstrip: list

    # a project's order (a single clip carries its own)
    clip_keys: list[str]            # the client's id of each clip
    is_combine_master: bool
    clip_jobs: list[str]            # immutable, and shared between masters
    transitions: list[str]          # as asked, one per boundary
    transitions_used: list[str]     # with "auto" resolved
    clip_starts: list[float]
    joined: "Joined"


store: dict[str, Job] = {}

# Edits queue here; exports have their own pool, so a download never waits behind two edits.
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="edit")

FILE_KEYS = ("source_path", "input_video_path", "intermediate_path")
STUCK_SECONDS = 6 * 3600


class JobCancelled(Exception):
    """Raised inside a worker at its next progress update once its job was stopped or removed."""


def register(job_id: str, owner_id: str, filename: str, **fields) -> Job:
    """Every job starts here, so each has an owner and a clock."""
    store[job_id] = {
        "status": "queued", "step": 0, "step_label": "Queued", "progress": 0,
        "input_video_path": None, "error": None, "elapsed": None,
        "filename": filename, "owner_id": owner_id, "created_at": time.time(), **fields,
    }
    return store[job_id]


def progress(job_id: str, step: int, label: str, percent: int) -> None:
    job = store.get(job_id)
    if job is None or job.get("cancelled"):
        raise JobCancelled("This job was stopped.")
    job.update(step=step, step_label=label, progress=percent, status="processing")
    print(f"   [Job {job_id}] Step {step}/8 — {label} ({percent}%)")


def finish(job_id: str, fields: dict) -> None:
    job = store.get(job_id)
    if job is None:
        raise JobCancelled("This job was removed.")
    job.update({"status": "done", "step": 8, "step_label": "Complete", "progress": 100, **fields})


def fail(job_id: str, error, label: str = "Failed", **fields) -> None:
    job = store.get(job_id)
    if job is not None:
        job.update(status="error", step_label=label, error=str(error), **fields)


def in_background(job_id: str, work, *args):
    """Run a job's work on the edit pool; whatever escapes it ends the job as an error."""
    def guarded():
        try:
            work(*args)
        except Exception as exc:
            print(f"\n❌ JOB {job_id} CRASHED: {exc}\n")
            fail(job_id, exc)
    return executor.submit(guarded)


def discard(paths) -> None:
    """Delete these files, if they are there."""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                print(f"   🗑️  Deleted: {path}")
        except OSError as exc:
            print(f"   ⚠️  Could not delete {path}: {exc}")


def files_of(job: Job) -> list[str]:
    return [path for path in [*(job.get(key) for key in FILE_KEYS), *job.get("piece_paths", [])] if path]


def referenced_files(exclude=frozenset()) -> set[str]:
    """Files some live job still points at (changed clips share uploads and pieces)."""
    return {os.path.basename(path) for job_id, job in list(store.items()) if job_id not in exclude
            for path in files_of(job)}


def _clips_in_use(exclude=frozenset()) -> set[str]:
    return {clip for job_id, job in list(store.items()) if job_id not in exclude
            for clip in job.get("clip_jobs", [])}


def touch(job_id: str) -> None:
    """It is being built on: its clock starts again."""
    if job_id in store:
        store[job_id]["created_at"] = time.time()


def delete(job_id: str, job: Job) -> None:
    """Forget a job and delete every file of it that no other job still uses."""
    in_use = _clips_in_use({job_id})        # a master takes the clips no other master plays
    ids = [job_id, *dict.fromkeys(c for c in job.get("clip_jobs", []) if c not in in_use)]
    shared = referenced_files(set(ids))
    for jid in ids:
        record = job if jid == job_id else store.get(jid, {})
        # by recorded path too: shared files carry the prefix of the job that made them
        discard([path for path in files_of(record) if os.path.basename(path) not in shared])
        workspace.remove_job_files(jid[:8], keep=shared)
        store.pop(jid, None)
    print(f"   🧹 Job {job_id} fully cleaned up")


def expire(now: float) -> None:
    """Delete jobs past the TTL. An unfinished job gets the longer of the TTL and STUCK_SECONDS."""
    playing = _clips_in_use()
    for job_id in list(store):
        job = store.get(job_id)
        if job is None or job_id in playing:    # gone, or goes with the master that plays it
            continue
        age = now - job.setdefault("created_at", now)
        unfinished = job.get("status") in ("queued", "processing")
        if age <= (max(workspace.TTL_SECONDS, STUCK_SECONDS) if unfinished else workspace.TTL_SECONDS):
            continue
        if unfinished:      # nothing legitimate runs this long
            job.update(status="error", step_label="Timed out", error="This job took too long and was stopped.")
        delete(job_id, job)
