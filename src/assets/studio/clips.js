// The project's clips, in playing order, each with its own rotation, framing and transition. No DOM.
import { emit } from './bus.js';
import { st } from './state.js';
import { pipeSignature } from './style.js';

export const MAX = 10; // the server refuses more

export const autoFrame = () => ({ mode: 'auto', cx: 0.5, cy: 0.5, zoom: 1 });
export const newId = () => `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;

let items = []; // [{id, file, name, rotate, frame, trans, dur, w, h, url, thumb}]
let selected = null;

export const all = () => items;
export const count = () => items.length;
export const byId = id => items.find(clip => clip.id === id) ?? null;
export const current = () => byId(selected) ?? items[0] ?? null;
export const signature = () => JSON.stringify([pipeSignature(st), items.map((clip, i) =>
  [clip.id, clip.rotate, frameFields(clip), i < items.length - 1 ? clip.trans : ''])]);

export function make({ id = newId(), file, name = file.name, saved = {} }) {
  return {
    id, file, name,
    rotate: [90, 180, 270].includes(+saved.rotate) ? +saved.rotate : 0,
    frame: { ...autoFrame(), ...saved.frame },
    trans: saved.trans || 'auto',
    captions: ['on', 'off'].includes(saved.captions) ? saved.captions : 'auto', // auto: not over captions already in the picture
    fresh: false, // just added
    dur: saved.dur || 0,
    w: 0, h: 0, url: '', thumb: '',
  };
}

export function kind(clip) {
  if (!clip?.w) return '';
  const sideways = clip.rotate % 180 !== 0;
  const w = sideways ? clip.h : clip.w, h = sideways ? clip.w : clip.h;
  return w > h * 1.05 ? 'landscape' : h > w * 1.05 ? 'portrait' : 'square';
}

// A custom frame with no ratio picked takes the clip's own shape.
export const shapeOf = clip => st.ratio || { portrait: 'vertical', square: 'square' }[kind(clip)] || 'landscape';

// A clip's framing as the server takes it. Nothing, when it keeps its own shape.
export function frameFields(clip) {
  const custom = clip.frame.mode === 'custom';
  if (!custom) return st.ratio ? { format: st.ratio, mode: 'auto' } : {};
  return {
    format: shapeOf(clip), mode: 'custom',
    cx: +(+clip.frame.cx).toFixed(4), cy: +(+clip.frame.cy).toFixed(4), zoom: +(+clip.frame.zoom).toFixed(2),
  };
}

export function reset(list) {
  items = list;
  if (!byId(selected)) selected = items[0]?.id ?? null;
}

export function select(id) {
  if (!byId(id) || id === selected) return;
  selected = id;
  emit('clips:selected', byId(id));
}

// A change of which clips there are, or how they follow one another: the join changes.
function restructure(next) {
  emit('project:before');
  reset(next);
  emit('clips:structure');
}

export function add(made) {
  const room = MAX - items.length;
  if (made.length > room) return `A project holds up to ${MAX} clips.`;
  for (const clip of made) clip.fresh = true;
  restructure([...items, ...made]);
  return '';
}

export function remove(id) {
  if (items.length < 2) return 'A project needs at least one clip. Replace it instead.';
  restructure(items.filter(clip => clip.id !== id));
  return '';
}

export function move(id, to) {
  const from = items.findIndex(clip => clip.id === id);
  const target = Math.max(0, Math.min(items.length - 1, to));
  if (from < 0 || from === target) return;
  const next = [...items];
  next.splice(target, 0, ...next.splice(from, 1));
  restructure(next);
}

export function replace(id, made) {
  const next = items.map(clip => (clip.id === id ? { ...made, trans: clip.trans } : clip));
  selected = made.id;
  restructure(next);
}

export function setTransition(id, trans) {
  restructure(items.map(clip => (clip.id === id ? { ...clip, trans } : clip)));
}

export function rotate(id, degrees) {
  const clip = byId(id);
  if (!clip) return;
  clip.rotate = degrees % 360;
  clip.frame = autoFrame(); // a dragged frame was placed on the old picture
}

// A clip added beside clips that were all turned the same way, filmed at the same size, is turned too.
// Returns how many were.
export function inheritTurn() {
  const added = items.filter(clip => clip.fresh), before = items.filter(clip => !clip.fresh);
  for (const clip of added) clip.fresh = false;
  const like = before[0];
  if (!like?.rotate || before.some(clip => clip.rotate !== like.rotate)) return 0;
  const same = added.filter(clip => clip.w && clip.w === like.w && clip.h === like.h);
  for (const clip of same) clip.rotate = like.rotate;
  return same.length;
}

export function setCaptions(id, value) {
  const clip = byId(id);
  if (!clip) return;
  clip.captions = value;
  emit('clips:captions');
}

export const hidesCaptions = span => {
  const choice = byId(span?.key)?.captions ?? 'auto';
  return choice === 'off' || (choice === 'auto' && !!span?.burned_captions);
};

// Which clips of an edit (by position) get no captions in the picture.
export const hiddenIn = spans => (spans ?? []).flatMap((span, i) => (hidesCaptions(span) ? [i] : []));

// What the browser measured.
export function measured(id, facts) {
  const clip = byId(id);
  if (clip) Object.assign(clip, facts);
}

// Undo: the clips as they were.
export const snapshot = () => items.map(clip => ({ ...clip, frame: { ...clip.frame } }));

export const saved = () =>
  items.map(({ id, name, rotate, frame, trans, captions, dur }) => ({ id, name, rotate, frame, trans, captions, dur }));
