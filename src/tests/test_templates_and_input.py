"""Templates, style validation and diagnostics. Each test is named for the audit finding it guards."""
import importlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import SRC

CONFIG = {"font": "Arial", "face": "Bold", "italic": False, "size": 40, "color": "#FFEE00",
          "y": 80, "align": "c", "wpc": 3, "lines": 1}


@pytest.fixture
def ocr(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "extract_caption_style",
                        lambda path: {"config": CONFIG, "samples": 9, "debug": {}})


def extract(client, name="My style"):
    return client.post("/extract-reference-style", data={"name": name},
                       files={"file": ("ref.mp4", b"not really a video", "video/mp4")})


# ---- L-12
def test_L12_each_user_sees_only_their_templates(make_user, ocr):
    a, _ = make_user()
    b, _ = make_user()
    key = extract(a, "Mine").json()["key"]
    assert a.get("/custom-templates").json() == {key: {"label": "Mine", **CONFIG}}
    assert b.get("/custom-templates").json() == {}


def test_L12_templates_are_stored_in_postgres(make_user, ocr, sql):
    client, user = make_user()
    key = extract(client, "Kept").json()["key"]
    assert sql("SELECT to_regclass('custom_templates')")[0][0], "table missing"
    assert sql("SELECT owner_id, label FROM custom_templates WHERE key = %s", (key,)) == [(user["id"], "Kept")]


def test_L12_parallel_creates_lose_nothing(make_user, ocr):
    client, _ = make_user()
    with ThreadPoolExecutor(12) as ex:
        assert set(ex.map(lambda i: extract(client, f"T{i}").status_code, range(24))) == {200}
    assert len(client.get("/custom-templates").json()) == 24


def test_L12_templates_table_is_hidden_from_anon(make_user, ocr, sql):
    client, _ = make_user()
    extract(client)
    assert sql("SELECT to_regclass('custom_templates')")[0][0], "table missing"
    sql("SET ROLE anon")
    try:
        assert sql("SELECT count(*) FROM custom_templates") == [(0,)]
    finally:
        sql("RESET ROLE")


def test_L12_legacy_file_is_imported_once(app_module, make_user, tmp_path):
    templates = importlib.import_module("templates")
    client, user = make_user()
    key = "ref_" + uuid.uuid4().hex[:8]
    path = tmp_path / "custom_templates.json"
    path.write_text(json.dumps({key: {"label": "Old", "owner_id": user["id"], **CONFIG}}))
    templates.import_legacy(str(path))
    assert client.get("/custom-templates").json() == {key: {"label": "Old", **CONFIG}}
    assert not path.exists() and (tmp_path / "custom_templates.json.migrated").exists()


def test_L12_unreadable_legacy_file_is_left_untouched(app_module, tmp_path):
    templates = importlib.import_module("templates")
    path = tmp_path / "custom_templates.json"
    path.write_text('{"half": ')
    templates.import_legacy(str(path))
    assert path.read_text() == '{"half": '


def test_L12_owner_can_delete_and_others_cannot(make_user, ocr):
    a, _ = make_user()
    b, _ = make_user()
    key = extract(a).json()["key"]
    assert b.delete(f"/custom-templates/{key}").status_code == 404
    assert a.delete(f"/custom-templates/{key}").status_code == 200
    assert a.get("/custom-templates").json() == {}


def test_L12_there_is_a_per_user_cap(make_user, ocr, monkeypatch):
    templates = importlib.import_module("templates")
    monkeypatch.setattr(templates, "MAX_PER_USER", 3)
    client, _ = make_user()
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda i: extract(client, f"T{i}").status_code, range(8)))
    assert codes.count(200) == 3 and codes.count(409) == 5


# ---- FE-06
def test_FE06_server_strips_markup_from_labels(make_user, ocr):
    client, _ = make_user()
    label = extract(client, "<img src=x onerror=alert(1)>\"'&\x00Nice").json()["label"]
    assert not set(label) & set("<>\"'&\x00")


# ---- FE-07
@pytest.mark.parametrize("colour", ["&H00fff", "red", "#FFFFFF", "&H00FFFFFF\nDialogue: x", "&H00FFFFFF\n", ""])
def test_FE07_bad_colours_fall_back(colour):
    from edit_render import build_style
    style, _ = build_style({"subtitle_preset": "custom", "subtitle_colour": colour, "emph_colour": colour})
    assert style.primary_colour == "&H00FFFFFF" and style.emph_colour == "&H0030EAB4"


def test_FE07_good_colours_are_kept():
    from edit_render import build_style
    style, _ = build_style({"subtitle_preset": "custom", "subtitle_colour": "&H0000ffff", "emph_colour": "&H801C28C0"})
    assert (style.primary_colour, style.emph_colour) == ("&H0000ffff", "&H801C28C0")


@pytest.mark.parametrize("size,expected", [("99999", 120), ("-4", 6), ("inf", 18), ("nan", 18), ("24", 24)])
def test_FE07_size_is_clamped(size, expected):
    from edit_render import build_style
    assert build_style({"subtitle_preset": "custom", "subtitle_size": size})[0].font_size == expected


@pytest.mark.parametrize("font", ["Arial,99,&H000000FF", "Arial\nDialogue: x", "Arial{\\p1}", "x" * 300, "Arial\n"])
def test_FE07_fonts_cannot_inject_ass(font):
    from edit_render import build_style
    style, _ = build_style({"subtitle_preset": "custom", "subtitle_font": font, "emph_font": font})
    assert style.font_name == "Arial" and style.emph_font == "Arial Black"


def test_FE07_every_bundled_font_is_accepted():
    from edit_render import build_style
    for family in [f["family"] for f in json.loads((SRC / "fonts" / "fonts.json").read_text())]:
        assert build_style({"subtitle_preset": "custom", "subtitle_font": family})[0].font_name == family


# ---- L-22
def test_L22_version_never_spawns_a_process(app_module, new_client, monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("spawned a process")
    for name in ("getoutput", "check_output", "run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden)
    assert new_client().get("/version").status_code == 200


def test_L22_debug_is_dev_only(app_module, make_user, monkeypatch):
    client, user = make_user()
    job_id = uuid.uuid4().hex
    app_module.job_store[job_id] = {"owner_id": user["id"], "status": "done"}
    try:
        assert client.get(f"/debug/{job_id}").status_code == 200
        monkeypatch.setenv("DEV_MODE", "0")
        assert client.get(f"/debug/{job_id}").status_code == 404
    finally:
        app_module.job_store.pop(job_id)
