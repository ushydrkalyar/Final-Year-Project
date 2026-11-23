"""The edit pipeline through HTTP and real ffmpeg. Whisper and yt-dlp are stubbed."""
import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid

import pytest

from conftest import fake_transcript

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def engine(app_module):
    """The module that runs the pipeline."""
    return app_module.pipeline


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    streams = {s["codec_type"]: s for s in info["streams"]}
    return streams, float(info["format"]["duration"])


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")

    def make(name, size="320x240", fps=25, rate=48000, channels=2, tone=440, audio=True):
        path = root / name
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d=6"]
        if audio:
            # sound only where the stub transcript has words: a gap as loud as speech is never cut
            cmd += ["-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate={rate}:duration=6,"
                    "volume='between(t,0.4,2.0)+between(t,3.8,5.3)':eval=frame",
                    "-ac", str(channels), "-c:a", "aac"]
        subprocess.run(cmd + ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
        return path

    wav = root / "ref.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=6", "-ar", "16000", str(wav)],
                   check=True)
    junk = root / "junk.mp4"
    junk.write_bytes(b"this is not a video")
    return {"clip": make("clip.mp4"), "other": make("other.mp4", "240x320", 50, 44100, 1, 880),
            "silent": make("silent.mp4", audio=False), "junk": junk, "wav": wav}


@pytest.fixture
def user(make_user):
    return make_user()[0]


def submit(client, *files, names=None, **fields):
    names = names or [f.name for f in files]
    return client.post("/edit-video/", data={k: str(v) for k, v in {"cv_analysis": "0", **fields}.items()},
                       files=[("video_file", (n, f.read_bytes(), "video/mp4")) for f, n in zip(files, names)])


