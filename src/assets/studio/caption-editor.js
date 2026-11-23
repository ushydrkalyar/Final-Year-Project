// The Captions tool: the editable list, and its keyboard shortcuts.
import { $, mmss, register } from './dom.js';
import * as captions from './captions.js';
import * as job from './job.js';
import * as player from './player.js';
import * as actions from './actions.js';
import * as selection from './selection.js';

const ADD = label => `<button class="addcap" data-click="addCaption">＋ ${label}</button>`;

export function render() {
  const list = captions.list();
  const box = $('clist');
  if (!list.length) {
    box.innerHTML = `<p class="note">No speech was detected in this video.</p>${ADD('Add caption')}`;
    return;
  }
  const query = ($('csearch').value || '').toLowerCase();
  const selected = captions.selection();
  const rows = list.map((c, i) => ({ c, i })).filter(({ c }) => c.t.toLowerCase().includes(query));
  box.innerHTML = rows.map(({ c, i }) => `<div class="crow${i === selected ? ' sel' : ''}" data-i="${i}" data-click="openCaption"><span class="n">${i + 1}</span><span class="tx" contenteditable="true" spellcheck="true" data-i="${i}" data-click="selectCaption" data-focus="selectCaption" data-blur="editCaption" data-keydown="captionKey">${esc(c.t)}</span><span class="tm">${mmss(c.s)}</span><button class="del" title="Delete caption" data-click="deleteCaption" data-i="${i}">×</button></div>`).join('')
    + ADD('Add caption at playhead');
}

export function markSelection(index) {
  document.querySelectorAll('.crow, .blk').forEach(el => el.classList.toggle('sel', +el.dataset.i === index));
  $('tSplit').disabled = index < 0;
  $('tDel').disabled = index < 0 && !selection.selected()?.in_edit;
}

function add() {
  const length = player.length();
  if (!length) return toast('Generate your edit first');
  const problem = captions.add(player.video.currentTime, length);
  if (problem) return toast(problem);
  setTimeout(() => {
    const el = document.querySelector('.crow.sel .tx');
    if (!el) return;
    el.scrollIntoView({ block: 'nearest' });
    el.focus();
    getSelection().selectAllChildren(el);
  }, 30);
}

function split() {
  const problem = captions.split(player.video.currentTime);
  if (problem) toast(problem);
}

// Delete takes out what is selected: a caption, or a range on the Cuts lane.
function removeSelected() {
  if (job.busy()) return;
  const range = selection.selected();
  if (captions.selection() >= 0) captions.remove(captions.selection());
  else if (range?.in_edit) actions.correct(range.start, range.end, 'CUT');
}

function restoreSelected() {
  const range = selection.selected();
  if (range && !range.in_edit) actions.correct(range.start, range.end, 'KEEP');
}

const TYPING = ['text', 'search', 'email', 'number', 'password', 'textarea'];
const index = el => +el.dataset.i;

export function init() {
  register({
    addCaption: add,
    splitCaption: split,
    deleteSelected: removeSelected,
    restoreSelected,
    openCaption: el => {
      captions.select(index(el));
      const caption = captions.list()[index(el)];
      if (caption) player.seek(caption.s + 0.01);
    },
    selectCaption: el => captions.select(index(el)),
    editCaption: el => captions.setText(index(el), el.innerText.replace(/\s+/g, ' ').trim()),
    deleteCaption: el => captions.remove(index(el)),
    captionKey: (el, event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        el.blur();
      } else if (event.key === 'Escape') {
        el.innerText = captions.list()[index(el)].t;
        el.blur();
      }
    },
    searchCaptions: render,
  });
}

export function shortcuts({ undo, redo }) {
  document.addEventListener('keydown', event => {
    const target = event.target;
    if (TYPING.includes(target.type) || target.tagName === 'SELECT' || target.isContentEditable) return;
    const key = event.key.toLowerCase();
    const command = event.metaKey || event.ctrlKey;
    if (job.busy()) return;
    if (command && key === 'z') {
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    } else if (key === 'delete' || key === 'backspace') {
      if (captions.selection() < 0 && !selection.selected()?.in_edit) return;
      event.preventDefault();
      removeSelected();
    } else if (key === 's' && !command) {
      split();
    }
  });
}
