// DOM basics, and how markup reaches code without globals:
//   <button data-click="split">   <input data-input="wpc">   <div contenteditable data-blur="editCaption">
// runs the handler registered under that name with (element, event).

export const $ = id => document.getElementById(id);

export function fmt(seconds) {
  const s = Math.max(0, seconds || 0);
  const minutes = Math.floor(s / 60);
  const part = n => String(n).padStart(2, '0');
  return `${part(Math.floor(minutes / 60))}:${part(minutes % 60)}:${part(Math.floor(s % 60))}`;
}

export const mmss = seconds => fmt(seconds).slice(3);

const actions = new Map();

export function register(handlers) {
  for (const [name, handler] of Object.entries(handlers)) {
    if (actions.has(name)) throw new Error(`Two actions are called "${name}"`);
    actions.set(name, handler);
  }
}

// The innermost marked element wins, so a button inside a clickable row does only its own job.
function dispatch(type, event) {
  const bubbles = type !== 'blur' && type !== 'focus';
  const target = event.target instanceof Element ? event.target : null;
  const el = bubbles ? target?.closest(`[data-${type}]`) : target?.matches(`[data-${type}]`) ? target : null;
  if (!el || el.disabled) return;
  actions.get(el.dataset[type])?.(el, event);
}

export function listen() {
  for (const type of ['click', 'input', 'change', 'keydown']) {
    document.addEventListener(type, event => dispatch(type, event));
  }
  for (const type of ['blur', 'focus']) {
    document.addEventListener(type, event => dispatch(type, event), true); // these don't bubble
  }
}

export const options = (list, value) => list.map(item => {
  const [id, label] = Array.isArray(item) ? item : [item, item];
  return `<option value="${esc(id)}" ${id == value ? 'selected' : ''}>${esc(label)}</option>`;
}).join('');

export function saveAs(url, name) {
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
}
