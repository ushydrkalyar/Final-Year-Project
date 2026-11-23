"""Pure front-end helpers, run in Node."""
import json
import shutil
import subprocess

import pytest

from conftest import SRC

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def run_js(asset, expr, prelude="globalThis.location = {origin: 'http://localhost:8000'};"):
    code = prelude + (SRC / "assets" / asset).read_text() + f"\nconsole.log(JSON.stringify({expr}))"
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


@pytest.mark.parametrize("raw", ["//evil", "/\\evil", "https://evil", "javascript:alert(1)", "", "/\t/evil",
                                 "/..//evil", "/%2e%2e//evil", "/.//evil", "data:text/html,x", None])
def test_FE01_rejects(raw):
    assert run_js("session.js", f"safeNext({json.dumps(raw)})") == "/home.html"


@pytest.mark.parametrize("raw", ["/home.html", "/studio.html?p=1", "/studio.html?p=a#cuts"])
def test_FE01_accepts(raw):
    assert run_js("session.js", f"safeNext({json.dumps(raw)})") == raw


def test_FE06_esc_covers_text_and_attributes():
    assert run_js("ui.js", "esc(`<a b=\"c\" d='e'>&`)") == "&lt;a b=&quot;c&quot; d=&#39;e&#39;&gt;&amp;"


@pytest.mark.parametrize("raw,expected", [("#fff", "#FFFFFF"), ("1a2B3c", "#1A2B3C"), ("#AABBCC", "#AABBCC"),
                                          ("red", None), ("#ff", None), ('#fff"><b>', None), (None, None)])
def test_FE07_norm_hex(raw, expected):
    assert run_js("ui.js", f"normHex({json.dumps(raw)})") == expected


LIVE_PRELUDE = """
globalThis.location = {origin: 'http://localhost:8000'};
globalThis.added = [];
globalThis.JASSUB = class { constructor(o) { added.push(...o.fonts) } addFont(u) { added.push(u) } setTrack() {} };
globalThis.calls = 0;
globalThis.fetch = async () => { calls++; await new Promise(r => setTimeout(r, 30));
  if (calls === 1 && globalThis.failFirst) return {ok: false, status: 500, json: async () => ({detail: 'no'})};
  return {ok: true, json: async () => ({anton: ['/fonts/Anton.ttf']})} };
globalThis.video = {paused: false, readyState: 4, addEventListener() {}};
globalThis.ass = 'Style: Default,Anton,20';
"""


def run_async_js(asset, body, prelude):
    code = prelude + (SRC / "assets" / asset).read_text() + (
        f"\n(async () => {{ {body} }})().then(v => console.log(JSON.stringify(v)))")
    return json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout)


def test_FE13_overlapping_shows_keep_their_fonts():
    body = "const c = LiveCaptions.create(video); await Promise.all([c.show(ass), c.show(ass)]); return added;"
    assert run_async_js("live_captions.js", body, LIVE_PRELUDE) == ["/fonts/Anton.ttf"]


def test_FE13_failed_catalog_is_not_cached():
    body = ("const c = LiveCaptions.create(video); await c.show(ass); await c.show(ass + '\\n'); return added;")
    assert run_async_js("live_captions.js", body, LIVE_PRELUDE + "globalThis.failFirst = true;") == ["/fonts/Anton.ttf"]


@pytest.mark.parametrize("password", ["short1", "lettersonly", "12345678", "Passw0rd", "abcdefg1", "pässwörd1", "ééééééé1",
                                      "abc def 1", "😀😀😀😀a1", "😀😀😀😀😀😀a1", ""])
def test_FE19_password_rule_matches_the_server(app_module, password):
    try:
        app_module.auth._check_password_strength(password)
        server_accepts = True
    except ValueError:
        server_accepts = False
    assert (run_js("password.js", f"passwordProblem({json.dumps(password)})") == "") == server_accepts


API_PRELUDE = """
globalThis.location = {origin: 'http://localhost:8000', href: '', pathname: '/studio.html', search: '?p=1'};
globalThis.goToLogin = () => { location.href = 'login' };
globalThis.detailText = (d, f) => d?.detail || f;
globalThis.seen = [];
globalThis.fetch = async (url, init) => { seen.push(init); if (url === '/down') throw new TypeError('offline');
  const status = +url.slice(1) || 200;
  return {ok: status < 400, status, json: async () => ({detail: url === '/401w' ? 'wrong_password' : 'no'})} };
"""


def test_FE19_api_sends_credentials_and_throws_on_failure():
    body = """await api('/200', {json: {a: 1}});
      const got = {};
      for (const p of ['/500', '/down']) try { await api(p) } catch (e) { got[p] = e.status }
      return [seen[0].credentials, seen[0].method, seen[0].body, got];"""
    assert run_async_js("api.js", body, API_PRELUDE) == ["include", "POST", '{"a":1}', {"/500": 500, "/down": 0}]


def test_FE19_api_401_goes_to_login_unless_told_not_to():
    body = """let quiet = 0;
      try { await api('/401', {redirect: false}) } catch (e) { quiet = e.status }
      const before = location.href;
      api('/401'); await new Promise(r => setTimeout(r, 5));
      return [quiet, before, location.href];"""
    assert run_async_js("api.js", body, API_PRELUDE) == [401, "", "login"]


def test_NEW4_no_resize_while_the_video_has_no_size():
    prelude = LIVE_PRELUDE + """
globalThis.JASSUB = class { constructor(o) { this._video = o.video; globalThis.made = this }
  resize() { added.push('resized') } addFont() {} setTrack() {} };
video.videoWidth = 0;"""
    body = ("await LiveCaptions.create(video).show(ass); made.resize(); const during = [...added];"
            "video.videoWidth = 1920; made.resize(); return [during, added];")
    assert run_async_js("live_captions.js", body, prelude) == [[], ["resized"]]


def test_FE19_asset_urls_are_versioned():
    """Browsers hold copies cached before /assets sent no-cache: pages must not ask for those URLs."""
    import re
    bare = [(page.name, url) for page in SRC.glob("*.html")
            for url in re.findall(r'(?:src|href)="(/assets/[^"]+\.(?:js|css)[^"]*)"', page.read_text())
            if "/vendor/" not in url and "?v=" not in url]
    assert bare == []


def test_locked_templates_are_unchanged():
    """The owner's lock check, so the suite can never pass while it fails."""
    import sys
    done = subprocess.run([sys.executable, "locked_templates/check_locked_templates.py"], cwd=SRC,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout


def test_keyword_overflow_check_passes():
    import sys
    done = subprocess.run([sys.executable, "tests/check_keyword_overflow.py"], cwd=SRC, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
