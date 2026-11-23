"""What a requested edit needs: which clips stay, which re-render, which are cut again.

A project is an ordered list of clips. A finished clip job is never changed: a change makes a new
job that reuses what it can, so other edits of the project, and Undo, stay valid.
Nothing here knows about HTTP.
"""
import dataclasses
import json
import os
import re
import uuid
from dataclasses import dataclass

import cut_list
import jobs
import pipeline

_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


class PlanError(ValueError):
    pass


class Expired(PlanError):
    """A clip to build on is gone: the client can send its file again."""


@dataclass
class Clip:
    key: str
    job: str | None                 # the finished clip it builds on
    upload: int | None              # or the index of the file sent with the request
    rotate: int
    frame: dict                     # {format, mode, cx, cy, zoom}
    transition: str                 # into the next clip
    # filled in while the request is received
    previous: dict | None = None
    name: str = ""
    job_id: str = ""
    source_path: str = ""
    input_path: str = ""


def parse_clips(raw: str, uploads: int, defaults: dict) -> list[Clip]:
    """The request's clip list. With none, every upload is a clip with the request's settings."""
    if not raw:
        entries = [{"upload": index} for index in range(uploads)]
    else:
        try:
            entries = json.loads(raw)
        except (ValueError, RecursionError):
            raise PlanError("Invalid clip list.")
    if not isinstance(entries, list) or not entries:
        raise PlanError("Send at least one clip.")
    clips = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise PlanError("Invalid clip list.")
        key, job, upload = str(entry.get("key", f"c{index}")), entry.get("job"), entry.get("upload")
        rotate, frame = entry.get("rotate", defaults["rotate"]), entry.get("frame", defaults["frame"])
        transition = entry.get("transition", defaults["transition"])
        known_upload = type(upload) is int and 0 <= upload < uploads
        known_job = isinstance(job, str) and bool(job)
        if (not _KEY.fullmatch(key) or known_upload == known_job or type(rotate) is not int
                or not isinstance(frame, dict) or not isinstance(transition, str)):
            raise PlanError("Invalid clip list.")
        clips.append(Clip(key, job if known_job else None, upload if known_upload else None,
                          rotate % 360, frame, transition))
    if len({clip.key for clip in clips}) != len(clips):
        raise PlanError("Two clips have the same key.")
    return clips


def attach_previous(clips: list[Clip], find) -> None:
    """Look up what each clip builds on; ``find`` returns only the caller's own jobs."""
    for clip in clips:
        if clip.job is None:
            continue
        job = find(clip.job)
        if job.get("status") != "done" or job.get("clip_jobs"):
            raise PlanError("A clip can only build on a finished clip.")
        needed = (job.get("source_path"), job.get("input_video_path"), job.get("intermediate_path"))
        if not all(path and os.path.exists(path) for path in needed) or not job.get("cut"):
            raise Expired("The original upload for this video has expired. Upload it again to change it.")
        if not all(os.path.exists(piece.path) for piece in job["cut"].pieces):
            raise Expired("Part of this edit has expired. Upload it again to change it.")
        jobs.touch(clip.job)
        clip.previous, clip.name, clip.source_path = job, job["filename"], job["source_path"]
        if clip.rotate == job.get("rotate", 0):
            clip.input_path = job["input_video_path"]


@dataclass
class Submission:
    job_id: str
    work: tuple | None      # (function, *arguments) for the worker; None when it is already done


def _register_clip(owner: str, clip: Clip, options: pipeline.EditOptions, keep=None, corrections=None) -> str:
    previous = clip.previous or {}
    fields = {
        "input_video_path": clip.input_path, "source_path": clip.source_path, "rotate": clip.rotate,
        "clip_keys": [clip.key], "options": options,
        "corrections": previous.get("corrections", []) if corrections is None else corrections,
        "piece_paths": list(previous.get("piece_paths", [])),     # held until this job has its own
    }
    if keep is not None:
        fields["keep_segments"] = keep
    jobs.register(clip.job_id, owner, clip.name, **fields)
    return clip.job_id


