// The caption look and edit settings: defaults, templates, and what they send to the server. No DOM, no state.

export const DEFAULTS = {
  tplTab: 'built', saving: false, tsearch: '', // the Templates tool's own view state
  font: 'Arial', face: 'Bold', size: 64, color: '#FFFFFF', italic: false,
  y: 82, x: 50, dy: 0, align: 'c', preset: 'custom', wpc: 0, lines: 2, letterSpacing: 0, lineSpacing: 1,
  em: false, emFont: 'Arial Black', emColor: '#B4EA30', emItalic: false, emScale: 2.1, glow: true, emEvery: 2,
  lang: 'auto', stylePreset: 'balanced', grade: 'none', ratio: '',
  maxPause: null, compressPause: null, // null = the preset's
};

// The presets' thresholds, for the Advanced sliders (pipeline.PACING_PRESETS; a test keeps them equal).
export const PRESET_PAUSES = {
  natural: { maxPause: 2.5, compressPause: 1.2 },
  balanced: { maxPause: 1.5, compressPause: 0.8 },
  aggressive: { maxPause: 0.5, compressPause: 0.3 },
};
export const pauseLimit = (st, key) => st[key] ?? (PRESET_PAUSES[st.stylePreset] ?? PRESET_PAUSES.balanced)[key];

const VIEW_ONLY = ['tab', 'mode', 'tplTab', 'tsearch', 'saving']; // tab and mode: saved by older versions
export const PRESET_KEYS = ['font', 'face', 'italic', 'emItalic', 'size', 'color', 'wpc', 'lines', 'em',
  'emFont', 'emColor', 'emScale', 'glow', 'emEvery', 'y', 'align'];

export const PRESETS = [['natural', 'Natural'], ['balanced', 'Balanced'], ['aggressive', 'Aggressive']];
export const GRADES = [['none', 'None'], ['cinematic', 'Cinematic'], ['bright', 'Bright & vivid'], ['matte', 'Matte'],
  ['vivid', 'High contrast'], ['cool', 'Cool & blue']];

export const TRANSITIONS = [
  ['auto', 'Match reference'], ['hard_cut', 'Hard cut'], ['crossfade', 'Crossfade'],
  ['fade', 'Fade to black'], ['fade_white', 'Fade to white'],
  ['wipe_left', 'Wipe left'], ['wipe_right', 'Wipe right'], ['wipe_up', 'Wipe up'], ['wipe_down', 'Wipe down'],
];

// [id, name], in the order the Templates tab shows them
export const TEMPLATES = [
  ['custom', 'Custom'], ['neon_pop', 'Lime Pop'], ['editorial', 'Red Editorial'], ['box_karaoke', 'Highlight Box'],
  ['soft_reveal', 'Soft Reveal'], ['animated', 'Animated'], ['impact_yellow', 'Impact Yellow'],
  ['outline_pop', 'Outline Pop'], ['bubble', 'Bubble'], ['white_card', 'White Card'], ['bold_white', 'Bold White'],
  ['yellow', 'Yellow'], ['cinema', 'Cinema'], ['box', 'Box'], ['minimal', 'Minimal'], ['top', 'Top'],
  ['default', 'Classic'],
];

// Templates whose every option stays editable. The others are fixed looks that live on the server.
export const EDITABLE = {
  neon_pop: {
    font: 'Montserrat', face: 'Bold', italic: false, emItalic: false, size: 28, color: '#FFFFFF', wpc: 3, lines: 1,
    em: true, emFont: 'Montserrat ExtraBold', emColor: '#BEEA2B', emScale: 3.4, glow: true, emEvery: 2, y: 50,
  },
  ...CUTSMART_LOCKED_TEMPLATES, // defined in studio.html, where the lock check reads them
  editorial: {
    font: 'Instrument Serif', face: 'Regular', italic: true, emItalic: false, size: 34, color: '#FFFFFF', wpc: 3,
    lines: 1, em: true, emFont: 'Anton', emColor: '#C0281C', emScale: 5.5, glow: false, emEvery: 1, y: 55,
  },
};

export const TAGS = {
  soft_reveal: ['Word fade', 'Italic accent', 'Clean'], box_karaoke: ['Karaoke', 'Box', 'Highlight'],
  impact_yellow: ['Bold', 'Outline'], outline_pop: ['Bold', 'Outline'], bubble: ['Bold', 'Box'],
  white_card: ['Bold', 'Box'], bold_white: ['Bold'], yellow: ['Bold'], cinema: ['Italic', 'Serif'], box: ['Box'],
  minimal: ['Light'], top: ['Top'], default: ['Classic'], animated: ['Animated', 'Word highlight'],
  custom: ['Your own settings'], neon_pop: ['Keyword pop', 'Scatter', 'Glow'],
  editorial: ['Keyword pop', 'Serif', 'Scatter'],
};

