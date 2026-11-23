// Output shape, following the speaker or a dragged custom frame.
import { $, register } from './dom.js';
import { emit } from './bus.js';
import * as clips from './clips.js';
import * as job from './job.js';
import { video } from './player.js';
import { st } from './state.js';

const ASPECT = { vertical: 9 / 16, landscape: 16 / 9, square: 1 };

let editing = false; // moving the frame over the original footage of a finished edit

const clip = () => clips.current();
const frame = () => clip().frame;

export const format = () => clips.shapeOf(clip());
export const aspect = () => ASPECT[format()];

// Is the selected clip reshaped at all? "Original" keeps it as it is.
export function active() {
  if (!clip()?.w) return false;
  if (frame().mode === 'custom') return true;
  if (!ASPECT[st.ratio]) return false;
  const natural = { vertical: 'portrait', landscape: 'landscape', square: 'square' }[st.ratio];
  return clips.kind(clip()) !== natural;
}

// Auto-follow crops sideways; the other way round, the whole picture goes on a backdrop.
const follows = () => format() === 'vertical' || (format() === 'square' && clips.kind(clip()) === 'landscape');

// The frame as a fraction of the source picture (the same maths as reframe.py).
function frameWindow() {
  const turned = !job.hasResult() && clip().rotate % 180 !== 0;
  const w = turned ? clip().h : clip().w, h = turned ? clip().w : clip().h;
  const shape = aspect();
  const wide = w / h >= shape;
  const width = wide ? h * shape : w;
  const height = wide ? h : w / shape;
  const zoom = Math.max(1, +frame().zoom || 1);
  return { w: Math.min(1, width / zoom / w), h: Math.min(1, height / zoom / h) };
}

const element = () => (editing && job.hasResult() ? $('vsrc') : video);
const guided = () => active() && (frame().mode === 'custom' || follows());
const visible = () => !!clip()?.w && !job.busy() && guided() && (!job.hasResult() || editing);

function clamp() {
  const size = frameWindow();
  frame().cx = Math.min(1 - size.w / 2, Math.max(size.w / 2, frame().cx));
  frame().cy = Math.min(1 - size.h / 2, Math.max(size.h / 2, frame().cy));
}

// Where the finished picture sits inside the preview (the caption preview is placed there).
export function viewRect() {
  if (!visible()) return video.getBoundingClientRect();
  const box = element().getBoundingClientRect();
  clamp();
  const size = frameWindow();
  return {
    left: box.left + (frame().cx - size.w / 2) * box.width,
    top: box.top + (frame().cy - size.h / 2) * box.height,
    width: size.w * box.width,
    height: size.h * box.height,
  };
}

export function draw() {
  const overlay = $('fedit'), tools = $('ftool'), shown = visible();
  const footage = editing && job.hasResult();
  overlay.style.display = shown ? 'block' : 'none';
  tools.style.display = shown ? 'flex' : 'none';
  $('vsrc').style.display = footage ? 'block' : 'none';
  if (!shown) return;
  const stage = $('stage').getBoundingClientRect();
  const box = element().getBoundingClientRect();
  if (!box.width) return;
  Object.assign(overlay.style, {
    left: `${box.left - stage.left}px`, top: `${box.top - stage.top}px`,
    width: `${box.width}px`, height: `${box.height}px`,
  });
  clamp();
  const size = frameWindow();
  const custom = frame().mode === 'custom';
  Object.assign($('fwin').style, {
    left: `${(frame().cx - size.w / 2) * 100}%`, top: `${(frame().cy - size.h / 2) * 100}%`,
    width: `${size.w * 100}%`, height: `${size.h * 100}%`,
  });
  $('fbadge').textContent = custom ? 'Custom frame' : 'Follows the speaker';
  $('fhint').textContent = custom ? 'Drag the frame to position it' : 'Drag the frame to adjust it yourself';
  $('fzoom').value = frame().zoom;
  $('fauto').style.display = custom ? 'inline-flex' : 'none';
  $('fdone').style.display = editing ? 'inline-flex' : 'none';
  $('fscrubw').style.display = footage ? 'flex' : 'none';
}

const changed = () => emit('frame');
const moving = () => emit('frame:moving');

export function closeEditor() {
  if (!editing) return;
  editing = false;
  const source = $('vsrc');
  source.pause();
  source.removeAttribute('src');
  source.load();
  video.style.display = 'block';
}

function customise() {
  if (!clip()?.w) return toast('Upload a video first');
  frame().mode = 'custom';
  if (job.hasResult()) { // show the original footage again, to move the frame over it
    editing = true;
    video.pause();
    video.style.display = 'none';
    const source = $('vsrc');
    source.onloadedmetadata = () => {
      source.currentTime = Math.min(1, (source.duration || 3) / 3);
      moving();
    };
    source.src = clip().url;
  }
  changed();
  toast('Drag the frame over the video to position it');
}

export function section() {
  if (!clip()) return '';
  const custom = frame().mode === 'custom';
  let note = 'Keeps the shape it was filmed in. Pick another with the ratio menu at the top.';
  if (custom) note = 'The frame stays exactly where you put it. Auto-follow keeps the speaker centred instead.';
  else if (active()) note = follows() ? 'The frame follows the speaker so they stay in the centre.' : 'The whole picture is kept, on a soft blurred backdrop.';
  const customise = clip().rotate % 180 ? '' : '<button class="btn" data-click="customiseFrame">Customise framing</button>';
  const auto = custom ? '<button class="btn" data-click="autoFrame">Auto-follow</button>' : '';
  return `<p class="note">${note}</p><div class="row">${customise}${auto}</div>`;
}

// Dragging the frame on the preview; the first drag switches to a custom frame.
function enableDrag() {
  const win = $('fwin');
  let drag = null;
  win.addEventListener('pointerdown', event => {
    if (!visible()) return;
    event.preventDefault();
    win.setPointerCapture(event.pointerId);
    const box = element().getBoundingClientRect();
    drag = { x: event.clientX, y: event.clientY, cx: frame().cx, cy: frame().cy, box, moved: false };
    win.classList.add('drag');
  });
  win.addEventListener('pointermove', event => {
    if (!drag) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    if (!drag.moved) return;
    frame().mode = 'custom';
    frame().cx = drag.cx + dx / drag.box.width;
    frame().cy = drag.cy + dy / drag.box.height;
    clamp();
    moving();
  });
  const end = () => {
    if (!drag) return;
    const { moved } = drag;
    drag = null;
    win.classList.remove('drag');
    if (moved) changed();
  };
  win.addEventListener('pointerup', end);
  win.addEventListener('pointercancel', end);
}

export function init() {
  enableDrag();
  register({
    setFormat: el => {
      closeEditor();
      st.ratio = el.value;
      for (const each of clips.all()) each.frame = clips.autoFrame(); // a dragged frame was made for the old shape
      changed();
    },
    autoFrame: () => {
      Object.assign(frame(), clips.autoFrame());
      changed();
    },
    customiseFrame: customise,
    doneFraming: () => {
      closeEditor();
      changed();
    },
    zoomFrame: el => {
      frame().zoom = +el.value;
      frame().mode = 'custom';
      clamp();
      moving();
    },
    zoomFrameDone: changed,
    scrubFootage: el => {
      const source = $('vsrc');
      if (source.duration) source.currentTime = source.duration * (+el.value / 1000);
    },
  });
}
