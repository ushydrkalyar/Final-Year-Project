"""C-03: job files are found by their prefix, and only job files are ever deleted."""
import os
import time
import uuid

import pytest

import workspace


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORK_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    def put(name, age_hours=0, folder=False):
        path = tmp_path / name
        if folder:
            path.mkdir()
            (path / "frame_0001.jpg").write_text("x")
        else:
            path.write_text("x")
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path
    return put


def test_C03_remove_takes_every_kind_of_job_file(work, tmp_path):
    p = "ab12cd34"
    for name in (f"raw_{p}_a.mp4", f"_graded_{p}.mp4", f"subtitles_remapped_{p}.srt", f"ref_video_{p}.mp4.part",
                 f"restyled_ref_{p}_a.mp4", f"concat_list_{p}.txt", f"temp_clip_{p}_0001.mp4"):
        work(name)
    work(f"frames_{p}", folder=True)
    workspace.remove_job_files(p)
    assert list(tmp_path.iterdir()) == []


def test_C03_remove_spares_kept_and_other_jobs(work, tmp_path):
    p = "ab12cd34"
    spared = {f"raw_{p}_shared.mp4", f"raw_99998888_{p}.mp4", "notes.txt", "raw_ab12cd345_x.mp4"}
    for name in spared:
        work(name)
    workspace.remove_job_files(p, keep={f"raw_{p}_shared.mp4"})
    assert {x.name for x in tmp_path.iterdir()} == spared


@pytest.mark.parametrize("prefix", ["", "raw", "ab", "*", "../../.."])
def test_C03_remove_refuses_anything_but_a_job_prefix(work, tmp_path, prefix):
    work("raw_ab12cd34_a.mp4")
    with pytest.raises(ValueError):
        workspace.remove_job_files(prefix)
    assert len(list(tmp_path.iterdir())) == 1


def test_C03_sweep_removes_job_folders_but_never_other_folders(work):
    old = work("frames_ab12cd34", age_hours=100, folder=True)
    mine = work("My Videos", age_hours=100, folder=True)
    fresh = work("frames_11112222", folder=True)
    workspace.sweep_files()
    assert not old.exists() and mine.exists() and fresh.exists()


def test_C03_full_cleanup_leaves_nothing(app_module, work, tmp_path):
    job = uuid.uuid4().hex
    p = job[:8]
    for name in (f"_graded_{p}.mp4", f"ref_video_{p}.mp4", f"subtitles_remapped_{p}.ass", f"_intermediate_{p}_extract.mp4"):
        work(name)
    work(f"frames_{p}", folder=True)
    app_module.job_store[job] = {"status": "done", "owner_id": "x"}
    app_module.jobs.delete(job, app_module.job_store[job])
    assert list(tmp_path.iterdir()) == []