export const isStyled = st => st.preset === 'custom' || !!EDITABLE[st.preset];

// Settings from storage or the server (projects, presets, templates): only well-formed values get in.
export function cleanStyle(raw) {
  const c = { ...raw };
  for (const [key, fallback] of [['color', '#FFFFFF'], ['emColor', '#B4EA30']]) {
    if (key in c) c[key] = normHex(c[key]) || fallback;
  }
  for (const [key, fallback] of [['font', 'Arial'], ['emFont', 'Arial Black']]) {
    if (key in c && !/^[\p{L}\p{N}_ .-]{1,64}$/u.test(c[key])) c[key] = fallback;
  }
  if ('ratio' in c && !['', 'vertical', 'landscape', 'square'].includes(c.ratio)) c.ratio = '';
  for (const [key, low, high] of [['maxPause', 0.2, 5], ['compressPause', 0.1, 3]]) {
    if (key in c) c[key] = c[key] !== null && +c[key] >= low && +c[key] <= high ? +c[key] : null;
  }
  for (const key of ['size', 'x', 'y', 'dy', 'wpc', 'lines', 'emScale', 'emEvery', 'letterSpacing', 'lineSpacing']) {
    if (!(key in c)) continue;
    if (c[key] === null || !Number.isFinite(+c[key])) delete c[key];
    else c[key] = +c[key];
  }
  if ('size' in c) c.size = Math.min(64, Math.max(16, c.size));
  return c;
}

// Saved settings without the panel's view state.
export function persistable(st) {
  const copy = structuredClone(st);
  VIEW_ONLY.forEach(key => delete copy[key]);
  return copy;
}

// What a template starts from: nothing of the one before it carries over.
export const TEMPLATE_LAYOUT = { x: 50, dy: 0, wpc: 0, lines: 2, letterSpacing: 0, lineSpacing: 1 };

export function hex2ass(colour) {
  const h = (normHex(colour) || '#FFFFFF').slice(1);
  return '&H00' + h.slice(4, 6) + h.slice(2, 4) + h.slice(0, 2);
}

const EMPHASIS_LOOK = { neon_pop: 'lime', box_karaoke: 'box', soft_reveal: 'soft' };

// The look, as the form fields /caption-preview, /captions, /export and /edit-video accept.
export function styleFields(st, form) {
  const styled = isStyled(st);
  const plain = styled && !st.em && st.preset === 'custom';
  const size = plain ? Math.max(6, Math.round(st.size * 0.30)) : Math.max(10, Math.round(st.size * 0.56));
  const position = st.y < 33 ? 6 : st.y < 66 ? 10 : 2;
  const margin = position === 2 ? Math.max(8, Math.round((100 - st.y) * 2.88)) : 0;
  const emphasis = styled && st.em;
  const fields = {
    subtitle_preset: styled ? (st.em ? 'pop' : 'custom') : st.preset,
    subtitle_font: st.font,
    subtitle_size: size,
    subtitle_colour: hex2ass(st.color),
    subtitle_position: position,
    words_per_caption: st.wpc,
    caption_lines: st.lines,
    subtitle_margin_v: margin,
    sub_bold: st.face === 'Bold' ? 1 : 0,
    sub_italic: st.italic ? 1 : 0,
    pos_y: (st.y / 100).toFixed(3),
    pos_x: ((+st.x || 50) / 100).toFixed(3),
    sub_align: st.align || 'c',
    emphasis: emphasis ? 1 : 0,
    emph_font: st.emFont,
    emph_italic: st.emItalic ? 1 : 0,
    emph_look: EMPHASIS_LOOK[st.preset] || 'classic',
    emph_colour: hex2ass(st.emColor),
    emph_scale: st.emScale,
    emph_glow: st.glow ? 1 : 0,
    emph_every: st.emEvery,
    letter_spacing: st.letterSpacing,
    line_spacing: st.lineSpacing,
    offset_y: styled ? 0 : ((+st.dy || 0) / 100).toFixed(3), // a fixed template, dragged up or down
  };
  for (const [key, value] of Object.entries(fields)) form.append(key, value);
  return form;
}

// What changes the cut itself: a different signature means a re-edit (clips.signature adds the clips).
export const pipeSignature = st =>
  JSON.stringify([st.stylePreset, st.grade, st.lang, st.maxPause, st.compressPause, st.ratio]);
