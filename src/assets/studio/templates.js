// The Templates tab: built-in looks, saved presets, and looks read from a reference video.
import { $, register } from './dom.js';
import { emit } from './bus.js';
import * as project from './project.js';
import { FIXED_LOOKS } from './preview.js';
import { st } from './state.js';
import { EDITABLE, PRESET_KEYS, TAGS, TEMPLATES, TEMPLATE_LAYOUT, cleanStyle } from './style.js';

const SAMPLE = 'the quick<br>brown fox';
const EMPTY_PRESETS = '<div style="color:var(--t3);padding:24px 6px;font-size:13.5px;line-height:1.6">No presets yet. Style the captions how you like, then press <b>Save preset</b>.</div>';
const NO_MATCH = '<div style="color:var(--t3);padding:16px">No templates match.</div>';

function markup() {
  return `<div class="tsw"><button class="${st.tplTab === 'built' ? 'on' : ''}" data-click="showTemplates" data-value="built">Built-in Templates</button><button class="${st.tplTab === 'mine' ? 'on' : ''}" data-click="showTemplates" data-value="mine">My Presets</button></div>
   <div class="tbar"><input class="in" id="tsrch" placeholder="Find a template" value="${esc(st.tsearch)}" data-input="searchTemplates"><button class="btn" data-click="toggleSaving">💾 Save preset</button></div>
   <div class="tbar"><button class="btn" data-click="pickReference" id="refUploadBtn">🎬 Style from reference video</button><input type="file" id="refVidInput" accept="video/*" style="display:none" data-change="readReference"></div>
   ${st.saving ? '<div class="saveRow"><input class="in" id="pname" placeholder="Preset name" style="flex:1"><button class="btn mint" data-click="savePreset">Save</button></div>' : ''}
   <div id="tpls"></div>`;
}

export function render() {
  $('tplBody').innerHTML = markup();
  renderList();
}

function sample(id, config) {
  if (id === 'soft_reveal') {
    return '<div style="font-family:Manrope,sans-serif;font-weight:700;font-size:15px;color:#fff;white-space:nowrap">All know is <span style="opacity:.45">just</span></div><div style="font-family:\'Instrument Serif\',serif;font-style:italic;font-size:24px;color:#fff;opacity:.8;text-shadow:0 0 8px #fff8;line-height:1.1">feeling</div>';
  }
  if (id === 'box_karaoke') {
    return '<div style="background:#fff;border-radius:5px;padding:7px 12px;font-family:\'Poppins Medium\',Poppins,sans-serif;font-size:15px;line-height:1.1;color:#141414;white-space:nowrap">All know is <span style="color:#C8C8C8">just something</span></div>';
  }
  const raw = config || EDITABLE[id];
  if (raw) {
    const c = Object.fromEntries(Object.entries(raw).map(([key, v]) => [key, typeof v === 'string' ? esc(v) : v]));
    const text = `font-family:'${c.font}';font-style:${c.italic ? 'italic' : 'normal'};font-weight:${c.face === 'Bold' ? 700 : 400};color:${c.color}`;
    if (c.em) {
      const glow = c.glow ? `text-shadow:0 0 10px ${c.emColor}99;` : '';
      return `<div style="${text};font-size:13px;align-self:flex-start;margin-left:16%">me</div><div style="font-family:'${c.emFont}';font-style:${c.emItalic ? 'italic' : 'normal'};color:${c.emColor};font-size:${Math.round(13 * c.emScale * 1.1)}px;line-height:1;text-transform:uppercase;${glow}">FEELING</div><div style="${text};font-size:13px;align-self:flex-end;margin-right:16%">as if</div>`;
    }
    if (c.fromRef) {
      const align = { l: 'left', r: 'right' }[c.align] || 'center';
      return `<div style="${text};font-size:20px;line-height:1.2;text-align:${align}">${SAMPLE}</div>`;
    }
  }
  const look = id === 'custom' ? 'color:#fff;font-weight:700;' : FIXED_LOOKS[id] || 'color:#fff;';
  return `<div style="${look}font-size:22px;line-height:1.15">${SAMPLE}</div>`;
}

function card({ id, name, look, tags = [], action, on = false, isNew = false, remove = '' }) {
  const safe = safeId(id);
  const removeButton = remove
    ? `<button class="del" aria-label="Delete ${esc(name)}" data-click="${remove}" data-id="${safe}">✕</button>` : '';
  return `<div class="tcard ${on ? 'on' : ''}" role="button" tabindex="0" aria-pressed="${on}" data-click="${action}" data-id="${safe}"><div class="hd"><span>${esc(name)}</span>${removeButton}${isNew ? '<span class="new">New</span>' : ''}</div><div class="pv">${look}</div><div class="tags">${tags.map(tag => `<span>${esc(tag)}</span>`).join('')}</div></div>`;
}

