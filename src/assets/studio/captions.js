// The caption list, its edits, and the live caption track's requests. No DOM.
import { emit } from './bus.js';
import * as clips from './clips.js';
import * as job from './job.js';
import { st } from './state.js';
import { styleFields } from './style.js';

const GATHER_MS = 50; // changes closer together than this share a request

let items = []; // [{s, e, t}] on the edited timeline
let edited = false; // the user changed text or timing: the server renders the list as given
let groupedAt = null; // words-per-caption the list was grouped with
let selected = -1;
let epoch = 0; // another edit took over
let wanted = 0; // the newest change
let sending = false;
let timer = null;
let waiting = [];

export const list = () => items;
export const isEdited = () => edited;
export const selection = () => selected;
export const snapshot = () => ({ items: items.map(c => ({ ...c })), edited, groupedAt });

export function parseSrt(text) {
  const out = [];
  for (const block of text.replace(/\r/g, '').split(/\n\n+/)) {
    const lines = block.trim().split('\n');
    const timing = lines.find(line => line.includes('-->'));
    if (!timing) continue;
    const [s, e] = timing.split('-->').map(part => {
      const [h, m, sec] = part.trim().replace(',', '.').split(':');
      return +h * 3600 + +m * 60 + +sec;
    });
    const t = lines.slice(lines.indexOf(timing) + 1).join(' ').replace(/<[^>]+>/g, '').replace(/\{[^}]*\}/g, '').trim();
    if (t) out.push({ s, e, t });
  }
  return out;
}

export const wire = () => JSON.stringify(items.map(c => ({ s: +c.s.toFixed(3), e: +c.e.toFixed(3), t: c.t })));

export function fields() {
  const form = styleFields(st, new FormData());
  const hidden = clips.hiddenIn(job.edit()?.timeline?.clips);
  if (hidden.length) form.append('hidden_clips', hidden.join(','));
  if (edited) {
    form.append('captions', wire());
    form.append('edited', 1);
    form.append('respect', st.wpc === groupedAt ? 1 : 0);
  }
  return form;
}

// A new edit arrived: start from its captions, or from the edits saved with the project.
export function reset(saved) {
  selected = -1;
  groupedAt = st.wpc;
  edited = !!saved;
  if (saved) items = saved;
}

// The list carried into a new cut, or brought back by Undo.
export function adopt(next) {
  items = next.items.map(c => ({ ...c }));
  edited = next.edited;
  groupedAt = next.groupedAt ?? st.wpc;
  selected = -1;
}

// Whatever is on its way belongs to the edit being replaced.
export function invalidate() {
  epoch++;
  clearTimeout(timer);
  timer = null;
  if (!sending) answer();
}

function answer() {
  for (const done of waiting.splice(0)) done();
}

// One request in flight, always followed by one for the newest change. A slider being dragged updates
// the preview as fast as the server answers, rather than waiting for it to stop.
export function refresh(now = false) {
  wanted++;
  const done = new Promise(resolve => waiting.push(resolve));
  schedule(now);
  return done;
}

function schedule(now) {
  if (sending || (timer && !now)) return;
  clearTimeout(timer);
  timer = setTimeout(send, now ? 0 : GATHER_MS);
}

async function send() {
  timer = null;
  sending = true;
  const mine = wanted, from = epoch;
  await load(() => from !== epoch, () => mine === wanted);
  sending = false;
  if (mine === wanted) answer();
  else schedule(true);
}

async function load(replaced, newest) {
  const id = job.editId();
  if (!id || job.busy()) return;
  const stale = () => replaced() || job.busy() || id !== job.editId();
  try {
    const track = await apiJson(`/captions/${id}`, { method: 'POST', body: fields(), fallback: 'Caption update failed' });
    if (stale()) return;
    const regrouped = newest() && (!edited || st.wpc !== groupedAt); // an overtaken answer never replaces the list
    if (regrouped) {
      items = parseSrt(track.srt);
      groupedAt = st.wpc;
    }
    emit('captions:track', { ass: track.ass, boxes: track.boxes ?? [], regrouped });
  } catch (error) {
    if (stale()) return;
    if (error.status === 404 || error.status === 409) {
      job.expire();
      emit('captions:expired');
    } else {
      emit('notice', error.message);
    }
  }
}

export function select(index) {
  selected = index;
  emit('captions:selected', index);
}

function remember(before = snapshot()) {
  emit('project:before', { captions: before });
}

function changed(keepList = false) {
  edited = true;
  emit('captions:edited', { keepList });
}

export function setText(index, text) {
  const caption = items[index];
  if (!caption || text === caption.t) return;
  remember();
  if (text) {
    caption.t = text;
  } else {
    items.splice(index, 1);
    selected = -1;
  }
  changed();
}

export function remove(index) {
  if (!items[index]) return;
  remember();
  items.splice(index, 1);
  selected = -1;
  changed();
}

// Returns why it couldn't, or ''.
export function add(at, length) {
  let s = Math.min(at, Math.max(0, length - 0.3));
  const inside = items.find(c => s >= c.s && s < c.e);
  if (inside) s = inside.e;
  const next = items.find(c => c.s >= s);
  const e = Math.min(length, s + 1.5, next ? next.s : length);
  if (e - s < 0.2) return 'No free space at the playhead';
  remember();
  const caption = { s, e, t: 'New caption' };
  items.push(caption);
  items.sort((a, b) => a.s - b.s);
  selected = items.indexOf(caption);
  changed();
  return '';
}

export function split(at) {
  const index = items.findIndex(c => at > c.s + 0.1 && at < c.e - 0.1);
  if (index < 0) return 'Put the playhead inside a caption to split it';
  const caption = items[index];
  const words = caption.t.split(/\s+/);
  if (words.length < 2) return 'Only one word — nothing to split';
  const share = (at - caption.s) / (caption.e - caption.s);
  const k = Math.min(words.length - 1, Math.max(1, Math.round(words.length * share)));
  remember();
  items.splice(index, 1,
    { s: caption.s, e: at, t: words.slice(0, k).join(' ') },
    { s: at, e: caption.e, t: words.slice(k).join(' ') });
  selected = index;
  changed();
  return '';
}

// The result of a typed request ("fix spelling of ...").
export function replaceAll(next, index) {
  remember();
  items = next.map(c => ({ s: +c.s, e: +c.e, t: c.t }));
  selected = index != null && items.length ? Math.min(index, items.length - 1) : -1;
  changed();
}

// A timeline drag mutates the caption as it moves; `before` is the list from when it began.
export function moved(before) {
  remember({ items: before, edited, groupedAt });
  changed();
}
