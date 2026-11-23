"""FE-20: Studio's models run without a DOM."""
import json
import shutil
import subprocess

import pytest

from conftest import SRC

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")

PRELUDE = """
globalThis.window = {addEventListener() {}};
globalThis.CUTSMART_LOCKED_TEMPLATES = {};
globalThis.normHex = v => /^#[0-9A-Fa-f]{6}$/.test(v || '') ? v.toUpperCase() : null;
globalThis.events = [];
globalThis.replies = {};
globalThis.apiJson = async path => { const r = replies[path]; const v = typeof r === 'function' ? r() : r;
  if (v instanceof Error) throw v; return v ?? {} };
globalThis.api = async path => { if (replies[path] instanceof Error) throw replies[path]; return {blob: async () => 'video'} };
const realTimeout = setTimeout;
globalThis.setTimeout = (fn) => realTimeout(fn, 1);
globalThis.wait = ms => new Promise(r => realTimeout(r, ms));
globalThis.failure = status => Object.assign(new Error('failed ' + status), {status});
globalThis.target = {sig: 'cut-settings'};
"""


def run(tmp_path, body):
    folder = tmp_path / "studio"
    shutil.copytree(SRC / "assets" / "studio", folder)
    (folder / "package.json").write_text('{"type": "module"}')
    (folder / "check.js").write_text(
        PRELUDE
        + "const bus = await import('./bus.js'); const job = await import('./job.js');\n"
        + "const style = await import('./style.js');\n"
        + "bus.on('job:state', s => events.push(s));\n"
        + f"const out = await (async () => {{ {body} }})();\nconsole.log(JSON.stringify(out));\n")
    done = subprocess.run(["node", str(folder / "check.js")], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


FIRST_EDIT = """
replies['/edit-video/'] = {job_id: 'a'}; replies['/status/a'] = {status: 'done'};
await job.submit({path: '/edit-video/', body: {}, target}); await wait(30);
"""


def test_FE20_states_of_a_successful_edit(tmp_path):
    got = run(tmp_path, FIRST_EDIT + "return [events, job.state(), job.editId(), job.edit().sig];")
    assert got == [["uploading", "processing", "ready"], "ready", "a", "cut-settings"]


def test_FE20_the_last_edit_survives_a_failed_one(tmp_path):
    got = run(tmp_path, FIRST_EDIT + """
        replies['/edit-video/'] = {job_id: 'b'}; replies['/status/b'] = {status: 'error', error: 'boom'};
        await job.submit({path: '/edit-video/', body: {}, target}); await wait(30);
        return [job.state(), job.editId(), job.making()];""")
    assert got == ["error", "a", None]


def test_FE20_the_edit_on_screen_is_not_the_one_being_made(tmp_path):
    got = run(tmp_path, FIRST_EDIT + """
        replies['/edit-video/'] = {job_id: 'b'}; replies['/status/b'] = {status: 'processing'};
        await job.submit({path: '/edit-video/', body: {}, target});
        const seen = [job.state(), job.editId(), job.making().id];
        replies['/status/b'] = {status: 'done'}; await wait(40);
        return [...seen, job.editId()];""")
    assert got == ["processing", "a", "b", "b"]


def test_FE20_polling_gives_up_after_five_misses(tmp_path):
    got = run(tmp_path, """let n = 0;
        replies['/edit-video/'] = {job_id: 'a'}; replies['/status/a'] = () => { n++; return failure(500) };
        await job.submit({path: '/edit-video/', body: {}, target}); await wait(80);
        return [n, job.state()];""")
    assert got == [5, "error"]


def test_FE20_a_refused_change_leaves_the_edit_ready(tmp_path):
    got = run(tmp_path, FIRST_EDIT + """let told;
        replies['/feedback/'] = failure(400);
        await job.submit({path: '/feedback/', body: {}, target, onRefused: m => { told = m }});
        return [job.state(), job.editId(), told];""")
    assert got == ["ready", "a", "failed 400"]


@pytest.mark.parametrize("status,event", [(404, "job:lost"), (403, "job:lost"), (500, "job:unreachable"), (0, "job:unreachable")])
def test_FE20_reopen(tmp_path, status, event):
    got = run(tmp_path, f"""const seen = [];
        for (const e of ['job:lost', 'job:unreachable']) bus.on(e, () => seen.push(e));
        replies['/status/z'] = failure({status});
        await job.reopen({{id: 'z', captions: null, target: () => target}});
        return seen;""")
    assert got == [event]


def test_FE20_editing_captions_is_undoable(tmp_path):
    got = run(tmp_path, """const c = await import('./captions.js'); const h = await import('./history.js');
        bus.on('project:before', change => h.push(change.captions));
        c.reset([{s: 0, e: 2, t: 'one two three four'}]);
        c.split(1); const two = c.list().map(x => x.t);
        c.adopt(h.undo(c.snapshot())); const back = c.list().map(x => x.t);
        c.adopt(h.redo(c.snapshot()));
        return [two, back, c.list().map(x => x.t), c.isEdited()];""")
    assert got == [["one two", "three four"], ["one two three four"], ["one two", "three four"], True]


def test_FE20_style_fields(tmp_path):
    got = run(tmp_path, """const f = {};
        style.styleFields({...style.DEFAULTS, color: '#FF8000'}, {append: (k, v) => { f[k] = v }});
        return [f.subtitle_preset, f.subtitle_size, f.subtitle_colour, f.subtitle_position, f.subtitle_margin_v];""")
    assert got == ["custom", 19, "&H000080FF", 2, 52]


def test_preset_pauses_match_the_server(tmp_path, app_module):
    got = run(tmp_path, "return style.PRESET_PAUSES;")
    assert got == {name: {"maxPause": p["max_duration"], "compressPause": p["compress_duration"]}
                   for name, p in app_module.pipeline.PACING_PRESETS.items()}


SEGMENTS = """const timeline = {original_duration: 20, edited_duration: 12,
  clips: [{key: 'a', original_start: 0}, {key: 'b', original_start: 10}],
  edited_timeline: [
    {clip: 0, original_start: 1, original_end: 4, edited_start: 0, edited_end: 3},
    {clip: 0, original_start: 6, original_end: 9, edited_start: 3, edited_end: 6},
    {clip: 1, original_start: 11, original_end: 17, edited_start: 6, edited_end: 12}],
  original_timeline: [{start: 0, end: 1}, {start: 1, end: 4, in_edit: true}, {start: 4, end: 6},
    {start: 6, end: 9, in_edit: true}, {start: 9, end: 11}, {start: 11, end: 17, in_edit: true}, {start: 17, end: 20}],
  pauses: [{start: 7, end: 7.8}]};
"""


@pytest.mark.parametrize("request_,view,expected", [
    ("{action:'CUT',kind:'range',start:1,end:2}", "edited", [2, 3]),
    ("{action:'CUT',kind:'range',start:3,end:5}", "edited", [6, 8]),        # at a join: the range after it
    ("{action:'CUT',kind:'range',start:2,end:5}", "original", [2, 5]),
    ("{action:'KEEP',kind:'range',start:2,end:4}", "edited", [4, 6]),
    ("{action:'KEEP',kind:'point',at:3.2}", "edited", [4, 6]),
    ("{action:'KEEP',kind:'point',at:5}", "original", [4, 6]),
    ("{action:'CUT',kind:'point',at:4.5}", "edited", [7, 7.8]),
    ("{action:'KEEP',kind:'edge',edge:'start'}", "edited", [0, 1]),
    ("{action:'KEEP',kind:'edge',edge:'end'}", "edited", [17, 20]),
    ("{action:'CUT',kind:'edge',edge:'start'}", "edited", [1, 1.5]),
])
def test_S06_comment_times_follow_the_timeline_on_screen(tmp_path, request_, view, expected):
    got = run(tmp_path, SEGMENTS + f"""const t = await import('./timemap.js');
        const r = t.resolveRequest({{timeline, request: {request_}, view: '{view}', speech: {{first: 0.5, last: 11}}}});
        return [r.start, r.end];""")
    assert got == pytest.approx(expected)


@pytest.mark.parametrize("request_", ["{action:'CUT',kind:'range',start:40,end:50}", "{action:'KEEP',kind:'point',at:10}",
                                      "{action:'CUT',kind:'point',at:11}"])
def test_S06_says_why_when_there_is_nothing_there(tmp_path, request_):
    got = run(tmp_path, SEGMENTS + f"""const t = await import('./timemap.js');
        return t.resolveRequest({{timeline, request: {request_}, view: 'edited', speech: null}});""")
    assert got["problem"] and "start" not in got


REBASE = """const m = await import('./remap.js');
const r = (key, from, to, at) => ({key, from, to, at, until: at + to - from});
const c = (s, e, t) => ({s, e, t});
"""


def test_edits_follow_a_reorder(tmp_path):
    got = run(tmp_path, REBASE + """return m.rebase([c(0, 2, 'MINE'), c(4, 6, 'theirs')],
        [r('a', 0, 3, 0), r('b', 0, 3, 3)], [r('b', 0, 3, 0), r('a', 0, 3, 3)], []);""")
    assert got == [{"s": 1, "e": 3, "t": "theirs"}, {"s": 3, "e": 5, "t": "MINE"}]


def test_edits_lose_only_what_was_cut(tmp_path):
    got = run(tmp_path, REBASE + """return m.rebase([c(0, 4, 'aa bb cc dd'), c(4, 6, 'gone'), c(6, 8, 'MINE')],
        [r('a', 0, 8, 0)], [r('a', 0, 2, 0), r('a', 6, 8, 2)], []);""")
    assert got == [{"s": 0, "e": 2, "t": "aa bb"}, {"s": 2, "e": 4, "t": "MINE"}]


def test_restored_and_added_footage_gets_fresh_captions_only_there(tmp_path):
    got = run(tmp_path, REBASE + """return m.rebase([c(0, 2, 'MINE')],
        [r('a', 0, 2, 0)], [r('a', 0, 4, 0), r('b', 0, 2, 4)], [c(0, 2, 'original'), c(2, 4, 'restored'), c(4, 6, 'added')]);""")
    assert [x["t"] for x in got] == ["MINE", "restored", "added"]


def test_a_removed_clip_takes_its_captions(tmp_path):
    got = run(tmp_path, REBASE + """return m.rebase([c(0, 2, 'one'), c(3, 5, 'two')],
        [r('a', 0, 3, 0), r('b', 0, 3, 3)], [r('b', 0, 3, 0)], []);""")
    assert got == [{"s": 0, "e": 2, "t": "two"}]
