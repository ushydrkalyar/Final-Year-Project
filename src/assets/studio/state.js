// The open project's settings (its clips are in clips.js).
import { emit } from './bus.js';
import { DEFAULTS, EDITABLE, cleanStyle, persistable } from './style.js';

export const st = structuredClone(DEFAULTS);

export const media = { duration: 0 }; // of what is playing

export function restore(saved) {
  const clean = persistable(cleanStyle(saved));
  // projects saved before clips had their own settings
  if (!('ratio' in saved) && saved.frame?.fmt) clean.ratio = saved.frame.fmt;
  for (const key of ['frame', 'rotate', 'trans']) delete clean[key];
  Object.assign(st, clean);
}

// A caption setting changed: the preview and the captions follow it.
//   layout:   words, lines, spacing and X apply on top of a fixed template; other edits switch it to Custom
//   rerender: the panel's markup depends on it
export function styleChanged({ layout = false, rerender = false } = {}) {
  emit('style:before');
  // not re-rendered for this: the slider being dragged would be destroyed under the pointer
  if (!layout && st.preset !== 'custom' && !EDITABLE[st.preset]) st.preset = 'custom';
  emit('style', { rerender });
}

// A setting that changes the cut, which applies on the next edit.
export function cutChanged() {
  emit('cut-settings');
}
