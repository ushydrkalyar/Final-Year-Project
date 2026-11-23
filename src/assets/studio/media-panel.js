// The Media tool: add, reorder, replace and remove clips.
import { $, mmss, register } from './dom.js';
import { emit } from './bus.js';
import * as clips from './clips.js';
import * as job from './job.js';

let replacing = null; // the id of the clip being replaced
let dragged = null;

export function render() {
  const list = clips.all();
  const chosen = clips.current()?.id;
  const busy = job.busy();
  const off = busy ? ' disabled' : '';
  $('mediaBody').innerHTML = list.map((clip, i) => {
    const id = safeId(clip.id), thumb = safeImg(clip.thumb);
    return `<div class="clipcard${clip.id === chosen ? ' on' : ''}" draggable="${!busy}" role="button" tabindex="0" data-media="${id}" data-click="selectClip" data-id="${id}">
      <span class="pic"><i style="transform:rotate(${Number(clip.rotate)}deg);${thumb ? `background-image:url(${thumb})` : ''}"></i></span>
      <div class="nm"><div class="nm" title="${esc(clip.name)}">${i + 1}. ${esc(clip.name)}</div><small class="note">${clip.dur ? mmss(clip.dur) : ''}</small>
        <div class="clipacts">
          <button class="icon" data-click="moveClip" data-id="${id}" data-by="-1" aria-label="Move earlier" title="Move earlier"${i && !busy ? '' : ' disabled'}>↑</button>
          <button class="icon" data-click="moveClip" data-id="${id}" data-by="1" aria-label="Move later" title="Move later"${i < list.length - 1 && !busy ? '' : ' disabled'}>↓</button>
          <button class="icon" data-click="rotateOne" data-id="${id}" aria-label="Turn this clip" title="Turn only this clip"${off}>↻</button>
          <button class="tbtn" data-click="pickReplacement" data-id="${id}"${off}>Replace</button>
          <button class="icon" data-click="removeClip" data-id="${id}" aria-label="Remove clip" title="Remove clip"${list.length > 1 && !busy ? '' : ' disabled'}>×</button>
        </div></div></div>`;
  }).join('')
    + `<button class="btn mint wide" data-click="pickClips"${busy || !list.length || list.length >= clips.MAX ? ' disabled' : ''}>+ Add clips</button>`
    + `<p class="note">Drag to reorder. Up to ${clips.MAX} clips.</p>`;
}

const usable = file => file.type.startsWith('video/') || /\.(mp4|mov|mkv|webm|avi)$/i.test(file.name);

function chosenFiles(input) {
  const files = [...(input.files ?? [])].filter(usable);
  input.value = '';
  try {
    CutSmartProjects.checkSizes(files);
  } catch (error) {
    toast(error.message);
    return [];
  }
  return files;
}

function enableDrag() {
  const list = $('mediaBody');
  list.addEventListener('dragstart', event => {
    const card = event.target.closest?.('[data-media]');
    if (!card) return;
    dragged = card.dataset.media;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', dragged);
  });
  list.addEventListener('dragover', event => {
    if (dragged && event.target.closest?.('[data-media]')) event.preventDefault();
  });
  list.addEventListener('drop', event => {
    const card = event.target.closest?.('[data-media]');
    if (!dragged || !card || job.busy()) return;
    event.preventDefault();
    clips.move(dragged, clips.all().findIndex(clip => clip.id === card.dataset.media));
    dragged = null;
  });
  list.addEventListener('dragend', () => { dragged = null; });
}

export function init() {
  enableDrag();
  register({
    selectClip: el => {
      clips.select(el.dataset.id);
      emit('clip:chosen', el.dataset.id);
    },
    moveClip: el => {
      if (job.busy()) return;
      clips.move(el.dataset.id, clips.all().findIndex(clip => clip.id === el.dataset.id) + +el.dataset.by);
    },
    removeClip: el => {
      if (job.busy()) return;
      const problem = clips.remove(el.dataset.id);
      if (problem) toast(problem);
    },
    pickClips: () => {
      if (!job.busy()) $('addIn').click();
    },
    pickReplacement: el => {
      if (job.busy()) return;
      replacing = el.dataset.id;
      $('replIn').click();
    },
    addClips: input => {
      const files = chosenFiles(input);
      if (!files.length || job.busy()) return;
      const problem = clips.add(files.map(file => clips.make({ file })));
      if (problem) toast(problem);
    },
    replaceVideo: input => {
      const [file] = chosenFiles(input);
      if (!file || job.busy() || !clips.byId(replacing)) return;
      clips.replace(replacing, clips.make({ file }));
    },
  });
}
