"""S-08: Lime Pop's plain words on landscape read like its sibling templates'."""
import re

import pytest

import subtitle_service
from render_engine import SubtitleStyle

WORDS = "ads was made by AI the other one".split()


def lime_sizes(tmp_path, monkeypatch, size):
    monkeypatch.setattr(subtitle_service, "_probe_dims", lambda path: size)
    style = SubtitleStyle(font_name="Montserrat", font_size=16, emphasis=True, emph_look="lime", emph_every=1,
                          emph_font="Montserrat ExtraBold", emph_scale=3.4, words_per_caption=3, layout="scatter", pos_y=0.5)
    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(WORDS)]
    out = tmp_path / "c.ass"
    subtitle_service.create_remapped_pop_ass({"segments": [{"start": 0, "end": 5, "words": words}]}, [(0, 5)], str(out),
                                             words_per_caption=3, style=style, video_path="probed")
    found = [int(n) for n in re.findall(r"\\fs(\d+)", out.read_text())]
    return min(found), max(found)


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (3840, 2160)])
def test_S08_plain_words_are_readable_on_landscape(tmp_path, monkeypatch, size):
    plain, _ = lime_sizes(tmp_path, monkeypatch, size)
    assert plain / min(size) >= 0.074       # Red Editorial's plain words are 0.067 of it, in a taller face


def test_S08_portrait_and_the_keyword_are_unchanged(tmp_path, monkeypatch):
    assert lime_sizes(tmp_path, monkeypatch, (1080, 1920)) == (68, 231)
    assert lime_sizes(tmp_path, monkeypatch, (1920, 1080))[1] == 207