def _submission(owner: str, keys: list[str], ids: list[str], todo: dict, options: pipeline.EditOptions,
                transitions: list[str], seconds: float, base: dict | None) -> Submission:
    if len(ids) == 1:
        if not todo:
            return Submission(ids[0], None)
        clip_options, previous = todo[ids[0]]
        return Submission(ids[0], (pipeline.run_edit, ids[0], clip_options, None, previous))
    master = uuid.uuid4().hex
    jobs.register(master, owner, "combined_output.mp4", is_combine_master=True, clip_jobs=list(ids),
                  clip_keys=list(keys), transitions=list(transitions), transition_seconds=seconds,
                  piece_paths=list((base or {}).get("piece_paths", [])))
    return Submission(master, (pipeline.run_multi_clip, master, todo, options, seconds, base))


def submit_edit(owner: str, clips: list[Clip], options: pipeline.EditOptions, frames: list, seconds: float,
                base: dict | None = None) -> Submission:
    """``frames`` is each clip's checked frame setting; every clip's files are in place."""
    built = [clip.previous for clip in clips if clip.previous]
    turned = any(clip.previous.get("rotate", 0) != clip.rotate for clip in clips if clip.previous)
    # turning a clip can change the project's shape: then the first clip's wins a tie, not the old shape
    current = built[0]["cut"].canvas if built and not turned else None
    canvas = pipeline.project_canvas([(c.input_path, f) for c, f in zip(clips, frames)], current)
    ids, todo = [], {}
    for clip, frame in zip(clips, frames):
        wanted = dataclasses.replace(options, frame=frame, canvas=canvas)
        previous, keep = clip.previous, None
        if previous and wanted.same_decisions(previous["options"]):
            if (previous["options"].same_picture(wanted) and previous["input_video_path"] == clip.input_path
                    and clip.key in previous.get("clip_keys", [clip.key])):
                ids.append(clip.job)
                continue
            keep = previous["keep_segments"]
        ids.append(_register_clip(owner, clip, wanted, keep))
        todo[clip.job_id] = (wanted, previous)
    return _submission(owner, [clip.key for clip in clips], ids, todo, options,
                       [clip.transition for clip in clips[:-1]], seconds, base)


def clips_of(job_id: str, job: dict) -> list[tuple[str, str, dict]]:
    """(key, job id, job) of every clip in a finished edit, in playing order."""
    ids = job.get("clip_jobs") or [job_id]
    keys = job.get("clip_keys") or [f"c{index}" for index in range(len(ids))]
    return [(key, clip_id, jobs.store.get(clip_id, {}) if job.get("clip_jobs") else job)
            for key, clip_id in zip(keys, ids)]


def submit_correction(owner: str, job_id: str, job: dict, start: float, end: float, kind: str) -> Submission:
    """One timestamp correction, in seconds on the project's original timeline."""
    parts, offset = [], 0.0
    members = clips_of(job_id, job)
    for index, (key, clip_id, record) in enumerate(members):
        length = record.get("original_duration", 0)
        first, last = max(start, offset), min(end, offset + length)
        if len(members) == 1:       # cut_list says what is wrong with the range
            first, last = start, end
        if last - first > 1e-3 or len(members) == 1:
            parts.append((index, first - offset, last - offset))
        offset += length
    if not parts:
        raise PlanError("That range is outside the video.")

    changes = []
    for index, first, last in parts:
        record = members[index][2]
        try:
            keep = cut_list.apply_correction(record.get("keep_segments", []), first, last, kind,
                                             record.get("original_duration", 0))
        except ValueError as exc:
            hint = " To drop a whole clip, remove it in Media." if len(members) > 1 and "whole video" in str(exc) else ""
            raise PlanError(f"{exc}{hint}")
        changes.append((index, first, last, keep))

    clips = [Clip(key, clip_id, None, record.get("rotate", 0), {}, "auto") for key, clip_id, record in members]
    attach_previous(clips, lambda clip_id: jobs.store.get(clip_id, {}))
    ids, todo = [clip_id for _, clip_id, _ in members], {}
    for index, first, last, keep in changes:
        clip = clips[index]
        options = dataclasses.replace(clip.previous["options"], reference_link="")
        clip.job_id = uuid.uuid4().hex
        corrections = [*clip.previous.get("corrections", []),
                       {"start": first, "end": last, "correction": kind.upper()}]
        ids[index] = _register_clip(owner, clip, options, keep, corrections)
        todo[clip.job_id] = (options, clip.previous)
    jobs.touch(job_id)
    any_options = next(iter(todo.values()))[0]
    return _submission(owner, [clip.key for clip in clips], ids, todo, any_options, job.get("transitions", []),
                       job.get("transition_seconds", 0.5), job if job.get("clip_jobs") else None)
