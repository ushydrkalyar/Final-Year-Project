// The Pacing tool and the first-run strip: what decides the cut.
import { $, options, register } from './dom.js';
import { cutChanged, st } from './state.js';
import { GRADES, PRESETS, TEMPLATES, pauseLimit } from './style.js';

let advanced = false;

const select = (key, list, label) => `<div class="row">${label}<select class="in w" aria-label="${label}" data-change="setCut" data-key="${key}">${options(list, st[key])}</select></div>`;

const slider = (key, label, low, high) => `<div class="row">${label}<input type="range" aria-label="${label}" min="${low}" max="${high}" step="0.1" value="${pauseLimit(st, key)}" data-input="previewPause" data-change="setPause" data-key="${key}"><span id="${key}V">${pauseLimit(st, key).toFixed(1)}s</span></div>`;

export function render() {
  $('paceBody').innerHTML = `${select('stylePreset', PRESETS, 'Preset')}
    <p class="note">Natural keeps more breathing room. Aggressive tightens every pause.</p>
    ${select('lang', CUTSMART_LANGUAGES, 'Language')}
    ${select('grade', GRADES, 'Colour grade')}
    <details class="acc" id="advanced"${advanced ? ' open' : ''}><summary data-click="toggleAdvanced">ADVANCED</summary>
      ${slider('maxPause', 'Cut pauses over', 0.2, 5)}
      ${slider('compressPause', 'Shorten pauses over', 0.1, 3)}
      <p class="note">Pauses between the two are shortened. Picking a preset resets both.</p>
    </details>`;
  $('stepPace').innerHTML = options(PRESETS, st.stylePreset);
  $('stepTpl').innerHTML = options(TEMPLATES, st.preset);
}

export function init() {
  register({
    setCut: el => {
      st[el.dataset.key] = el.value;
      if (el.dataset.key === 'stylePreset') st.maxPause = st.compressPause = null;
      render();
      cutChanged();
    },
    toggleAdvanced: el => { advanced = !el.parentElement.open; },
    previewPause: el => { $(`${el.dataset.key}V`).textContent = `${(+el.value).toFixed(1)}s`; },
    setPause: el => {
      const value = +el.value;
      const cut = el.dataset.key === 'maxPause';
      st[el.dataset.key] = value;
      // shortening can't start later than cutting
      if (cut && pauseLimit(st, 'compressPause') > value) st.compressPause = value;
      if (!cut && pauseLimit(st, 'maxPause') < value) st.maxPause = value;
      render();
      cutChanged();
    },
  });
}
