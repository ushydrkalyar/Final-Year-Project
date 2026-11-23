// The screen's shape: the open tool, the inspector and the timeline's height, remembered per account.
import { $, register } from './dom.js';
import { emit } from './bus.js';

const MIN_TIMELINE = 200;
const MIN_WORK = 240;
const KEY_STEP = 24;

let key = '';
let saved = { tool: 'captions', inspector: true, timeline: 0 };

export const tool = () => saved.tool;

function store() {
  try {
    if (key) localStorage.setItem(key, JSON.stringify(saved));
  } catch { /* storage is full or blocked: the layout just isn't remembered */ }
}

function apply() {
  document.querySelectorAll('.rail [data-value]').forEach(button => {
    const open = button.dataset.value === saved.tool;
    button.classList.toggle('on', open);
    button.setAttribute('aria-pressed', open);
  });
  document.querySelectorAll('.drawer [data-tool]').forEach(panel => { panel.hidden = panel.dataset.tool !== saved.tool; });
  $('drawer').hidden = !saved.tool;
  $('inspector').hidden = !saved.inspector;
  if (saved.timeline) {
    const most = Math.max(MIN_TIMELINE, innerHeight - MIN_WORK);
    document.body.style.setProperty('--tlh', `${Math.min(most, Math.max(MIN_TIMELINE, saved.timeline))}px`);
  }
  emit('layout');
}

export function load(userId) {
  key = `cs_layout:${userId}`;
  try {
    saved = { ...saved, ...JSON.parse(localStorage.getItem(key) || '{}') };
  } catch { /* unreadable: the defaults */ }
  if (saved.tool && !document.querySelector(`[data-tool="${CSS.escape(String(saved.tool))}"]`)) saved.tool = 'captions';
  apply();
}

export function open(name) {
  saved.tool = name;
  store();
  apply();
}

function resize(height) {
  saved.timeline = Math.round(height);
  apply();
}

function enableSplitter() {
  const bar = $('vsplit');
  let drag = null;
  bar.addEventListener('pointerdown', event => {
    event.preventDefault();
    bar.setPointerCapture(event.pointerId);
    drag = { y: event.clientY, height: document.querySelector('.tl').offsetHeight };
    bar.classList.add('drag');
  });
  bar.addEventListener('pointermove', event => {
    if (drag) resize(drag.height + drag.y - event.clientY);
  });
  const end = () => {
    if (!drag) return;
    drag = null;
    bar.classList.remove('drag');
    store();
  };
  bar.addEventListener('pointerup', end);
  bar.addEventListener('pointercancel', end);
}

export function init() {
  enableSplitter();
  window.addEventListener('resize', () => saved.timeline && apply());
  register({
    openTool: el => open(saved.tool === el.dataset.value ? '' : el.dataset.value),
    toggleInspector: () => {
      saved.inspector = !saved.inspector;
      store();
      apply();
    },
    resizeTimeline: (el, event) => {
      const way = { ArrowUp: 1, ArrowDown: -1 }[event.key];
      if (!way) return;
      event.preventDefault();
      resize(document.querySelector('.tl').offsetHeight + way * KEY_STEP);
      store();
    },
  });
}