def finish(client, job, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        state = client.get(f"/status/{job}").json()
        if state.get("status") in ("done", "error"):
            return state
        time.sleep(0.2)
    pytest.fail(f"job {job} did not finish: {state}")


def edit(client, *files, **fields):
    r = submit(client, *files, **fields)
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    state = finish(client, job)
    assert state["status"] == "done", state
    return job


def export(client, job, **fields):
    r = client.post(f"/export/{job}", data=fields)
    assert r.status_code == 200, r.text
    rid = r.json()["rid"]
    end = time.time() + 60
    while time.time() < end:
        if client.get(f"/export-status/{rid}").json()["status"] != "processing":
            break
        time.sleep(0.2)
    return client.get(f"/export-result/{rid}")


def work_files(app_module):
    return set(os.listdir(app_module.workspace.WORK_DIR))


# ---- the main journey
DONE_KEYS = {"status", "step", "step_label", "progress", "input_video_path", "source_path", "elapsed", "evaluation",
             "style_profile", "reference_used", "raw_scene_analysis", "language", "keep_segments", "filler_cuts",
             "edge_cuts", "decisions", "original_duration", "edited_duration", "intermediate_path", "options",
             "analysis", "cut", "piece_paths", "filmstrip", "transcript_data", "clips_to_keep", "real_durations",
             "filename", "owner_id"}


def test_single_clip_journey(app_module, user, media, tmp_path):
    job = edit(user, media["clip"])
    assert DONE_KEYS <= set(app_module.job_store[job])
    timeline = user.get(f"/timeline/{job}").json()
    assert 0 < timeline["edited_duration"] < timeline["original_duration"]
    assert user.get(f"/report/{job}").json()["report"]["quality_score"]["grade"]
    assert user.get(f"/intermediate/{job}").status_code == 200
    track = user.post(f"/captions/{job}", data={}).json()
    assert "Dialogue:" in track["ass"] and "Hello" in track["srt"]
    result = export(user, job)
    assert result.status_code == 200
    (tmp_path / "out.mp4").write_bytes(result.content)
    streams, duration = ffprobe(tmp_path / "out.mp4")
    assert {"video", "audio"} <= set(streams) and abs(duration - timeline["edited_duration"]) < 0.5


def test_owner_checks(app_module, user, make_user, new_client):
    job = uuid.uuid4().hex
    app_module.job_store[job] = {"owner_id": "someone-else", "status": "done"}
    try:
        for path in ("status", "timeline", "report", "intermediate"):
            assert user.get(f"/{path}/{job}").status_code == 403, path
            assert new_client().get(f"/{path}/{job}").status_code == 401, path
    finally:
        app_module.job_store.pop(job)


def test_corrections_chain(user, media):
    job = edit(user, media["clip"])
    gap = next(s for s in user.get(f"/timeline/{job}").json()["original_timeline"] if not s["in_edit"])
    r = user.post("/feedback/", data={"job_id": job, "timestamp_start": gap["start"], "timestamp_end": gap["end"],
                                      "correction": "KEEP"})
    kept = r.json()["job_id"]
    assert finish(user, kept)["status"] == "done"
    longer = user.get(f"/timeline/{kept}").json()["edited_duration"]
    assert longer > user.get(f"/timeline/{job}").json()["edited_duration"]
    r = user.post("/feedback/", data={"job_id": kept, "timestamp_start": 3.85, "timestamp_end": 4.22, "correction": "CUT"})
    cut = r.json()["job_id"]
    assert finish(user, cut)["status"] == "done"
    assert user.get(f"/timeline/{cut}").json()["edited_duration"] < longer
    srt = user.post(f"/captions/{cut}", data={}).json()["srt"]
    assert "CutSmart" in srt and "This" not in srt


def test_NEW1_correction_does_not_inherit_the_parents_cached_length(app_module, user, media):
    job = edit(user, media["clip"])
    user.post(f"/captions/{job}", data={})      # caches the parent's length
    before = app_module.job_store[job]["intermediate_duration"]
    r = user.post("/feedback/", data={"job_id": job, "timestamp_start": 0, "timestamp_end": 6, "correction": "KEEP"})
    child = r.json()["job_id"]
    finish(user, child)
    user.post(f"/captions/{child}", data={})
    assert app_module.job_store[child]["intermediate_duration"] > before + 0.5


def clip_of(timeline, index, **changes):
    clip = timeline["clips"][index]
    return {"key": clip["key"], "job": clip["job_id"], "rotate": clip["rotate"], "transition": "hard_cut",
            "frame": {}, **changes}


def change(client, base, clips, *uploads, **fields):
    r = client.post("/edit-video/", data={k: str(v) for k, v in {"cv_analysis": "0", "base_job": base,
                                                                  "clips": json.dumps(clips), **fields}.items()},
                    files=[("video_file", (f.name, f.read_bytes(), "video/mp4")) for f in uploads] or None)
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    state = finish(client, job)
    assert state["status"] == "done", state
    return job, client.get(f"/timeline/{job}").json()


@pytest.fixture
def work_done(app_module, monkeypatch):
    """What a request really does: transcriptions, and video pieces encoded."""
    done = {"transcribed": 0, "encoded": 0}
    eng = engine(app_module)
    transcribe, encode = eng.transcribe_audio, eng.edit_render.segment_render.encode_range

    def transcribed(*a, **k):
        done["transcribed"] += 1
        return transcribe(*a, **k)

    def encoded(*a, **k):
        done["encoded"] += 1
        return encode(*a, **k)
    monkeypatch.setattr(eng, "transcribe_audio", transcribed)
    monkeypatch.setattr(eng.edit_render.segment_render, "encode_range", encoded)
    return lambda: dict(done), lambda: done.update(transcribed=0, encoded=0)


def test_multi_clip(user, media):
    job = edit(user, media["clip"], media["clip"], transition_type="hard_cut")
    assert "Dialogue:" in user.post(f"/captions/{job}", data={}).json()["ass"]
    timeline = user.get(f"/timeline/{job}").json()
    assert timeline["original_duration"] == pytest.approx(12, abs=0.5)
    assert [c["original_start"] for c in timeline["clips"]] == [0, pytest.approx(6, abs=0.25)]
    assert timeline["clips"][1]["edited_end"] == pytest.approx(timeline["edited_duration"], abs=0.01)


def test_S03_add_reorder_remove_make_only_what_changed(user, media, work_done):
    counts, reset = work_done
    one = edit(user, media["clip"])
    first = user.get(f"/timeline/{one}").json()
    reset()
    two, timeline = change(user, one, [clip_of(first, 0), {"key": "added", "upload": 0, "transition": "hard_cut"}],
                           media["clip"])
    assert counts()["transcribed"] == 1                                     # only the new clip
    assert timeline["clips"][0]["job_id"] != "" and [c["key"] for c in timeline["clips"]] == ["c0", "added"]

    reset()
    flipped, after = change(user, two, [clip_of(timeline, 1), clip_of(timeline, 0)])
    assert counts() == {"transcribed": 0, "encoded": 0}
    assert [c["job_id"] for c in after["clips"]] == [c["job_id"] for c in reversed(timeline["clips"])]

    reset()
    _, alone = change(user, flipped, [clip_of(after, 0)])
    assert counts()["transcribed"] == 0 and [c["key"] for c in alone["clips"]] == ["added"]


def test_S03_transitions_are_per_boundary_and_length_is_true(user, media):
    job = edit(user, media["clip"], media["clip"], media["clip"], transition_type="hard_cut")
    before = user.get(f"/timeline/{job}").json()
    clips = [clip_of(before, 0, transition="crossfade"), clip_of(before, 1, transition="fade"), clip_of(before, 2)]
    blended, after = change(user, job, clips, transition_duration=0.4)
    assert [c["transition"] for c in after["clips"]] == ["crossfade", "fade", None]
    assert before["edited_duration"] - after["edited_duration"] == pytest.approx(0.4, abs=0.041)
    assert after["clips"][1]["edited_start"] < after["clips"][0]["edited_end"]
    body = user.get(f"/intermediate/{blended}")
    assert body.status_code == 200
    path = __import__("app").job_store[blended]["intermediate_path"]
    streams, length = ffprobe(path)
    assert length == pytest.approx(after["edited_duration"], abs=0.05)
    assert float(streams["audio"]["duration"]) == pytest.approx(float(streams["video"]["duration"]), abs=0.05)


def test_S03_corrections_reach_any_clip_and_leave_the_others(user, media):
    job = edit(user, media["clip"], media["clip"], transition_type="hard_cut")
    before = user.get(f"/timeline/{job}").json()
    gap = next(s for s in before["original_timeline"] if not s["in_edit"] and s["clip"] == 1)
    r = user.post("/feedback/", data={"job_id": job, "timestamp_start": gap["start"], "timestamp_end": gap["end"],
                                      "correction": "KEEP"})
    assert r.status_code == 200, r.text
    child = r.json()["job_id"]
    assert finish(user, child)["status"] == "done"
    after = user.get(f"/timeline/{child}").json()
    assert after["clips"][0]["job_id"] == before["clips"][0]["job_id"]
    assert after["clips"][1]["job_id"] != before["clips"][1]["job_id"]
    assert after["edited_duration"] > before["edited_duration"]
    assert user.get(f"/timeline/{job}").json() == before        # Undo goes back to it


def test_S03_a_clip_is_only_ever_the_callers_own(user, make_user, media):
    job = edit(user, media["clip"])
    stranger = make_user()[0]
    r = stranger.post("/edit-video/", data={"clips": json.dumps([{"key": "a", "job": job}])})
    assert r.status_code == 403


@pytest.mark.parametrize("clips", ["not json", "[]", "{}", '[{"key":"a"}]', '[{"key":"a","upload":4}]',
                                   '[{"key":"<b>","upload":0}]', '[{"key":"a","upload":0,"job":"x"}]',
                                   '[{"key":"a","upload":0,"transition":"sparkle"}]',
                                   '[{"key":"a","upload":0},{"key":"a","upload":0}]'])
def test_S03_bad_clip_lists(user, media, clips):
    assert submit(user, media["junk"], clips=clips).status_code == 400


def test_S03_clips_outlive_the_master_that_made_them(app_module, user, media):
    first = edit(user, media["clip"], media["clip"], transition_type="hard_cut")
    timeline = user.get(f"/timeline/{first}").json()
    second, _ = change(user, first, [clip_of(timeline, 1), clip_of(timeline, 0)])
    app_module.jobs.delete(first, app_module.job_store[first])
    for path in [piece.path for piece in app_module.job_store[second]["joined"].pieces]:
        assert os.path.exists(path)
    assert user.post(f"/captions/{second}", data={}).status_code == 200
    app_module.jobs.delete(second, app_module.job_store[second])
    assert not any(clip["job_id"] in app_module.job_store for clip in timeline["clips"])


def test_changing_the_picture_keeps_the_users_cuts(user, media, work_done):
    counts, reset = work_done
    job = edit(user, media["clip"])
    cut = user.post("/feedback/", data={"job_id": job, "timestamp_start": 3.85, "timestamp_end": 4.22,
                                        "correction": "CUT"}).json()["job_id"]
    finish(user, cut)
    before = user.get(f"/timeline/{cut}").json()
    reset()
    _, graded = change(user, cut, [clip_of(before, 0)], color_grade="cinematic")
    assert counts()["transcribed"] == 0
    assert graded["original_timeline"] == before["original_timeline"]
    _, faster = change(user, cut, [clip_of(before, 0)], style_preset="aggressive")
    assert any(not s["in_edit"] and s["start"] <= 4.0 <= s["end"] for s in faster["original_timeline"])


def test_configurable_thresholds(app_module, user, media):
    job = edit(user, media["clip"], max_pause=2.2, compress_pause=0.9)
    profile = app_module.job_store[job]["style_profile"]
    assert (profile["max_duration"], profile["compress_duration"]) == (2.2, 0.9)


@pytest.mark.parametrize("fields", [{"max_pause": 0.1}, {"max_pause": "nan"}, {"compress_pause": 9},
                                    {"max_pause": 0.5, "compress_pause": 1.0}])
def test_bad_thresholds(user, media, fields):
    assert submit(user, media["junk"], **fields).status_code == 400


@pytest.mark.parametrize("kind", ["junk", "silent"])
def test_unusable_uploads_are_rejected(app_module, user, media, kind):
    before = work_files(app_module)
    assert submit(user, media[kind]).status_code == 422
    assert work_files(app_module) == before


def test_rotate_90(app_module, user, media):
    job = edit(user, media["clip"], rotate=90)
    video = ffprobe(app_module.job_store[job]["intermediate_path"])[0]["video"]
    assert (video["width"], video["height"]) == (240, 320)


# ---- L-06
@pytest.mark.parametrize("slow", ["probe", "rotate"])
def test_L06_upload_work_does_not_block_other_requests(app_module, user, media, monkeypatch, slow):
    import httpx
    from fastapi import HTTPException

    def stall(*args, **kwargs):
        time.sleep(1.5)
        raise HTTPException(status_code=422, detail="stop here")
    monkeypatch.setattr(app_module, "_reject_unusable", stall if slow == "probe" else lambda uploads: None)
    if slow == "rotate":
        monkeypatch.setattr(app_module, "_upright", stall)

    async def scenario():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000",
                                     cookies=dict(user.cookies)) as client:
            started = time.perf_counter()
            upload = asyncio.create_task(client.post(
                "/edit-video/", data={"rotate": "90"},
                files={"video_file": ("a.mp4", media["clip"].read_bytes(), "video/mp4")}))
            await asyncio.sleep(0.4)
            assert (await client.get("/auth/me")).status_code == 200
            answered = time.perf_counter() - started
            assert (await upload).status_code == 422
            return answered
    assert asyncio.run(scenario()) < 1.2


