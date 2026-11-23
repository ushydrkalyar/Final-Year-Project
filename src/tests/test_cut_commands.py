"""S-06: timed comments."""
import pytest

from cut_commands import parse_cut


@pytest.mark.parametrize("comment,expected", [
    ("cut 0:05 to 0:08", ("CUT", "range", 5, 8)),
    ("Cut from 5 to 8 seconds", ("CUT", "range", 5, 8)),
    ("please remove 1:02.5 - 1:04", ("CUT", "range", 62.5, 64)),
    ("trim between 3s and 4.5s", ("CUT", "range", 3, 4.5)),
    ("restore 0:10 until 0:12", ("KEEP", "range", 10, 12)),
    ("keep the pause at 0:12", ("KEEP", "point", 12)),
    ("don't cut the pause at 12 sec", ("KEEP", "point", 12)),
    ("Don’t remove the gap around 1:00", ("KEEP", "point", 60)),
    ("remove the pause at 3s", ("CUT", "point", 3)),
    ("remove the silence at the start", ("CUT", "edge", "start")),
    ("bring back the silence at the end", ("KEEP", "edge", "end")),
])
def test_timed_requests(comment, expected):
    got = parse_cut(comment)
    rest = {"range": ("start", "end"), "point": ("at",), "edge": ("edge",)}[got["kind"]]
    assert (got["action"], got["kind"], *(got[key] for key in rest)) == expected


@pytest.mark.parametrize("comment", [
    "remove 'um' from caption 4", "remove 'the' at 22 sec", "delete caption 5", "remove the caption at 3 sec",
    "change caption 2 to cut it out", "fix spelling of cutt to cut", "replace door with gate", "cut it out", "",
    "at 2 sec change 'closing' to 'sealing'", "delete the line at 0:04", 'remove "5 to 8" from caption 1',
])
def test_caption_comments_are_left_to_the_caption_parser(comment):
    assert parse_cut(comment) is None


def test_backwards_range_says_why():
    assert parse_cut("cut 0:08 to 0:05")["ok"] is False
