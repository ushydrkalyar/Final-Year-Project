"""C-04: one cached probe, always with a timeout."""
import json
import shutil
import subprocess

import pytest

import media_probe

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def make(path, *args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args, str(path)], check=True)
    return str(path)


def test_C04_landscape(tmp_path):
    path = make(tmp_path / "land.mp4", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2", "-f", "lavfi", "-i",
                "sine=sample_rate=44100:duration=2", "-ac", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")
    got = media_probe.info(path)
    assert (got.width, got.height, got.fps, got.rotation) == (320, 180, 25.0, 0)
    assert got.duration == pytest.approx(2, abs=0.1) and got.has_video and got.has_audio
    assert (got.codec, got.audio_codec, got.sample_rate, got.channels) == ("h264", "aac", 44100, 1)
    assert got.size_bytes == (tmp_path / "land.mp4").stat().st_size


def test_C04_rotation_metadata(tmp_path):
    flat = make(tmp_path / "flat.mp4", "-f", "lavfi", "-i", "testsrc2=s=320x180:d=1", "-c:v", "libx264")
    turned = make(tmp_path / "turned.mp4", "-display_rotation", "90", "-i", flat, "-c", "copy")
    got = media_probe.info(turned)
    assert (got.width, got.height) == (320, 180)
    assert got.display_size == (180, 320)


def test_C04_audio_only_and_non_media(tmp_path):
    got = media_probe.info(make(tmp_path / "a.wav", "-f", "lavfi", "-i", "sine=duration=1"))
    assert got.has_audio and not got.has_video
    junk = tmp_path / "junk.mp4"
    junk.write_text("not media")
    assert media_probe.info(str(junk)) is None
    assert media_probe.info(str(tmp_path / "missing.mp4")) is None


def test_C04_every_probe_has_a_timeout_and_is_cached(tmp_path, monkeypatch):
    path = tmp_path / "x.mp4"
    path.write_text("x")
    calls = []

    def run(cmd, **kw):
        calls.append(kw.get("timeout"))
        body = {"streams": [{"codec_type": "video", "width": 8, "height": 8}], "format": {"duration": "1"}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
    monkeypatch.setattr(media_probe.subprocess, "run", run)
    media_probe.info(str(path))
    media_probe.info(str(path))
    assert calls == [30]
    path.write_text("changed")
    media_probe.info(str(path))
    assert len(calls) == 2


def test_C04_a_hung_probe_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "hung.mp4"
    path.write_text("x")

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(media_probe.subprocess, "run", run)
    assert media_probe.info(str(path)) is None


def test_C04_ffprobe_is_only_run_by_media_probe():
    from conftest import SRC
    runners = [p.name for p in SRC.glob("*.py") if '["ffprobe"' in p.read_text() or '"ffprobe", "-' in p.read_text()]
    assert runners == ["media_probe.py"]
