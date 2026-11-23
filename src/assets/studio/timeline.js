// The timeline: captions, clips, the AI's decisions and the sound, in edited or original time.
import { $, mmss, register } from './dom.js';
import { emit } from './bus.js';
import * as captions from './captions.js';
import * as clips from './clips.js';
import { NAMES } from './cuts.js';
import * as job from './job.js';
import * as player from './player.js';
import * as selection from './selection.js';
import { toEdited, toOriginal } from './timemap.js';

const MIN_CAPTION_S = 0.2;
const PEAK_COUNT = 600;
const JOIN_PX = 9; // a removed range has no width in edited time: it is a mark on its join
const ROUNDING_S = 0.06;

let zoom = 1;
let peaks = null;
let summary = '';
let mode = 'edited';
let ranges = []; // what the Cuts lane shows, by index

export const view = () => mode;
const data = () => job.edit()?.timeline;
const original = () => mode === 'original' && !!data();

const length = () => (original() ? data().original_duration : player.length()) || 1;
const shown = t => (original() ? toOriginal(data().edited_timeline, t, 'start') : t);
const percent = t => (t / length()) * 100;

function tickEvery(seconds) {
  return seconds > 600 ? 60 : seconds > 120 ? 30 : seconds > 60 ? 10 : seconds > 20 ? 5 : 2;
}

function setZoom(value) {
  zoom = value;
  const total = length();
  $('tlin').style.width = zoom <= 1 ? '100%' : `${Math.round($('tlw').clientWidth * zoom)}px`;
  const base = tickEvery(total);
  const step = zoom >= 6 ? Math.max(1, Math.round(base / 5)) : zoom >= 3 ? Math.max(1, Math.round(base / 2)) : base;
  let marks = '';
  for (let t = 0; t <= total; t += step) marks += `<span style="left:${percent(t)}%">${mmss(t)}</span>`;
  $('ruler').innerHTML = marks;
  drawWave();
  drawPlayhead();
}

export function drawPlayhead() {
  const lane = $('lCap');
  if (!lane.offsetWidth || $('ph').style.display === 'none') return;
  const at = Math.min(1, shown(player.video.currentTime || 0) / length());
  $('ph').style.left = `${lane.offsetLeft + at * lane.offsetWidth}px`;
}

export function drawStat() {
  const count = captions.list().length;
  const label = `${count}${count === 1 ? ' caption' : ' captions'}`;
  $('tstat').textContent = summary ? `${summary} · ${label}` : label;
}

export function drawBlocks() {
  const selected = captions.selection();
  const fixed = original(); // in original time a caption can straddle a cut: drag it in Edited
  const kept = data()?.edited_timeline ?? [];
  const handles = side => (fixed ? '' : `<b class="hd ${side}"></b>`);
  const spans = data()?.clips ?? [];
  const hidden = new Set(clips.hiddenIn(spans));
  $('lCap').innerHTML = captions.list().map((c, i) => {
    const middle = (c.s + c.e) / 2;
    const owner = spans.findLastIndex(span => middle >= span.edited_start);
    const off = hidden.has(Math.max(0, owner));
    const s = fixed ? toOriginal(kept, c.s, 'start') : c.s, e = fixed ? toOriginal(kept, c.e, 'end') : c.e;
    return `<div class="blk${i === selected ? ' sel' : ''}${off ? ' off' : ''}" data-i="${i}" style="left:${percent(s)}%;width:${Math.max(0.15, percent(e - s))}%" title="${esc(c.t)}">${handles('l')}<span>${esc(c.t)}</span>${handles('r')}</div>`;
  }).join('');
}

// Filmstrip frames come from the original. In edited time: the ones that survived.
function drawClips() {
  const timeline = data();
  if (!timeline) {
    $('lVid').innerHTML = '<div class="film empty"></div>';
    return;
  }
  const spans = timeline.clips;
  $('lVid').innerHTML = spans.map((clip, index) => {
    const start = clip[`${mode}_start`];
    let end = clip[`${mode}_end`];
    if (index < spans.length - 1) end = Math.min(end, spans[index + 1][`${mode}_start`]); // under a transition
    const frames = timeline.filmstrip.filter(f => f.clip === index && (original()
      || timeline.edited_timeline.some(g => f.timestamp >= g.original_start && f.timestamp <= g.original_end)));
    const pictures = frames.map(f => `<img alt="" src="data:image/jpeg;base64,${f.data}">`).join('');
    return `<div class="film${pictures ? '' : ' empty'}" data-clip="${safeId(clip.key)}" title="${esc(clip.name)}" style="left:${percent(start)}%;width:${percent(end - start)}%;right:auto">${pictures}<span class="clipname">${index + 1}. ${esc(clip.name)}</span></div>`;
  }).join('');
}

