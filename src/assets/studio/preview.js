// The caption look before there is an edit: sample captions drawn by the server with the export's own
// engine and fonts, over the upload. CSS stands in until that image arrives.
import { $ } from './dom.js';
import { emit } from './bus.js';
import * as clips from './clips.js';
import * as framing from './framing.js';
import * as job from './job.js';
import { st } from './state.js';
import { isStyled, styleFields } from './style.js';

const DEBOUNCE_MS = 170;

export const FIXED_LOOKS = {
  neon_pop: 'color:#B4EA30;font-weight:900;',
  editorial: 'font-family:Instrument Serif,Georgia;font-style:italic;color:#C0281C;',
  impact_yellow: 'font-family:Impact;color:#FFF000;-webkit-text-stroke:2px #000;',
  outline_pop: 'font-family:Arial Black;color:#fff;-webkit-text-stroke:3px #000;',
  bubble: 'font-family:Arial;background:#74F5B8;color:#0B0B0B;padding:4px 12px;border-radius:8px;display:inline-block;',
  white_card: 'font-family:Arial;background:#fff;color:#111;padding:4px 12px;border-radius:8px;display:inline-block;',
  cinema: 'font-family:Georgia;font-style:italic;font-weight:400;color:#fff;',
  bold_white: 'color:#fff;',
  yellow: 'color:#FFE600;',
  box: 'background:rgba(0,0,0,.7);color:#fff;padding:4px 12px;display:inline-block;',
  minimal: 'color:#fff;font-weight:400;',
  animated: 'color:#fff;',
  top: 'color:#fff;',
  default: 'color:#fff;',
};

let seq = 0;
let timer = null;
let url = '';
let key = '';
let usable = false;

function dimensions() {
  const clip = clips.current();
  const sideways = clip.rotate % 180 !== 0;
  const source = [clip.w || 9, clip.h || 16];
  const [w, h] = framing.active() ? [framing.aspect(), 1] : sideways ? source.reverse() : source;
  const scale = 1280 / Math.max(w, h);
  return [Math.round(w * scale), Math.round(h * scale)];
}

function request() {
  if (!clips.current()?.w) return;
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const form = styleFields(st, new FormData());
    const [w, h] = dimensions();
    form.append('out_w', w);
    form.append('out_h', h);
    const wanted = [...form.entries()].map(pair => pair.join('=')).join('&');
    if (wanted === key && usable) return;
    const mine = ++seq;
    try {
      const image = await (await api('/caption-preview', { method: 'POST', body: form })).blob();
      if (mine !== seq) return;
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(image);
      key = wanted;
      usable = true;
      $('ovimg').onload = () => emit('preview:painted');
      $('ovimg').src = url;
    } catch {
      if (mine === seq) {
        usable = false;
        key = '';
        drawStandIn();
      }
      return;
    }
    placeImage();
  }, DEBOUNCE_MS);
}

function placeImage() {
  const image = $('ovimg');
  if (!usable || !url || !clips.count() || job.hasResult()) {
    image.style.display = 'none';
    return false;
  }
  const stage = $('stage').getBoundingClientRect();
  const view = framing.viewRect();
  Object.assign(image.style, {
    left: `${view.left - stage.left}px`, top: `${view.top - stage.top}px`,
    width: `${view.width}px`, height: `${view.height}px`, display: 'block',
  });
  $('ovl').style.display = 'none';
  return true;
}

function emphasisSample() {
  const colour = esc(st.emColor);
  const glow = st.glow ? `text-shadow:0 0 12px ${colour};` : '';
  const spacing = +st.lineSpacing || 1;
  const line = (side, word) => `<div style="text-align:${side};padding-${side}:14%;line-height:${(1.1 * spacing).toFixed(2)}">${word}</div>`;
  const keyword = `<div style="font-family:'${esc(st.emFont)}',Impact,sans-serif;font-style:${st.emItalic ? 'italic' : 'normal'};color:${colour};font-size:${+st.emScale}em;line-height:${(0.95 * spacing).toFixed(2)};text-transform:uppercase;font-weight:400;${glow}">inside</div>`;
  return line('left', 'up') + keyword + line('right', 'like');
}

function sample() {
  if (st.em && isStyled(st)) return emphasisSample();
  const words = 'Your captions will look like this on screen'.split(' ').slice(0, st.wpc || 4);
  if (st.lines < 2 || words.length < 2) return words.join(' ');
  const half = Math.ceil(words.length / 2);
  return `${words.slice(0, half).join(' ')}<br>${words.slice(half).join(' ')}`;
}

function placeStandIn(el) {
  const styled = isStyled(st);
  const stage = $('stage').getBoundingClientRect();
  const view = framing.viewRect();
  const y = st.preset === 'top' ? 12 : styled ? st.y : 82;
  const x = Math.min(75, Math.max(25, +st.x || 50));
  const width = view.width * Math.min(1, (2 * Math.min(x, 100 - x)) / 100);
  const align = { l: 'left', r: 'right' }[st.align] || 'center';
  const look = styled
    ? `font-family:'${st.font}',sans-serif;color:${st.color};font-weight:${st.face === 'Bold' ? 700 : 400};font-style:${st.italic ? 'italic' : 'normal'};text-shadow:0 2px 6px rgba(0,0,0,.7);text-align:${align};`
    : FIXED_LOOKS[st.preset] || 'color:#fff;';
  el.style.cssText = `display:block;right:auto;transform:translateY(-50%);
    top:${view.top - stage.top + (view.height * y) / 100}px;
    left:${view.left - stage.left + (view.width * x) / 100 - width / 2}px;width:${width}px;
    font-size:${Math.max(12, (styled ? st.size : 30) * (view.width / 540))}px;
    letter-spacing:${+st.letterSpacing || 0}px;line-height:${+st.lineSpacing || 1};${look}`;
}

function drawStandIn() {
  const el = $('ovl');
  if (!clips.count() || job.hasResult()) {
    el.style.display = 'none';
    return;
  }
  $('ovimg').style.display = 'none';
  el.innerHTML = sample();
  placeStandIn(el);
}

export function update() {
  if (!clips.count() || job.hasResult()) { // a finished edit: libass draws the real captions
    $('ovl').style.display = 'none';
    $('ovimg').style.display = 'none';
    return;
  }
  request();
  if (!placeImage()) drawStandIn();
}
