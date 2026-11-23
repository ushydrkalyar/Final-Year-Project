"""Pure pipeline rules: no ffmpeg, no database. Audit findings are named in the tests that guard them."""
import math
import unicodedata

import pandas as pd
import pytest

import conftest  # noqa: F401  (stubs whisper before any project import)
import cut_list
import edit_render
import render_engine
from comment_parser import parse_command
from decision_engine import classify_pauses, compress_target
from filler_remover import detect_fillers
from style_extractor import calculate_style_profile

PRESETS = {"aggressive": (0.5, 0.3, 0.0), "natural": (2.5, 1.2, 0.5), "balanced": (1.5, 0.8, 0.0)}


def transcript(text, language="en", gap=0.3):
    words = [{"word": " " + w, "start": i * (0.3 + gap), "end": i * (0.3 + gap) + 0.3}
             for i, w in enumerate(text.split())]
    return {"language": language, "segments": [{"start": 0, "end": words[-1]["end"], "text": text, "words": words}]}


def _language_arg(language):
    return {"language": language}


def cut_words(text, language="en", gap=0.3):
    t = transcript(text, language, gap)
    cuts = detect_fillers(t, **_language_arg(language))
    return [w["word"].strip() for w in t["segments"][0]["words"]
            if any(s - 1e-3 <= w["start"] and w["end"] <= e + 1e-3 for s, e in cuts)]


def pauses(*rows):
    return pd.DataFrame([{"start_time": float(i * 5), "end_time": i * 5 + d, "duration": d, "rms_energy": 0.001,
                          "is_sentence_boundary": b, "previous_word": w, "next_word": "x", "speech_density": 2.5}
                         for i, (d, b, w) in enumerate(rows)])


# ---- cut_list
def test_normalize():
    assert cut_list.normalize([(3, 4), (-1, 1), (1, 2), (3.5, 9)], 6) == [(0, 2), (3, 6)]
    assert cut_list.normalize([(0, 1), (2, 2.01)], 5, min_len=0.05) == [(0, 1)]


def test_subtract_and_removed():
    assert cut_list.subtract([(0, 5)], 2, 3) == [(0, 2), (3, 5)]
    assert cut_list.removed([(1, 2), (3, 4)], 5) == [(0, 1), (2, 3), (4, 5)]


def test_corrections():
    keep = [(0, 2), (3, 5)]
    assert cut_list.apply_correction(keep, 2, 3, "KEEP", 5) == [(0, 5)]
    assert cut_list.apply_correction(keep, 3, 5, "cut", 5) == [(0, 2)]
    for args in ((6, 7, "CUT"), (2, 1, "CUT"), (0, 5, "CUT"), (1, 2, "NOPE")):
        with pytest.raises(ValueError):
            cut_list.apply_correction(keep, *args, 5)


@pytest.mark.parametrize("start,end", [(math.nan, 2), (1, math.nan), (1, math.inf), (-math.inf, 2)])
def test_L21_non_finite_corrections_are_rejected(start, end):
    with pytest.raises(ValueError):
        cut_list.apply_correction([(0, 5)], start, end, "CUT", 5)


def test_L08_corrections_leave_no_slivers():
    assert cut_list.apply_correction([(0, 2), (3, 5)], 0.02, 2, "CUT", 5) == [(3, 5)]
    with pytest.raises(ValueError):
        cut_list.apply_correction([(0, 2)], 0.02, 1.99, "CUT", 2)


def test_label_timeline_prefers_the_later_reason_on_a_tie():
    reasons = [{"start": 2, "end": 3, "kind": "pause_cut", "reason": "Pause"},
               {"start": 2, "end": 3, "kind": "user_cut", "reason": "You"}]
    assert cut_list.label_timeline([(0, 2), (3, 5)], 5, reasons)[1]["type"] == "user_cut"


