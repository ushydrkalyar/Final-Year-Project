// What was removed and how to change it: the Pacing tool's list, the Remove form, and the inspector of a range.
import { $, mmss, register } from './dom.js';
import * as actions from './actions.js';
import * as job from './job.js';
import * as selection from './selection.js';
import { toOriginal } from './timemap.js';
import { video } from './player.js';

export const NAMES = {
  speech_kept: 'Kept', pause_kept: 'Pause kept', pause_cut: 'Pause cut', pause_compressed: 'Pause compressed',
  filler_cut: 'Filler word', silence_cut: 'Silence', user_cut: 'Removed by you',
};

let mark = { in: null, out: null };

function removedList(timeline) {
  const removed = timeline.original_timeline.filter(segment => !segment.in_edit);
  const rows = removed.map(x => `<div class="cutrow"><span class="cdot" style="background:${esc(timeline.colour_map[x.type] || '#888')}"></span><div><b>${mmss(x.start)} – ${mmss(x.end)}</b> · ${x.duration.toFixed(1)}s<br><small>${esc(x.reason)}</small></div><button class="btn" data-click="restoreCut" data-start="${Number(x.start)}" data-end="${Number(x.end)}">Restore</button></div>`).join('');
  return `<h5 class="label">REMOVED FROM THE ORIGINAL (${removed.length})</h5>`
    + (rows || '<p class="note">Nothing was removed from this video.</p>');
}

function removeForm() {
  const valid = mark.in !== null && mark.out !== null && mark.out > mark.in;
  const show = t => (t === null ? '-' : mmss(t));
  return `<h5 class="label">REMOVE A SECTION</h5>
    <p class="note">Play the video, mark where the section starts and ends, then remove it.</p>
    <div class="cutmark"><button class="btn" data-click="markCut" data-value="in">Mark in</button><button class="btn" data-click="markCut" data-value="out">Mark out</button>
    <span>${show(mark.in)} to ${show(mark.out)}</span>
    <button class="btn mint" ${valid ? '' : 'disabled'} data-click="removeSection">Remove section</button></div>`;
}

export function render() {
  const edit = job.edit();
  $('cutBody').innerHTML = edit?.id && edit.timeline
    ? removedList(edit.timeline) + removeForm()
    : '<p class="note">Every section CutSmart removes is listed here after your first edit, and you can put any of it back.</p>';
}

export function sections() {
  const range = selection.selected();
  if (!range) return [['', '', '<p class="note">Click a range on the Cuts lane to see why it was kept or removed.</p>']];
  const button = range.in_edit
    ? '<button class="btn wide" data-click="removeRange">Remove</button>'
    : '<button class="btn mint wide" data-click="restoreRange">Restore</button>';
  return [['why', esc((NAMES[range.type] || 'Range').toUpperCase()), `
    <div class="row"><span>Original time</span><b>${range.start.toFixed(2)}s to ${range.end.toFixed(2)}s</b></div>
    <div class="row"><span>Length</span><b>${(range.end - range.start).toFixed(2)}s</b></div>
    <p class="note" id="cutWhy">${esc(range.reason || '')}</p>${button}`]];
}

export function clearMarks() {
  mark = { in: null, out: null };
}

function change(kind) {
  const range = selection.selected();
  if (range) actions.correct(range.start, range.end, kind);
}

export function init() {
  register({
    markCut: el => {
      mark[el.dataset.value] = video.currentTime || 0;
      render();
    },
    restoreCut: el => actions.correct(+el.dataset.start, +el.dataset.end, 'KEEP'),
    restoreRange: () => change('KEEP'),
    removeRange: () => change('CUT'),
    removeSection: () => {
      const kept = job.edit().timeline.edited_timeline;
      actions.correct(toOriginal(kept, mark.in, 'start'), toOriginal(kept, mark.out, 'end'), 'CUT');
    },
  });
}