# ---- L-16
@pytest.mark.parametrize("name,download", [
    ("it's \"my\" clip#1.MOV", "edited_it_s _my_ clip_1.mp4"),
    ("plain.mov", "edited_plain.mp4"),
])
def test_L16_odd_names(app_module, user, media, name, download):
    r = submit(user, media["clip"], names=[name])
    job = r.json()["job_id"]
    assert not set(app_module.job_store[job]["input_video_path"]) & set("'\"#")
    assert finish(user, job)["status"] == "done"
    header = export(user, job).headers["content-disposition"]
    assert download in header.replace("%20", " ")


def test_L16_non_latin_names_are_kept_and_fit_the_filesystem():
    from filenames import safe_filename
    assert safe_filename("मेरी वीडियो.mp4") == "मेरी वीडियो.mp4"
    assert safe_filename("میری ویڈیو.mov") == "میری ویڈیو.mov"
    assert len(safe_filename("ویڈیو" * 100 + ".mp4").encode()) <= 130
    assert safe_filename(None) == "video.mp4"
    assert "/" not in safe_filename("../../etc/passwd")


def test_L16_join_survives_an_apostrophe(media, tmp_path, monkeypatch):
    import edit_render
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "it's here"
    folder.mkdir()
    source = shutil.copy(media["clip"], folder / "it's a.mp4")
    monkeypatch.chdir(folder)
    made = edit_render.cut(str(source), [(0, 2), (3, 6)], "aaaaaaaa")
    assert ffprobe(made.path)[1] == pytest.approx(5, abs=0.1)