# ---- render_engine.get_keep_segments
def keeps(rows=(), **kw):
    df = pd.DataFrame(rows, columns=["start_time", "end_time", "decision"])
    return [(round(s, 3), round(e, 3)) for s, e in render_engine.get_keep_segments(df, 10.0, **kw)]


def test_keep_segments():
    assert keeps() == [(0, 10)]
    assert keeps([(2, 4, "CUT")]) == [(0, 2), (4, 10)]
    assert keeps([(2, 4, "COMPRESS")], style_profile={"compress_duration": 0.8}) == [(0, 2.4), (4, 10)]
    assert keeps([(2, 4, "CUT")], filler_cuts=[(3.5, 4.5)]) == [(0, 2), (4.5, 10)]
    assert keeps([(2, 4, "CUT"), (4.1, 5, "CUT")]) == [(0, 2), (5, 10)]
    assert keeps(silence_cuts=[(0, 1), (9, 10)]) == [(1, 9)]


# ---- decision_engine
@pytest.mark.parametrize("name,expected", [("aggressive", ["KEEP", "CUT", "CUT"]),
                                           ("balanced", ["KEEP", "KEEP", "CUT"]),
                                           ("natural", ["KEEP", "KEEP", "COMPRESS"])])
def test_classify_by_preset(name, expected):
    longest, compress, boost = PRESETS[name]
    profile = {"max_duration": longest, "compress_duration": compress, "speaking_rate": 2.5,
               "boundary_keep_boost": boost}
    got = classify_pauses(pauses((.25, False, "a"), (.6, False, "b"), (2.0, False, "c")), profile)
    assert list(got["decision"]) == expected
    assert 0.05 <= compress_target(profile) < compress


def test_classify_empty():
    assert classify_pauses(pd.DataFrame(), {}).empty


def test_L07_fast_reference_penalty_uses_the_shared_list():
    profile = {"max_duration": 1.0, "compress_duration": 0.5, "speaking_rate": 4.2}
    got = classify_pauses(pauses((.7, False, "like"), (.7, False, "um")), profile)
    assert list(got["decision"]) == ["COMPRESS", "CUT"]


# ---- filler_remover
@pytest.mark.parametrize("text,cut", [
    ("I like this so much", []),
    ("Well right now it actually works okay", []),
    ("um, hello Uh. world", ["um,", "Uh."]),
    ("So, you know, it works", ["you", "know,"]),
    ("It works. You know, really well", ["You", "know,"]),
    ("It works, I mean, really well", ["I", "mean,"]),
    ("um I mean it", ["um"]),
    ("I know what you mean", []),
    ("What kind of car is that", []),
    ("take them to the ER now", []),
    ("Do you like it? I guess. Maybe", []),
])
def test_L07_english(text, cut):
    assert cut_words(text) == cut


@pytest.mark.parametrize("language,text", [
    ("ur", "main aur tum lekin woh phir bhi bas agar na"),
    ("hi", "main aur tum lekin woh phir bhi bas sahi hi"),
    ("ar", "هذا صح يعني طيب عم"),
    ("de", "er kommt um drei so"),
    ("pt", "um carro"),
    ("ur", "ہم گھر جا رہے ہیں"),
    ("hi", "हम घर जा रहे हैं"),
])
def test_L07_ordinary_words_survive_in_other_languages(language, text):
    assert cut_words(text, language) == []


@pytest.mark.parametrize("language,word", [("ur", "اممم"), ("ur", "um"), ("hi", "उम्म"), ("ar", "اممم"),
                                           ("de", "äh"), ("fr", "euh"), ("xx", "hmm")])
def test_L07_hesitations_in_other_languages(language, word):
    assert cut_words(f"AAA {word} BBB", language) == [word]


def test_L07_combining_marks_match_in_either_normal_form():
    word = unicodedata.normalize("NFD", "äh")
    assert cut_words(f"ja {word} gut", "de") == [word]


