// One Undo for the whole project: caption edits, cuts, and clip changes. No DOM.
import { emit } from './bus.js';

const MAX_UNDO = 60;

let past = [];
let future = [];
let serial = 0;
let held = null; // the state from before a change that is still being made
let look = null; // the state from before a change of style that may still be going on
let lookTimer = null;
const LOOK_MS = 700; // a slider moved, then let go: one step

// Each entry is the project as it was. `id` lets a comment's Undo know it is still the last change.
export function push(state) {
  past.push({ id: ++serial, state });
  if (past.length > MAX_UNDO) past.shift();
  future = [];
  emit('history');
}

// A style change: the first of a burst remembers where it started, the burst is one step.
export function restyle(state) {
  look ??= state;
  clearTimeout(lookTimer);
  lookTimer = setTimeout(commitLook, LOOK_MS);
}

export function commitLook() {
  clearTimeout(lookTimer);
  lookTimer = null;
  const state = look;
  look = null;
  if (state) push(state);
}

export function hold(state) {
  held ??= state;
}

export function settle(changed) {
  const state = held;
  held = null;
  if (changed && state) push(state);
}

export function clear() {
  look = null;
  clearTimeout(lookTimer);
  past = [];
  future = [];
  held = null;
  emit('history');
}

export const canUndo = () => past.length > 0 || look !== null;
export const canRedo = () => future.length > 0;
export const top = () => past[past.length - 1]?.id ?? 0;

export function undo(now) {
  commitLook();
  const entry = past.pop();
  if (!entry) return null;
  future.push({ id: entry.id, state: now });
  emit('history');
  return entry.state;
}

export function redo(now) {
  const entry = future.pop();
  if (!entry) return null;
  past.push({ id: entry.id, state: now });
  emit('history');
  return entry.state;
}