# ---- L-09
def test_L09_oversize_second_clip_leaves_nothing(app_module, user, media, monkeypatch, tmp_path):
    small = tmp_path / "small.mp4"
    small.write_bytes(b"x" * 1000)
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 2000)
    before = work_files(app_module)
    assert submit(user, small, media["clip"]).status_code == 413
    assert user.get("/jobs/").json() == {}
    assert work_files(app_module) == before


def test_L09_rotate_failure_leaves_nothing(app_module, user, media, monkeypatch):
    from fastapi import HTTPException
    seen = []

    def upright(path, degrees, job_id):
        seen.append(path)
        if len(seen) == 2:
            raise HTTPException(status_code=422, detail="Couldn't rotate this video.")
        return path
    monkeypatch.setattr(app_module, "_upright", upright)
    before = work_files(app_module)
    assert submit(user, media["clip"], media["clip"], rotate=90).status_code == 422
    assert user.get("/jobs/").json() == {}
    assert work_files(app_module) == before


def test_L09_sweeper_expires_stuck_jobs(app_module):
    old, fresh = uuid.uuid4().hex, uuid.uuid4().hex
    now = time.time()
    app_module.job_store[old] = {"status": "processing", "created_at": now - 8 * 24 * 3600, "owner_id": "x"}
    app_module.job_store[fresh] = {"status": "processing", "created_at": now - 3600, "owner_id": "x"}
    try:
        app_module._sweep_expired_jobs()
        assert old not in app_module.job_store and fresh in app_module.job_store
    finally:
        app_module.job_store.pop(old, None)
        app_module.job_store.pop(fresh, None)