function decisionRanges(timeline) {
  const kept = timeline.original_timeline.filter(segment => segment.in_edit);
  const stillIn = p => kept.some(k => p.start >= k.start - ROUNDING_S && p.end <= k.end + ROUNDING_S);
  return [
    ...kept,
    ...timeline.pauses.filter(p => p.decision === 'KEEP' && stillIn(p))
      .map(p => ({ start: p.start, end: p.end, clip: p.clip, type: 'pause_kept', reason: `Pause kept: ${p.reason}`, in_edit: true })),
    ...timeline.original_timeline.filter(segment => !segment.in_edit),
  ];
}

function drawDecisions() {
  const timeline = data();
  ranges = timeline ? decisionRanges(timeline) : [];
  if (!timeline) {
    $('lCut').innerHTML = '';
    return;
  }
  const kept = timeline.edited_timeline;
  const chosen = selection.selected();
  $('lCut').innerHTML = ranges.map((range, i) => {
    const join = !original() && !range.in_edit;
    // in edited time each kept range is drawn where it plays (clips can overlap under a transition)
    const own = !original() && range.in_edit
      ? kept.filter(k => k.clip === range.clip && k.original_start < range.end && k.original_end > range.start) : [];
    if (!original() && range.in_edit && !own.length) return '';
    let place;
    if (original()) {
      place = `left:${percent(range.start)}%;width:${percent(range.end - range.start)}%`;
    } else if (join) {
      const at = percent(toEdited(kept, range.start));
      place = `left:${at}%;width:${JOIN_PX}px;transform:translateX(-${at}%)`; // whole, even at either end
    } else {
      const first = own[0], last = own[own.length - 1];
      const from = first.edited_start + Math.max(0, range.start - first.original_start);
      const to = Math.min(last.edited_end, last.edited_start + range.end - last.original_start);
      place = `left:${percent(from)}%;width:${percent(to - from)}%`;
    }
    const name = NAMES[range.type] || range.type;
    const why = range.in_edit && range.type === 'speech_kept' ? range.reason : `${name}, ${(range.end - range.start).toFixed(2)}s: ${range.reason}`;
    const picked = chosen && chosen.start === range.start && chosen.end === range.end && chosen.type === range.type;
    return `<button class="cut${join ? ' join' : ''}${picked ? ' sel' : ''}" data-click="selectRange" data-r="${i}" data-type="${esc(range.type)}" title="${esc(why)}" aria-label="${esc(why)}" style="${place};background:${esc(timeline.colour_map[range.type] || '#888')}">${join ? '' : `<span>${esc(name)}</span>`}</button>`;
  }).join('');
}

export function markRange() {
  const chosen = selection.selected();
  document.querySelectorAll('.cut').forEach(el => {
    const range = ranges[+el.dataset.r];
    el.classList.toggle('sel', !!chosen && range?.start === chosen.start && range?.end === chosen.end && range?.type === chosen.type);
  });
  $('tRestore').disabled = !chosen || chosen.in_edit || job.busy();
}

function draw() {
  drawBlocks();
  drawClips();
  drawDecisions();
  setZoom(zoom);
}

export function build() {
  const edit = job.edit();
  if (!edit) return;
  const { timeline } = edit;
  $('tlmsg').style.display = 'none';
  $('ph').style.display = 'block';
  $('tAdd').disabled = false;
  summary = timeline
    ? `${timeline.edited_duration?.toFixed(1)}s edited · ${timeline.stats?.total_removed_s ?? 0}s removed` : '';
  draw();
  drawStat();
  readPeaks(edit.blob);
}

async function readPeaks(blob) {
  try {
    const context = new (window.AudioContext || window.webkitAudioContext)();
    const audio = await context.decodeAudioData(await blob.arrayBuffer());
    const samples = audio.getChannelData(0);
    const size = Math.floor(samples.length / PEAK_COUNT);
    const found = [];
    for (let i = 0; i < PEAK_COUNT; i++) {
      let peak = 0;
      for (let j = 0; j < size; j += Math.max(1, size >> 6)) peak = Math.max(peak, Math.abs(samples[i * size + j] || 0));
      found.push(peak);
    }
    context.close();
    if (job.edit()?.blob !== blob) return; // a newer edit replaced it meanwhile
    const loudest = Math.max(...found) || 1;
    peaks = found.map(peak => peak / loudest);
  } catch {
    peaks = null;
  }
  drawWave();
}

