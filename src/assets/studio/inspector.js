// The right side: the options of whatever is selected. One section is open at a time.
import { $, register } from './dom.js';

const views = new Map(); // kind -> () => [[id, title, body], ...]
const opened = new Map(); // kind -> id of its open section

let kind = 'text';

export function provide(name, view) {
  views.set(name, view);
}

export function render() {
  document.querySelectorAll('#itabs button').forEach(button => button.classList.toggle('on', button.dataset.value === kind));
  const body = $('rbody');
  const top = body.scrollTop;
  const sections = views.get(kind)?.() ?? [];
  const open = opened.has(kind) ? opened.get(kind) : sections.find(([id]) => id)?.[0];
  body.innerHTML = sections.map(([id, title, html]) => (id
    ? `<details class="acc" name="inspector"${id === open ? ' open' : ''}><summary data-click="openSection" data-value="${id}">${title}</summary>${html}</details>`
    : html)).join('');
  body.scrollTop = top;
}

export function show(name) {
  if (name === kind) return;
  kind = name;
  render();
}

export function leave(name) {
  if (name === kind) show('text');
}

export function init() {
  register({
    inspect: el => show(el.dataset.value),
    // the click comes before the toggle
    openSection: el => opened.set(kind, el.parentElement.open ? '' : el.dataset.value),
  });
}