def test_L09_jobs_are_stamped_when_registered(app_module, user, media):
    started = time.time()
    job = edit(user, media["clip"])
    assert app_module.job_store[job].get("created_at", 0) >= started


def test_shared_upload_survives_while_a_correction_uses_it(app_module, user, media):
    job = edit(user, media["clip"])
    child = user.post("/feedback/", data={"job_id": job, "timestamp_start": 0, "timestamp_end": 6,
                                          "correction": "KEEP"}).json()["job_id"]
    finish(user, child)
    raw = app_module.job_store[job]["input_video_path"]
    app_module.jobs.delete(job, app_module.job_store[job])
    assert os.path.exists(raw)
    app_module.jobs.delete(child, app_module.job_store[child])
    assert not os.path.exists(raw)


# ---- L-10
def test_L10_too_many_clips(user, media):
    r = submit(user, *[media["junk"]] * 11)
    assert r.status_code == 400 and "10" in r.json()["detail"]


def test_L10_dead_clip_thread_fails_the_master(app_module, user, media, monkeypatch):
    eng = engine(app_module)
    real, calls = eng.run_edit, []

    def run(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("clip thread died")
        return real(*args, **kwargs)
    monkeypatch.setattr(eng, "run_edit", run)
    master = submit(user, media["clip"], media["clip"]).json()["job_id"]
    try:
        assert finish(user, master, timeout=20)["status"] == "error"
    finally:
        job = app_module.job_store.get(master, {})
        for clip in job.get("clip_jobs", []):       # lets the old polling loop end
            if app_module.job_store.get(clip, {}).get("status") not in ("done", "error"):
                app_module.job_store[clip]["status"] = "error"
        finish(user, master, timeout=60)


def test_L10_at_most_three_clips_run_at_once(app_module, user, media, monkeypatch):
    eng = engine(app_module)
    lock, running, peak = threading.Lock(), [0], [0]

    def run(job_id, *args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.6)
        with lock:
            running[0] -= 1
        app_module.job_store[job_id].update(status="error", error="stop here")
    monkeypatch.setattr(eng, "run_edit", run)
    master = submit(user, *[media["clip"]] * 5).json()["job_id"]
    finish(user, master)
    time.sleep(1)
    assert peak[0] <= 3


def test_L10_master_crash_becomes_an_error(app_module, user, media, monkeypatch):
    eng = engine(app_module)

    def crash(*args, **kwargs):
        raise RuntimeError("master thread died")
    monkeypatch.setattr(eng, "run_multi_clip", crash)
    job = submit(user, media["clip"], media["clip"]).json()["job_id"]
    assert finish(user, job, timeout=10)["status"] == "error"


# ---- references: L-11, L-19, Q9
def test_L11_failed_prefetch_downloads_once(app_module, user, media, monkeypatch):
    calls = []
    monkeypatch.setattr(engine(app_module), "download_youtube_audio", lambda *a, **k: calls.append(a) and None)
    job = edit(user, media["clip"], media["clip"], reference_link="https://invalid.invalid/x",
               style_preset="aggressive")
    assert len(calls) == 1
    assert app_module.job_store[job]["style_profile"]["max_duration"] == 0.5


def test_L19_failed_reference_uses_the_chosen_preset(app_module, user, media, monkeypatch):
    monkeypatch.setattr(engine(app_module), "download_youtube_audio", lambda *a, **k: None)
    job = edit(user, media["clip"], reference_link="https://invalid.invalid/x", style_preset="aggressive")
    assert app_module.job_store[job]["style_profile"]["max_duration"] == 0.5


@pytest.fixture
def reference(app_module, media, monkeypatch):
    videos = []

    def audio(url, out):
        shutil.copy(media["wav"], out)
        return out
    monkeypatch.setattr(engine(app_module), "download_youtube_audio", audio)
    monkeypatch.setattr(engine(app_module), "download_youtube_video", lambda *a, **k: videos.append(a) and None)
    return videos


def test_L19_cv_switch_skips_the_video_download(user, media, reference):
    edit(user, media["clip"], reference_link="https://example.com/ref", cv_analysis="0")
    assert reference == []


LOOK = {"font": "Arial", "size": 40, "color": "#FFD400", "y": 70}


@pytest.mark.parametrize("clips", [1, 2])
def test_reference_match_reuses_speech_and_reports_the_caption_look(app_module, user, media, reference,
                                                                    monkeypatch, work_done, clips):
    counts, reset = work_done
    job = edit(user, *[media["clip"]] * clips, transition_type="hard_cut")
    before = user.get(f"/timeline/{job}").json()
    monkeypatch.setattr(engine(app_module), "download_youtube_video",
                        lambda url, out: str(shutil.copy(media["clip"], out)))
    monkeypatch.setattr(engine(app_module), "extract_caption_style", lambda path: {"samples": 5, "config": LOOK})
    reset()
    _, after = change(user, job, [clip_of(before, i) for i in range(clips)],
                      reference_link="https://example.com/ref", require_reference=1, copy_caption_style=1)
    assert counts()["transcribed"] == 1                 # the reference, once
    assert after["reference_used"] and after["caption_look"] == LOOK


@pytest.mark.parametrize("clips", [1, 2])
def test_reference_match_fails_rather_than_pretending(app_module, user, media, monkeypatch, clips):
    job = edit(user, *[media["clip"]] * clips, transition_type="hard_cut")
    before = user.get(f"/timeline/{job}").json()
    monkeypatch.setattr(engine(app_module), "download_youtube_audio", lambda *a, **k: None)
    r = user.post("/edit-video/", data={"cv_analysis": "0", "require_reference": "1", "reference_link": "https://x.invalid/v",
                                        "clips": json.dumps([clip_of(before, i) for i in range(clips)])})
    state = finish(user, r.json()["job_id"])
    assert state["status"] == "error" and "reference" in state["error"]


# ---- L-17
def test_L17_job_list_survives_concurrent_inserts(app_module, make_user):
    client, me = make_user()
    added = []

    class Racing(dict):
        def __getitem__(self, key):
            if key == "status" and not added:
                added.append(uuid.uuid4().hex)
                app_module.job_store[added[0]] = {"owner_id": "x", "status": "done"}
            return super().__getitem__(key)
    job = uuid.uuid4().hex
    app_module.job_store[job] = Racing(owner_id=me["id"], status="done", progress=100, step_label="Complete")
    try:
        assert client.get("/jobs/").status_code == 200
    finally:
        for key in [job, *added]:
            app_module.job_store.pop(key, None)


def test_L17_double_export_is_one_encode(app_module, user, media, monkeypatch):
    job = edit(user, media["clip"])
    burns = []

    def burn(inter, out, **kwargs):
        burns.append(out)
        time.sleep(1.0)
        shutil.copy(inter, out)
    monkeypatch.setattr(app_module, "burn_subtitles", burn)
    rids = {user.post(f"/export/{job}", data={}).json()["rid"] for _ in range(2)}
    other = user.post(f"/export/{job}", data={"words_per_caption": 1}).json()["rid"]
    time.sleep(3.5)
    assert len(rids) == 1 and other not in rids
    assert len(burns) == 2


def test_L17_old_exports_are_pruned(app_module):
    rid = uuid.uuid4().hex
    app_module._EXPORTS[rid] = {"status": "done", "progress": 100, "path": "gone.mp4", "ass": "gone.ass",
                                "owner_id": "x", "created_at": time.time() - 8 * 24 * 3600}
    app_module._sweep_expired_jobs()
    assert rid not in app_module._EXPORTS


# ---- L-20
def test_L20_correction_counts_with_the_jobs_language(app_module, user, media, monkeypatch):
    spoken = fake_transcript([("مرحبا", .5, .9), ("اممم", .95, 1.3), ("كيف", 1.35, 1.9), ("الحال.", 3.9, 4.5)])
    spoken["language"] = "ar"
    monkeypatch.setattr(engine(app_module), "transcribe_audio", lambda *a, **k: spoken)
    job = edit(user, media["clip"])
    before = user.get(f"/report/{job}").json()["report"]["fillers"]["total_filler_words_detected"]
    child = user.post("/feedback/", data={"job_id": job, "timestamp_start": 0, "timestamp_end": 6,
                                          "correction": "KEEP"}).json()["job_id"]
    finish(user, child)
    after = user.get(f"/report/{child}").json()["report"]["fillers"]["total_filler_words_detected"]
    assert before == after == 1


# ---- L-21
@pytest.mark.parametrize("field,value", [("style_preset", "turbo"), ("transition_type", "sparkle"),
                                         ("transition_duration", 0), ("transition_duration", "nan"),
                                         ("transition_duration", 9)])
def test_L21_bad_submit(user, media, field, value):
    assert submit(user, media["junk"], **{field: value}).status_code == 400


@pytest.fixture
def finished(app_module, make_user, tmp_path):
    client, me = make_user()
    inter = tmp_path / "inter.mp4"
    inter.write_bytes(b"x")
    job = uuid.uuid4().hex
    app_module.job_store[job] = {"owner_id": me["id"], "status": "done", "intermediate_path": str(inter),
                                 "intermediate_duration": 5.0, "transcript_data": fake_transcript(),
                                 "clips_to_keep": [(0, 5)], "keep_segments": [(0, 5)], "original_duration": 5,
                                 "input_video_path": str(inter)}
    yield client, job
    app_module.job_store.pop(job, None)


@pytest.mark.parametrize("captions", ['[1]', '{"a":1}', '[{"t":"no times"}]', '[{"s":"x","e":1,"t":"a"}]',
                                      '[{"s":NaN,"e":1,"t":"a"}]', 'not json'])
@pytest.mark.parametrize("endpoint", ["captions", "caption-command"])
def test_L21_bad_captions(finished, endpoint, captions):
    client, job = finished
    r = client.post(f"/{endpoint}/{job}", data={"captions": captions, "edited": 1, "comment": "delete caption 1"})
    assert r.status_code == 400


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_L21_bad_feedback(finished, value):
    client, job = finished
    assert client.post("/feedback/", data={"job_id": job, "timestamp_start": 1, "timestamp_end": value,
                                           "correction": "CUT"}).status_code == 400


@pytest.mark.parametrize("status", ["queued", "processing", "error"])
def test_L21_export_before_ready_is_409(app_module, finished, status):
    client, job = finished
    app_module.job_store[job].update(status=status, progress=5, error="x")
    assert client.post(f"/export/{job}", data={}).status_code == 409


@pytest.mark.parametrize("path", ["/editor", "/editor.html", "/download/x", "/srt/x"])
def test_S04_the_legacy_editor_is_gone(user, path):
    assert user.get(path).status_code == 404


# ---- L-18
def test_L18_mixed_formats_keep_pitch_and_sync(media, tmp_path, monkeypatch):
    import clip_combiner
    import edit_render
    import numpy as np
    monkeypatch.chdir(tmp_path)
    paths = [str(media["clip"]), str(media["other"])]
    canvas = edit_render.natural_canvas(paths[0])
    cuts = [edit_render.cut(path, [(0, 6)], prefix, canvas=canvas) for path, prefix in zip(paths, ("aaaaaaaa", "bbbbbbbb"))]
    clip_combiner.join_clips(cuts, ["hard_cut"], 0.5, "cccccccc", "out.mp4")
    streams, _ = ffprobe("out.mp4")
    assert abs(float(streams["audio"]["duration"]) - float(streams["video"]["duration"])) < 0.15
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-ss", "8", "-t", "2", "-i", "out.mp4", "-map", "0:a", "-ac", "1",
                          "-ar", "8000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    samples = np.frombuffer(pcm, dtype="f4")
    assert np.argmax(abs(np.fft.rfft(samples))) * 8000 / len(samples) == pytest.approx(880, abs=8)


def test_NEW2_failed_evaluation_still_reports_the_edited_length(app_module, user, media, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("evaluation fell over")
    monkeypatch.setattr(engine(app_module), "evaluate", boom)
    job = edit(user, media["clip"])
    timeline = user.get(f"/timeline/{job}").json()
    assert 0 < timeline["edited_duration"] < timeline["original_duration"]


def test_C05_copy_join_exports_at_the_source_rate(media, tmp_path, monkeypatch):
    import edit_render
    import media_probe
    import render_engine
    monkeypatch.chdir(tmp_path)
    cuts = [(0.013, 0.6), (1.01, 1.55), (2.02, 2.68), (3.03, 3.71), (4.04, 4.77), (5.05, 5.93)]
    made = edit_render.cut(str(media["clip"]), cuts, "aaaaaaaa")
    (tmp_path / "x.ass").write_text("[Script Info]\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour\n"
                                    "Style: D,Arial,20,&H00FFFFFF\n[Events]\nFormat: Layer, Start, End, Style, Text\n"
                                    "Dialogue: 0,0:00:00.00,0:00:09.00,D,hello\n")
    render_engine.burn_subtitles(made.path, "export.mp4", ass_path="x.ass", job_prefix="c05")
    assert media_probe.info("export.mp4").fps == 25


def test_cinematic_and_matte_change_the_picture(media, tmp_path, monkeypatch):
    import edit_render
    monkeypatch.chdir(tmp_path)

    def frame(grade, prefix):
        made = edit_render.cut(str(media["clip"]), [(0, 1)], prefix, color_grade=grade)
        return subprocess.run(["ffmpeg", "-v", "error", "-i", made.path, "-frames:v", "1", "-f", "rawvideo", "-"],
                              capture_output=True, check=True).stdout
    plain = frame("none", "aaaaaaaa")
    assert frame("cinematic", "bbbbbbbb") != plain and frame("matte", "cccccccc") != plain


@pytest.mark.parametrize("clips", [1, 2])
def test_S04_studio_gets_what_the_editor_showed(app_module, user, media, reference, clips):
    job = edit(user, *[media["clip"]] * clips, transition_type="hard_cut", reference_link="https://example.com/ref")
    seen = user.get(f"/timeline/{job}").json()["reference"]
    assert {"speaking_rate", "cuts_per_minute", "boundary_ratio", "max_duration", "compress_duration",
            "style_match_pct"} <= set(seen)
    assert seen["speaking_rate"] is not None
    report = user.get(f"/report/{job}").json()["report"]
    assert {"total_detected", "cut", "compressed", "kept"} <= set(report["pauses"])
    assert report["speech"]["transcription_quality"]
    plain = edit(user, media["clip"])
    assert user.get(f"/timeline/{plain}").json()["reference"] is None
