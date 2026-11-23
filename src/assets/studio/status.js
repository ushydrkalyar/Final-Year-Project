// The job's state over the preview, the main buttons, and the line in the top bar.
import { $ } from './dom.js';
import * as clips from './clips.js';
import * as job from './job.js';
import { st } from './state.js';

function panel(title, body, buttons = '') {
  const box = $('state');
  box.innerHTML = `<h3>${title}</h3>${body}${buttons ? `<div class="acts">${buttons}</div>` : ''}`;
  box.hidden = false;
}

const button = (label, action, kind = 'mint') => `<button class="btn ${kind}" data-click="${action}">${label}</button>`;

export function clear() {
  $('state').hidden = true;
}

export function empty() {
  panel('No video yet', '<p>Upload a video from Home to begin.</p>', button('Go to Home', 'goHome'));
}

export function ready() {
  clear();
  foot('');
}

export function working(label, percent) {
  panel('Editing your video', `<p id="bl">${esc(label || 'Cutting the silences and writing your captions…')}</p><div class="pbar"><i id="bp" style="width:${percent || 4}%"></i></div>`);
}

export function progress({ label, percent }) {
  if ($('bp')) $('bp').style.width = `${Math.max(4, percent)}%`;
  if ($('bl')) $('bl').textContent = `${label} · ${percent}%`;
}

export function failed({ message, previous }) {
  const back = previous ? button('Back to your last edit', 'backToEdit', '') : '';
  panel('Something went wrong', `<p>${esc(message)}</p>`, button('Try again', 'retry') + back);
  foot('Failed');
}

export function unreachable() {
  panel('Could not reach the server', '<p>Your edit is saved. Try again in a moment.</p>', button('Retry', 'reopen'));
}

export function foot(text) {
  $('foot').textContent = text;
  $('foot').title = text;
}

// The main button exists only for work the server must do: the first edit, or a re-edit after
// something changed the cut itself (trimming, colour, language, rotation, framing).
export function needsReedit() {
  const edit = job.edit();
  return !!edit?.id && edit.sig !== clips.signature();
}

export function controls({ exporting = false } = {}) {
  const busy = job.busy();
  const editable = !!job.editId();
  const hasVideo = clips.count() > 0;
  const main = $('genB');
  main.disabled = busy || !hasVideo;
  $('expB').disabled = $('expMore').disabled = busy || !editable || exporting;
  $('srtB').disabled = busy || !editable;
  $('refSubmitBtn').disabled = busy;
  $('ratio').disabled = $('rotL').disabled = $('rotB').disabled = busy || !hasVideo;
  $('ratio').value = st.ratio;
  $('addB').disabled = busy || !hasVideo || clips.count() >= clips.MAX;
  $('steps').hidden = busy || !hasVideo || job.hasResult();
  $('paceFoot').hidden = busy || !editable || !needsReedit();
  if (busy) return; // keep the label the work was started with
  main.textContent = editable ? 'Re-edit video' : 'Generate edit';
  main.style.display = !editable || needsReedit() ? '' : 'none';
}