export function drawWave() {
  const canvas = $('wave');
  if (!canvas.parentElement.offsetWidth) return;
  canvas.width = canvas.parentElement.offsetWidth;
  canvas.height = canvas.parentElement.offsetHeight;
  const g = canvas.getContext('2d');
  g.clearRect(0, 0, canvas.width, canvas.height);
  if (!job.hasResult() || !peaks) return;
  g.fillStyle = 'rgba(116,245,184,.75)';
  const edited = player.length() || 1, total = length();
  const width = Math.max(1, (edited / peaks.length / total) * canvas.width - 1);
  peaks.forEach((level, i) => {
    const height = Math.max(2, level * canvas.height * 0.85);
    // in original time, sound is drawn where it was recorded; removed ranges stay empty
    const x = (shown((i / peaks.length) * edited) / total) * canvas.width;
    g.fillRect(x, (canvas.height - height) / 2, width, height);
  });
}

function enableDrag() {
  let drag = null;
  document.addEventListener('pointerdown', event => {
    const block = event.target.closest?.('.blk');
    if (!block || !$('lCap').contains(block)) return;
    const index = +block.dataset.i;
    const caption = captions.list()[index];
    if (!caption) return;
    const { classList } = event.target;
    drag = {
      index, block, caption,
      fixed: original() || job.busy(),
      mode: classList.contains('l') ? 'start' : classList.contains('r') ? 'end' : 'move',
      x: event.clientX, s: caption.s, e: caption.e,
      length: player.length() || 1,
      width: $('lCap').getBoundingClientRect().width,
      before: captions.list().map(c => ({ ...c })),
      moved: false,
    };
    block.setPointerCapture?.(event.pointerId);
    captions.select(index);
    event.preventDefault();
  });
  document.addEventListener('pointermove', event => {
    if (!drag || drag.fixed) return;
    const dx = event.clientX - drag.x;
    if (Math.abs(dx) > 2) drag.moved = true;
    if (!drag.moved) return;
    const { caption, length: total } = drag;
    const list = captions.list();
    const low = list[drag.index - 1]?.e ?? 0;
    const high = list[drag.index + 1]?.s ?? total;
    const delta = (dx / drag.width) * total;
    if (drag.mode === 'move') {
      const span = drag.e - drag.s;
      caption.s = Math.min(Math.max(drag.s + delta, low), high - span);
      caption.e = caption.s + span;
    } else if (drag.mode === 'start') {
      caption.s = Math.min(Math.max(drag.s + delta, low), drag.e - MIN_CAPTION_S);
    } else {
      caption.e = Math.max(Math.min(drag.e + delta, high), drag.s + MIN_CAPTION_S);
    }
    drag.block.style.left = `${percent(caption.s)}%`;
    drag.block.style.width = `${Math.max(0.15, percent(caption.e - caption.s))}%`;
    player.seek(drag.mode === 'end' ? caption.e - 0.01 : caption.s + 0.01);
  });
  document.addEventListener('pointerup', () => {
    if (!drag) return;
    const { moved, before } = drag;
    drag = null;
    const caption = captions.list()[captions.selection()];
    if (moved) captions.moved(before);
    else if (caption) player.seek(caption.s + 0.01);
  });
}

export function init() {
  enableDrag();
  player.video.addEventListener('timeupdate', drawPlayhead);
  register({
    zoomTimeline: el => setZoom(+el.value),
    setView: el => {
      mode = el.dataset.value;
      document.querySelectorAll('#tView button').forEach(button => button.classList.toggle('on', button === el));
      if (job.hasResult()) draw();
      emit('timeline:view', mode);
    },
    selectRange: el => {
      const range = ranges[+el.dataset.r];
      if (!range) return;
      captions.select(-1);
      selection.select(range);
      player.seek(toEdited(data().edited_timeline, range.start));
    },
    seekTimeline: (el, event) => {
      if (event.target.closest('.blk')) return;
      const film = event.target.closest('[data-clip]');
      if (film) emit('clip:picked', film.dataset.clip);
      const lane = $('lCap').getBoundingClientRect();
      if (event.clientX < lane.left || event.clientX > lane.right || !player.video.duration) return;
      const at = Math.min(1, (event.clientX - lane.left) / lane.width) * length();
      player.seek(original() ? toEdited(data().edited_timeline, at) : at);
    },
  });
}