export function renderList() {
  const list = $('tpls');
  if (!list) return;
  const query = st.tsearch.toLowerCase();
  const matches = name => name.toLowerCase().includes(query);
  if (st.tplTab === 'mine') {
    list.innerHTML = project.presets().filter(p => matches(p.name)).map(p => card({
      id: p.id, name: p.name, tags: ['Saved'], action: 'applyPreset', remove: 'deletePreset',
      look: sample('x', p.cfg.em ? p.cfg : null),
    })).join('') || EMPTY_PRESETS;
    return;
  }
  list.innerHTML = TEMPLATES.filter(([, name]) => matches(name)).map(([id, name]) => card({
    id, name, look: sample(id), tags: TAGS[id], action: 'useTemplate', on: st.preset === id,
    isNew: !!EDITABLE[id], remove: EDITABLE[id]?.fromRef ? 'deleteTemplate' : '',
  })).join('') || NO_MATCH;
}

function use(id) {
  emit('style:before');
  if (id !== 'custom' && id !== st.preset) Object.assign(st, TEMPLATE_LAYOUT);
  st.preset = id;
  if (EDITABLE[id]) Object.assign(st, EDITABLE[id]);
  else st.em = false;
  emit('template', { rerender: true });
  renderList();
}

function add(key, label, config) {
  if (EDITABLE[key]) return;
  EDITABLE[key] = { ...cleanStyle(config), fromRef: true };
  TEMPLATES.push([key, label]);
  TAGS[key] = ['From reference video'];
}

export async function loadCustom() {
  try {
    for (const [key, { label, ...config }] of Object.entries(await apiJson('/custom-templates'))) {
      add(key, label, config);
    }
    renderList();
    emit('panel');
  } catch { /* the built-in templates still work */ }
}

async function readReference(input) {
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  const button = $('refUploadBtn');
  const label = button?.textContent;
  if (button) {
    button.textContent = 'Analyzing reference video… (can take under a minute)';
    button.disabled = true;
  }
  try {
    const form = new FormData();
    form.append('file', file);
    form.append('name', file.name.replace(/\.[^.]+$/, ''));
    const made = await apiJson('/extract-reference-style',
      { method: 'POST', body: form, fallback: 'Could not read captions in that video' });
    add(made.key, made.label, made.config);
    use(made.key);
    st.tplTab = 'built';
    renderList();
    toast(`Template "${made.label}" created from your reference video`);
  } catch (error) {
    toast(error.status ? error.message : 'Upload failed — check your connection and try again');
  } finally {
    if (button) {
      button.textContent = label;
      button.disabled = false;
    }
  }
}

export function init() {
  register({
    useTemplate: el => use(el.dataset.id),
    pickTemplate: el => use(el.value),
    searchTemplates: el => {
      st.tsearch = el.value;
      renderList();
    },
    showTemplates: el => {
      st.tplTab = el.dataset.value;
      render();
    },
    toggleSaving: () => {
      st.saving = !st.saving;
      render();
    },
    savePreset: () => {
      const saved = project.presets();
      const name = ($('pname').value || '').trim() || `Preset ${saved.length + 1}`;
      const cfg = Object.fromEntries(PRESET_KEYS.map(key => [key, st[key]]));
      project.savePresets([{ id: `u${Date.now()}`, name, cfg }, ...saved]);
      st.saving = false;
      st.tplTab = 'mine';
      render();
      toast('Preset saved');
    },
    applyPreset: el => {
      const preset = project.presets().find(p => safeId(p.id) === el.dataset.id);
      if (!preset) return;
      emit('style:before');
      Object.assign(st, cleanStyle(preset.cfg));
      st.preset = 'custom';
      emit('template', { rerender: true });
    },
    deletePreset: el => {
      project.savePresets(project.presets().filter(p => safeId(p.id) !== el.dataset.id));
      renderList();
    },
    deleteTemplate: async el => {
      const { id } = el.dataset;
      try {
        await api(`/custom-templates/${id}`, { method: 'DELETE' });
      } catch {
        return toast("Couldn't delete that template. Try again.");
      }
      const index = TEMPLATES.findIndex(([key]) => key === id);
      if (index >= 0) TEMPLATES.splice(index, 1);
      delete EDITABLE[id];
      delete TAGS[id];
      if (st.preset === id) use('custom');
      renderList();
    },
    pickReference: () => $('refVidInput')?.click(),
    readReference,
  });
}
