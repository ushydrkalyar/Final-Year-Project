// The inspector for captions: text, layout, emphasis and position.
import { $, options, register } from './dom.js';
import { emit } from './bus.js';
import { st, styleChanged } from './state.js';
import { TEMPLATES, isStyled } from './style.js';

let fonts = ['Arial', 'Arial Black', 'Impact', 'Georgia', 'Helvetica', 'Verdana', 'Trebuchet MS', 'Courier New',
  'Times New Roman', 'Didot', 'Futura', 'Avenir Next'];

const toggle = (on, action, label, value) =>
  `<button class="${on ? 'on' : ''}" data-click="${action}" data-value="${value}">${label}</button>`;

const help = text => `<p class="note">${text}</p>`;

function templateNotice() {
  if (st.preset === 'custom') return '';
  const name = (TEMPLATES.find(([id]) => id === st.preset) || [0, ''])[1];
  const limits = isStyled(st) ? '. Every option below is editable.'
    : '. Layout and position keep the template; font and colour switch to Custom.';
  return `<div style="color:var(--t2);font-size:12.5px;background:rgba(116,245,184,.06);border-radius:12px;padding:10px 12px;margin:8px 0">Template: <b style="color:var(--primary)">${esc(name)}</b>${limits}</div>`;
}

function layoutSection() {
  return `
   <div class="row">Words per caption<input type="range" min="0" max="8" value="${esc(st.wpc)}" data-input="setRange" data-key="wpc"><span id="wpcv" style="width:42px;text-align:right">${esc(st.wpc || 'Auto')}</span></div>
   ${st.em ? '' : `<div class="row">Lines<div class="sq">${[1, 2].map(n => toggle(st.lines === n, 'setLines', `${n} line${n > 1 ? 's' : ''}`, n)).join('')}</div></div>`}
   ${help('Auto keeps natural phrases. Pick a number to show exactly that many words at once.')}
   <div class="row">Line spacing<input type="range" min="0.5" max="2.5" step="0.1" value="${esc(st.lineSpacing)}" data-input="setRange" data-key="lineSpacing"><span id="lnsv" style="width:42px;text-align:right">${(+st.lineSpacing).toFixed(1)}×</span></div>
   ${help('Controls the gap between stacked caption lines. 1.0× is the default spacing.')}`;
}

function colourRow(key, id) {
  return `<div class="row">Color<div style="display:flex;gap:8px;align-items:center"><input type="color" value="${esc(st[key])}" data-input="pickColour" data-key="${key}" style="width:32px;height:32px;border:0;background:none"><input class="in" style="width:78px" id="${id}" value="${esc(st[key])}" data-change="typeColour" data-key="${key}"></div></div>`;
}

function fontSection() {
  return `
   <div class="row">Font Family<select class="in w" data-change="setValue" data-key="font">${options(fonts, st.font)}</select></div>
   <div class="row">Font Face<select class="in w" data-change="setValue" data-key="face">${options(['Bold', 'Regular'], st.face)}</select></div>
   <div class="row">Italic<div class="sq"><button class="${st.italic ? 'on' : ''}" data-click="flip" data-key="italic"><i>t</i></button></div></div>
   <div class="row">Font Size<input type="range" min="16" max="64" value="${esc(st.size)}" data-input="setRange" data-key="size"><input class="in s" id="szv" value="${esc(st.size)}" data-change="typeSize"> px</div>
   <div class="row">Letter spacing<input type="range" min="-2" max="20" step="0.5" value="${esc(st.letterSpacing)}" data-input="setRange" data-key="letterSpacing"><span id="ltsv" style="width:42px;text-align:right">${esc(st.letterSpacing)}px</span></div>
   ${colourRow('color', 'hx')}`;
}

function emphasisSection() {
  const body = !st.em
    ? '<p style="font-size:12px;color:var(--t3);line-height:1.5">Turn on to make one word per caption bigger, in its own font and colour, with the other words placed around it.</p>'
    : `
   <div class="row">Font Family<select class="in w" data-change="setValue" data-key="emFont">${options(fonts, st.emFont)}</select></div>
   <div class="row">Italic<div class="sq"><button class="${st.emItalic ? 'on' : ''}" data-click="flip" data-key="emItalic"><i>t</i></button></div></div>
   ${colourRow('emColor', 'emhx')}
   <div class="row">Size vs normal<input type="range" min="1.2" max="4" step="0.1" value="${esc(st.emScale)}" data-input="setRange" data-key="emScale"><span id="emsv" style="width:42px;text-align:right">${(+st.emScale).toFixed(1)}×</span></div>
   <div class="row">Glow<div class="sq">${toggle(st.glow, 'setGlow', 'On', 1)}${toggle(!st.glow, 'setGlow', 'Off', 0)}</div></div>
   <div class="row">Keyword in<div class="sq">${toggle(st.emEvery === 1, 'setEvery', 'Every caption', 1)}${toggle(st.emEvery === 2, 'setEvery', 'Every 2nd', 2)}</div></div>`;
  return `
   <div class="row">Highlight a keyword<div class="sq">${toggle(st.em, 'setEmphasis', 'On', 1)}${toggle(!st.em, 'setEmphasis', 'Off', 0)}</div></div>
   ${body}`;
}

