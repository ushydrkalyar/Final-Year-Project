// The edit job's life, as one state. No DOM.
//
//   idle -> uploading -> processing -> ready
//              |             |
//              +-------------+------> error
//
// `current` is the last finished edit. It stays usable while a newer one is made, and if that fails.
import { emit } from './bus.js';

const MAX_MISSES = 5;
const RETRY_MS = 2500;
const TIMEOUT_MS = 20000;
const EXPIRED = [404, 409];

let phase = 'idle';
let current = null; // {id, blob, timeline, report, sig}; id is null once it expired on the server
let pending = null; // {id, sig, copyLook, matched, reopened}
let run = 0;
let timer = null;

export const state = () => phase;
export const busy = () => phase === 'uploading' || phase === 'processing';
export const edit = () => current;
export const making = () => pending;
export const hasResult = () => current !== null;
export const editId = () => current?.id ?? null;

function go(next) {
  phase = next;
  emit('job:state', next);
}

const settled = () => (current ? 'ready' : 'idle');

function stopPolling() {
  clearTimeout(timer);
  run++;
}

function fail(message) {
  stopPolling();
  pending = null;
  go('error');
  emit('job:failed', { message, previous: !!editId() });
}

// Send a request that makes an edit, then follow it. `target` is {sig, ...} of the edit being made.
//   resend:    a body to try once more with, when what `body` built on has expired
//   label:     what the busy panel says
//   onRefused: given for changes to an existing edit, where a refusal leaves that edit as it was
export async function submit({ path, body, target, label, startAt, onRefused, fallback, resend }) {
  if (busy()) return;
  stopPolling();
  go('uploading');
  emit('job:submitting', { label, replacesView: !onRefused });
  try {
    const send = form => apiJson(path, { method: 'POST', body: form, fallback });
    const data = await send(body).catch(error => {
      if (resend && EXPIRED.includes(error.status)) return send(resend());
      throw error;
    });
    pending = { id: data.job_id, ...target, reopened: null };
    go('processing');
    emit('job:accepted', { label, startAt, replacesView: !!onRefused });
    poll();
  } catch (error) {
    if (!onRefused) return fail(error.message);
    go(settled());
    emit('job:refused');
    onRefused(error.message);
  }
}

// One request in flight; gives up after MAX_MISSES failures in a row.
function poll() {
  stopPolling();
  const mine = run;
  let misses = 0;
  const again = () => {
    if (mine === run) timer = setTimeout(tick, RETRY_MS);
  };
  const tick = async () => {
    let status;
    try {
      status = await apiJson(`/status/${pending.id}`, { signal: AbortSignal.timeout(TIMEOUT_MS) });
    } catch (error) {
      if (mine !== run) return;
      if (++misses < MAX_MISSES) return again();
      return fail(error.status === 404 ? 'Server was restarted. Please generate again.'
        : error.status ? 'The server could not report on this edit.' : 'Could not reach the server.');
    }
    if (mine !== run) return;
    misses = 0;
    if (status.status === 'error') return fail(status.error || 'Processing failed');
    if (status.status === 'done') {
      stopPolling();
      return load();
    }
    emit('job:progress', { label: status.step_label || 'Processing', percent: status.progress || 0 });
    again();
  };
  again();
}

const optional = path => apiJson(path).catch(() => null);

async function load() {
  const { id, reopened, ...target } = pending;
  try {
    const timeline = await optional(`/timeline/${id}`);
    const report = (await optional(`/report/${id}`))?.report ?? null;
    const blob = await (await api(`/intermediate/${id}`, { fallback: 'Could not load the edit' })).blob();
    const previous = current;
    current = { id, sig: target.sig, timeline, report, blob };
    pending = null;
    go('ready');
    emit('job:ready', { edit: current, previous, made: target, captions: reopened?.captions ?? null });
  } catch (error) {
    if (!reopened) return fail(error.message);
    if ([403, 404, 409].includes(error.status)) return lost();
    unreachable();
  }
}

// Undo and Redo put an edit made earlier back on screen: nothing is rendered.
export function show(edit) {
  stopPolling();
  pending = null;
  current = edit;
  go(settled());
}

function lost() {
  pending = null;
  go(settled());
  emit('job:lost');
}

function unreachable() {
  pending = null;
  go(settled());
  emit('job:unreachable');
}

// Open a project's saved job as that edit, or resume it. `saved` is {id, captions, target()}.
export async function reopen({ id, captions, target }) {
  if (busy()) return;
  let status;
  try {
    status = await apiJson(`/status/${id}`, { signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch (error) {
    if (busy()) return;
    return error.status === 404 || error.status === 403 ? lost() : unreachable();
  }
  if (busy()) return; // Generate was pressed while we asked
  if (status.status === 'error') {
    return fail(`Your last edit failed: ${status.error || 'Processing failed'}`);
  }
  if (!['queued', 'processing', 'done'].includes(status.status)) return unreachable();
  const done = status.status === 'done';
  emit('job:reopening', { done });
  pending = { id, ...target(), reopened: done ? { captions } : null };
  go('processing');
  if (done) await load();
  else poll();
}

// The server no longer has the edit on screen.
export function expire() {
  if (!current) return;
  current = { ...current, id: null };
  emit('job:state', phase);
}
