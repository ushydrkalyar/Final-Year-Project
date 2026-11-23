"""C-07: the combined report is built from the clips' reports, by the evaluator."""
import evaluator


def report(score, saved, fillers, wpm, cut):
    return {"quality_score": {"overall": score}, "time_savings": {"time_saved_s": saved},
            "speech": {"wpm_original": wpm, "wpm_edited": wpm * 2},
            "fillers": {"total_filler_words_detected": fillers},
            "pauses": {"cut": cut, "compressed": 1, "silence_ratio_original_pct": 40, "silence_ratio_edited_pct": 10}}


def test_C07_aggregate():
    got = evaluator.aggregate([report(90, 2, 1, 100, 2), {}, report(84, 3, 0, 120, 1)], 20.0, 15.0)
    assert got == {
        "quality_score": {"overall": 87.0, "grade": "A"},
        "time_savings": {"time_saved_s": 5, "time_saved_pct": 25.0,
                         "original_duration_s": 20.0, "edited_duration_s": 15.0},
        "speech": {"wpm_original": 110.0, "wpm_edited": 220.0, "wpm_increase_pct": 100.0, "transcription_quality": "unknown"},
        "fillers": {"total_filler_words_detected": 1},
        "pauses": {"cut": 3, "compressed": 2, "kept": 0, "total_detected": 0, "silence_ratio_original_pct": 40.0, "silence_ratio_edited_pct": 10.0},
    }


def test_C07_nothing_to_aggregate():
    assert evaluator.aggregate([{}, {}], 10.0, 8.0) == {}


def test_C07_a_combined_video_is_graded_like_a_single_clip():
    assert evaluator.aggregate([report(72, 1, 0, 100, 1)], 10.0, 9.0)["quality_score"]["grade"] == evaluator._score_to_grade(72)