function alignSection() {
  if (st.em) return '';
  const icons = { l: '☰', c: '≡', r: '☷' };
  return `<div class="row">Alignment<div class="sq">${Object.entries(icons).map(([id, icon]) => toggle(st.align === id, 'setAlign', icon, id)).join('')}</div></div>`;
}

function positionSection() {
  const x = Math.round(st.x), y = Math.round(isStyled(st) ? st.y : st.dy || 0);
  const vertical = isStyled(st)
    ? `<div class="row">Vertical position (Y)<input type="range" min="5" max="95" value="${y}" data-input="setRange" data-key="y"><span id="yv" style="width:40px;text-align:right">${y}%</span></div>`
    : `<div class="row">Move up or down<input type="range" min="-90" max="90" value="${y}" data-input="setRange" data-key="dy"><span id="dyv" style="width:40px;text-align:right">${y}%</span></div>`;
  return `${vertical}
   <div class="row">Horizontal position (X)<input type="range" min="5" max="95" value="${x}" data-input="setRange" data-key="x"><span id="xv" style="width:52px;text-align:right">${x === 50 ? 'Centre' : `${x}%`}</span></div>
   ${help('Or drag the captions on the preview. They always stay inside the picture.')}${alignSection()}`;
}

export function sections() {
  return [
    ['', '', templateNotice()],
    ['layout', 'LAYOUT', layoutSection()],
    ['text', 'TEXT', fontSection()],
    ['emphasis', 'EMPHASIS', emphasisSection()],
    ['position', 'POSITION', positionSection()],
  ];
}

// Each slider's readout, and whether it applies on top of a fixed template.
const SLIDERS = {
  wpc: { out: 'wpcv', show: v => v || 'Auto', layout: true },
  lineSpacing: { out: 'lnsv', show: v => `${v.toFixed(1)}×`, layout: true },
  letterSpacing: { out: 'ltsv', show: v => `${v}px`, layout: true },
  x: { out: 'xv', show: v => (v === 50 ? 'Centre' : `${v}%`), layout: true },
  y: { out: 'yv', show: v => `${v}%` },
  dy: { out: 'dyv', show: v => `${v}%`, layout: true },
  emScale: { out: 'emsv', show: v => `${v.toFixed(1)}×` },
  size: { out: 'szv', show: v => v },
};

export function setFonts(families) {
  fonts = [...fonts.slice(0, 4), ...families];
  emit('panel');
}

export function init() {
  register({
    setRange: el => {
      const key = el.dataset.key, { out, show, layout } = SLIDERS[key];
      st[key] = +el.value;
      const readout = $(out);
      if (readout.matches('input')) readout.value = show(st[key]);
      else readout.textContent = show(st[key]);
      styleChanged({ layout: !!layout });
    },
    setValue: el => {
      st[el.dataset.key] = el.value;
      styleChanged();
    },
    flip: el => {
      st[el.dataset.key] = !st[el.dataset.key];
      styleChanged({ rerender: true });
    },
    setLines: el => {
      st.lines = +el.dataset.value;
      styleChanged({ rerender: true, layout: true });
    },
    setEmphasis: el => {
      st.em = el.dataset.value === '1';
      styleChanged({ rerender: true });
    },
    setGlow: el => {
      st.glow = el.dataset.value === '1';
      styleChanged({ rerender: true });
    },
    setEvery: el => {
      st.emEvery = +el.dataset.value;
      styleChanged({ rerender: true });
    },
    setAlign: el => {
      st.align = el.dataset.value;
      styleChanged({ rerender: true });
    },
    pickColour: el => {
      st[el.dataset.key] = el.value.toUpperCase();
      el.nextElementSibling.value = st[el.dataset.key];
      styleChanged();
    },
    typeColour: el => {
      const colour = normHex(el.value);
      el.value = colour || st[el.dataset.key];
      if (!colour) return toast('Use a colour like #FFD400 or #FD0');
      st[el.dataset.key] = colour;
      el.previousElementSibling.value = colour;
      styleChanged();
    },
    typeSize: el => {
      const size = el.value.trim() === '' ? NaN : Number(el.value);
      if (!Number.isFinite(size)) {
        el.value = st.size;
        return;
      }
      st.size = Math.round(Math.min(64, Math.max(16, size)));
      el.value = st.size;
      el.previousElementSibling.value = st.size;
      styleChanged();
    },
  });
}
