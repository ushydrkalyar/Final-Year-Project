"""S-07: pieces are frame-exact, join without drift, and only changed ones are encoded."""
import shutil
import subprocess
from fractions import Fraction

import numpy as np
import pytest

import edit_render
import segment_render

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")

STEP = 2        # grey levels between neighbouring source frames
LEVELS = 100


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *map(str, args)], check=True)


@pytest.fixture(scope="module", params=["25", "30000/1001", "60"])
def numbered(request, tmp_path_factory):
    """20 s whose frame N has grey level 24 + 2 * (N % 100), with a tone that rises as it plays."""
    path = tmp_path_factory.mktemp("numbered") / "src.mp4"
    ff("-f", "lavfi", "-i",
       f"color=black:s=160x120:r={request.param}:d=20,format=yuv420p,geq=lum='24+{STEP}*mod(N\\,{LEVELS})':cb=128:cr=128",
       "-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*(300+20*t)*t)':s=48000:d=20",
       "-c:v", "libx264", "-bf", "3", "-g", "90", "-color_range", "tv", "-c:a", "aac", path)
    return str(path), Fraction(request.param)


def greys(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 160 * 120 * 3 // 2)[:, :160 * 120].mean(axis=1)


def sound(path):
    return np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", "48000",
                                         "-f", "f32le", "-"], capture_output=True, check=True).stdout, "f4")


KEEP = [(0.0, 1.013), (2.5, 9.47), (9.9, 9.955), (11.99, 20.0)]


def test_every_frame_and_sample_is_the_one_asked_for(numbered, tmp_path, monkeypatch):
    source, fps = numbered
    monkeypatch.chdir(tmp_path)
    made = edit_render.cut(source, KEEP, "aaaaaaaa")
    ranges = segment_render.frame_ranges(KEEP, made.canvas)
    wanted = [n for a, b in ranges for n in range(a, b)]
    shown = greys(made.path)
    assert len(shown) == len(wanted) == made.frames
    expected = np.array([24 + STEP * (n % LEVELS) for n in wanted])
    last = int(20 * fps) - 1            # past the last source frame, that frame is held
    inside = np.array(wanted) < last
    assert np.abs(shown - expected)[inside].max() < STEP * 0.75
    assert sum(made.real_durations) == pytest.approx(made.frames / float(fps))

    original, edited = sound(source), sound(made.path)
    assert abs(len(edited) / 48000 - made.frames / float(fps)) < 0.05
    done = 0
    for a, b in ranges:
        if (b - a) / fps > 1:       # the middle of each long range lines up to within 2 ms
            middle, at = made.canvas.samples((a + b) // 2), made.canvas.samples(done + (b - a) // 2)
            part = edited[at:at + 9600]
            scores = [float(np.dot(part, original[middle + lag:middle + lag + len(part)]))
                      for lag in range(-480, 481, 8)]
            assert abs(range(-480, 481, 8)[int(np.argmax(scores))]) <= 96
        done += b - a


def test_a_correction_encodes_only_what_changed(numbered, tmp_path, monkeypatch):
    source, _ = numbered
    monkeypatch.chdir(tmp_path)
    first = edit_render.cut(source, [(0, 20)], "aaaaaaaa")
    encoded = []
    real = segment_render.encode_range
    monkeypatch.setattr(segment_render, "encode_range",
                        lambda src, a, b, *rest: (encoded.append(b - a), real(src, a, b, *rest)))
    second = edit_render.cut(source, [(0, 13.0), (14.0, 20)], "bbbbbbbb", previous=first)
    assert 0 < sum(encoded) <= second.canvas.frames(segment_render.CHUNK_S)
    assert len(greys(second.path)) == second.frames
    encoded.clear()
    edit_render.cut(source, [(0, 20)], "cccccccc", previous=first)
    assert encoded == []


def test_split_points_are_fixed_on_the_original():
    canvas = segment_render.Canvas(160, 120, Fraction(25))
    assert segment_render.split_range(10, 400, canvas) == [(10, 150), (150, 300), (300, 400)]
    assert segment_render.split_range(160, 400, canvas)[1] == (300, 400)
    assert segment_render.split_range(10, 160, canvas) == [(10, 160)]   # no splinter at the end
    assert segment_render.split_range(7, 7, canvas) == []


@pytest.mark.parametrize("rate,expected", [("25/1", 25), ("30000/1001", Fraction(30000, 1001)), ("90000/1", 30),
                                           ("120/1", 60), ("0/0", 30), ("", 30)])
def test_frame_rate(rate, expected):
    assert segment_render.frame_rate(rate) == expected


def test_L08_a_failed_piece_fails_the_render_and_leaves_nothing(numbered, tmp_path, monkeypatch):
    source, _ = numbered
    monkeypatch.chdir(tmp_path)
    real = segment_render.encode_range

    def encode(src, first, end, *rest):
        if first:
            raise RuntimeError("Could not encode segment 2")
        real(src, first, end, *rest)
    monkeypatch.setattr(segment_render, "encode_range", encode)
    with pytest.raises(RuntimeError, match="segment 2"):
        edit_render.cut(source, [(0, 1), (2, 3)], "aaaaaaaa")
    assert list(tmp_path.iterdir()) == []


def test_L08_a_sliver_has_no_duration(numbered, tmp_path, monkeypatch):
    source, _ = numbered
    monkeypatch.chdir(tmp_path)
    assert edit_render.cut(source, [(0, 1), (2, 2.005)], "aaaaaaaa").real_durations[1] == 0


HOSTILE = {
    "sound_outlasts_picture": ["-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=3", "-f", "lavfi", "-i", "sine=d=4"],
    "picture_outlasts_sound": ["-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=4", "-f", "lavfi", "-i", "sine=d=2"],
    "late_sound": ["-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=4", "-itsoffset", "0.7", "-f", "lavfi", "-i", "sine=d=3"],
    "mono_44k": ["-f", "lavfi", "-i", "testsrc2=s=162x122:r=50:d=4", "-f", "lavfi", "-i", "sine=sample_rate=44100:d=4"],
    "variable_rate": ["-f", "lavfi", "-i", "testsrc2=s=160x120:r=30:d=4,select='not(eq(mod(n\\,7)\\,3))'", "-f", "lavfi",
                      "-i", "sine=d=4", "-fps_mode", "vfr"],
}


@pytest.mark.parametrize("kind", HOSTILE)
def test_awkward_uploads(kind, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ff(*HOSTILE[kind], "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "in.mp4")
    import media_probe
    length = media_probe.duration("in.mp4")
    made = edit_render.cut("in.mp4", [(0.2, 1.1), (1.5, length)], "aaaaaaaa")
    found = media_probe.info(made.path)
    assert found.duration == pytest.approx(sum(made.real_durations), abs=0.06)
    assert found.video_seconds == pytest.approx(sum(made.real_durations), abs=0.02)
    assert (found.sample_rate, found.channels) == (48000, 2)


def test_rotation_metadata_comes_out_upright(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ff("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=2", "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "flat.mp4")
    ff("-display_rotation", "90", "-i", "flat.mp4", "-c", "copy", "turned.mp4")
    import media_probe
    found = media_probe.info(edit_render.cut("turned.mp4", [(0, 2)], "aaaaaaaa").path)
    assert (found.width, found.height, found.rotation) == (120, 160, 0)


@pytest.mark.parametrize("grade", ["none", "cinematic", "bright", "matte", "vivid", "cool"])
def test_grades_render(numbered, grade, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert edit_render.cut(numbered[0], [(1, 2)], "aaaaaaaa", color_grade=grade).frames
