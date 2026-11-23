// Dragging the captions on the preview.
//
// While dragged, the drawn layer itself moves (a transform: no request, no re-render), so box and text
// are one thing. On release the new position goes to the server, and the layer keeps its offset until
// the track drawn there is painted, so nothing snaps back.
import { $ } from './dom.js';
import { emit } from './bus.js';
import { st, styleChanged } from './state.js';
import { isStyled } from './style.js';

const MARGIN = 0.015; // caption_layout.MARGIN: the export is kept in the same bounds
const SNAP = 0.012; // of the width, to the centre
const SAMPLE_PX = 320; // the layer is measured at this width
const INK = 40; // alpha
const MEASURE_MS = 120;
const GIVE_UP_MS = 4000; // if no new track ever comes
const PAD = 6;

let source = () => null; // the element the captions are drawn on, while they can be moved
let boxOf = () => null; // the caption on screen, as the server placed it: {l, t, r, b, hx, hy}
let drag = null;
let shift = null; // {el, x, y}: how far the layer is moved, in px
let ink = null;
let measureTimer = null;
let giveUp = null;

const sample = document.createElement('canvas');

export function follow(getLayer, getBox) {
  source = getLayer;
  boxOf = getBox;
}

// The drawn pixels' box, in fractions of the layer. null = nothing is drawn right now.
function measure(el) {
  const w = el.width || el.naturalWidth, h = el.height || el.naturalHeight;
  if (!w || !h) return null;
  sample.width = SAMPLE_PX;
  sample.height = Math.max(1, Math.round((SAMPLE_PX * h) / w));
  const g = sample.getContext('2d', { willReadFrequently: true });
  g.clearRect(0, 0, sample.width, sample.height);
  let pixels;
  try {
    g.drawImage(el, 0, 0, sample.width, sample.height);
    pixels = g.getImageData(0, 0, sample.width, sample.height).data;
  } catch {
    return null; // nothing decoded to read yet
  }
  let left = sample.width, top = sample.height, right = -1, bottom = -1;
  for (let y = 0; y < sample.height; y++) {
    for (let x = 0; x < sample.width; x++) {
      if (pixels[(y * sample.width + x) * 4 + 3] <= INK) continue;
      if (x < left) left = x;
      if (x > right) right = x;
      if (y < top) top = y;
      bottom = y;
    }
  }
  return right < 0 ? null : {
    left: left / sample.width, top: top / sample.height,
    right: (right + 1) / sample.width, bottom: (bottom + 1) / sample.height,
  };
}

// The layer's box as if it weren't moved.
function home(el) {
  const box = el.getBoundingClientRect();
  const moved = shift?.el === el ? shift : { x: 0, y: 0 };
  return { left: box.left - moved.x, top: box.top - moved.y, width: box.width, height: box.height };
}

export function place() {
  if (drag) return;
  const handle = $('grab');
  const el = source();
  handle.hidden = !el || !ink;
  if (handle.hidden) return;
  const stage = $('stage').getBoundingClientRect();
  const box = home(el);
  Object.assign(handle.style, {
    left: `${box.left - stage.left + ink.left * box.width - PAD}px`,
    top: `${box.top - stage.top + ink.top * box.height - PAD}px`,
    width: `${(ink.right - ink.left) * box.width + 2 * PAD}px`,
    height: `${(ink.bottom - ink.top) * box.height + 2 * PAD}px`,
    transform: shift?.el === el ? `translate(${shift.x}px,${shift.y}px)` : '',
  });
}

export function update() {
  if (drag) return;
  const el = source();
  ink = el ? measure(el) : null;
  place();
}

// The captions were painted. Measured at most every MEASURE_MS while playing.
export function changed() {
  if (measureTimer) return;
  measureTimer = setTimeout(() => {
    measureTimer = null;
    update();
  }, MEASURE_MS);
}

// The track for the new position is on screen (or the layer is gone).
export function settled() {
  if (drag || !shift) return;
  clearTimeout(giveUp);
  shift.el.style.transform = '';
  shift = null;
  update();
}

function start(event, handle) {
  const el = source();
  if (!el || event.button !== 0) return;
  const box = measure(el);
  if (!box) return;
  event.preventDefault();
  handle.setPointerCapture(event.pointerId);
  clearTimeout(giveUp);
  ink = box;
  place();
  const at = shift?.el === el ? shift : { x: 0, y: 0 };
  const size = home(el);
  // a second drag can start before the last one's track arrives: its box is moved by the pending offset
  const known = boxOf();
  const bound = known ? { left: known.l, top: known.t, right: known.r, bottom: known.b } : box;
  const held = known && !at.x && !at.y ? { x: known.hx, y: known.hy } : { x: 0, y: 0 };
  drag = {
    el, box: bound, held, size, px: event.clientX, py: event.clientY, from: { x: at.x, y: at.y },
    x: at.x, y: at.y, moved: false, centre: false,
    low: { x: (MARGIN - bound.left) * size.width, y: (MARGIN - bound.top) * size.height },
    high: { x: (1 - MARGIN - bound.right) * size.width, y: (1 - MARGIN - bound.bottom) * size.height },
  };
  el.style.willChange = 'transform';
  handle.classList.add('drag');
  emit('inspect', 'text');
}

const within = (value, low, high) => (low > high ? 0 : Math.min(high, Math.max(low, value)));

function move(event, handle) {
  if (!drag) return;
  const dx = event.clientX - drag.px, dy = event.clientY - drag.py;
  if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
  drag.moved = true;
  let x = within(drag.from.x + dx, drag.low.x, drag.high.x);
  const middle = (drag.box.left + drag.box.right) / 2 + x / drag.size.width;
  drag.centre = Math.abs(middle - 0.5) < SNAP;
  if (drag.centre) x += (0.5 - middle) * drag.size.width;
  drag.x = x;
  drag.y = within(drag.from.y + dy, drag.low.y, drag.high.y);
  shift = { el: drag.el, x: drag.x, y: drag.y };
  drag.el.style.transform = handle.style.transform = `translate(${drag.x}px,${drag.y}px)`;
  $('vguide').hidden = !drag.centre;
}

const round = value => Math.round(value * 10) / 10;
const percent = value => Math.min(98, Math.max(2, round(value)));

function end(handle) {
  if (!drag) return;
  const { el, held, size, from, x, y, moved, centre } = drag;
  drag = null;
  el.style.willChange = '';
  handle.classList.remove('drag');
  $('vguide').hidden = true;
  if (!moved) return;
  const dx = ((x - from.x) / size.width) * 100, dy = ((y - from.y) / size.height) * 100;
  // held: the frame's edge had stopped them short of the stored position
  st.x = centre ? 50 : percent(+st.x + held.x * 100 + dx);
  if (isStyled(st)) st.y = percent(+st.y + held.y * 100 + dy);
  else st.dy = Math.min(98, Math.max(-98, round((+st.dy || 0) + held.y * 100 + dy)));
  giveUp = setTimeout(settled, GIVE_UP_MS);
  styleChanged({ layout: true });
  emit('positioned');
}

export function init() {
  const handle = $('grab');
  handle.addEventListener('pointerdown', event => start(event, handle));
  handle.addEventListener('pointermove', event => move(event, handle));
  handle.addEventListener('pointerup', () => end(handle));
  handle.addEventListener('pointercancel', () => end(handle));
  handle.addEventListener('lostpointercapture', () => end(handle));
}