def test_L07_merge_never_swallows_a_word_between_two_fillers():
    words = [("um", 0, .3), ("a", .32, .42), ("uh", .44, .7), ("er", .75, .9)]
    t = {"segments": [{"words": [{"word": w, "start": s, "end": e} for w, s, e in words]}]}
    assert detect_fillers(t) == [(0, .3), (.44, .9)]


def test_L07_forced_language_is_reported_as_a_code(monkeypatch):
    import transcription_service as ts

    class Echo:     # what openai-whisper does: the name it was given comes back untouched
        def transcribe(self, path, language=None, **kw):
            return {"language": language or "de", "segments": []}
    monkeypatch.setattr(ts, "_WHISPER_MODEL", Echo())
    assert ts.transcribe_audio("x.wav", "ur")["language"] == "ur"
    assert ts.transcribe_audio("x.wav", "auto")["language"] == "de"


# ---- edit_render
def test_build_style():
    style, preset = edit_render.build_style({"subtitle_preset": "cinema", "words_per_caption": "40", "pos_x": "9"})
    assert (preset, style.font_name, style.words_per_caption, style.pos_x) == ("cinema", "Georgia", 12, 0.98)
    style, _ = edit_render.build_style({"subtitle_preset": "custom", "line_spacing": "zzz", "emph_scale": "99"})
    assert (style.line_spacing, style.emph_scale) == (1.0, 6.0)


def test_transcript_from_captions():
    got = edit_render.transcript_from_captions([{"s": 2, "e": 3, "t": "two words"}, {"s": 0, "e": 1, "t": " "}], True)
    assert got["respect_segments"] and len(got["segments"]) == 1
    assert [w["word"] for w in got["segments"][0]["words"]] == ["two", "words"]


def test_edited_transcript():
    t = {"segments": [{"words": [{"word": "a", "start": .2, "end": .5}, {"word": "cut", "start": 1.2, "end": 1.5},
                                 {"word": "b", "start": 2.2, "end": 2.6}, {"word": "edge", "start": .8, "end": 1.2}]}]}
    words = edit_render.edited_transcript(t, [(0, 1), (2, 3)], [1.1, 1.0], offset=10)["segments"][0]["words"]
    assert [(w["word"], w["start"]) for w in words] == [("a", 10.2), ("b", 11.3)]


def test_L08_zero_length_segments_count_as_zero():
    from subtitle_service import _seg_duration
    assert _seg_duration(0, 0, 0.02, [0.0]) == 0.0


# ---- style_extractor
@pytest.mark.parametrize("duration", [0.05, 0.6, 30.0])
def test_style_profile_clamps(duration):
    got = calculate_style_profile(pauses((duration, True, "a"), (duration, False, "b")), transcript("a b c"))
    assert 0.4 <= got["max_duration"] <= 4.0
    assert 0.15 <= got["compress_duration"] < got["max_duration"]


# ---- comment_parser
CAPS = [{"s": 0, "e": 1, "t": "Hello wrld"}, {"s": 1, "e": 2, "t": "close the deal"},
        {"s": 2, "e": 3, "t": "close the door"}]


@pytest.mark.parametrize("command,index,after", [
    ("fix spelling of 'wrld' to 'world'", 0, "Hello world"),
    ("change caption 2 to win it", 1, "win it"),
    ("caption 3: shut it", 2, "shut it"),
    ("caption 3 should say shut it", 2, "shut it"),
    ("change 'deal' to 'sale'", 1, "close the sale"),
    ("replace door with gate", 2, "close the gate"),
    ("remove 'the' from caption 2", 1, "close deal"),
    ("delete caption 1", 0, None),
    ("delete the caption that says deal", 1, None),
])
def test_caption_commands(command, index, after):
    got = parse_command(command, CAPS)
    assert (got["ok"], got["index"], got["after"]) == (True, index, after)


@pytest.mark.parametrize("command", ["change caption 9 to x", "change 'close' to 'shut'", "make it bigger", ""])
def test_caption_commands_that_must_not_edit(command):
    assert parse_command(command, CAPS)["ok"] is False
