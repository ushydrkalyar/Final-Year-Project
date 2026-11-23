// The Comments tool: one typed line changes a caption or, with a time, the cut.
import { $, mmss, register } from './dom.js';
import { emit } from './bus.js';
import * as actions from './actions.js';
import * as captions from './captions.js';
import * as history from './history.js';
import * as job from './job.js';
import * as timeline from './timeline.js';
import { resolveRequest } from './timemap.js';

const MAX_SHOWN = 30;

let entries = []; // [{text, result, change}], newest first; `change` is the history entry it made
let awaiting = null; // a cut comment whose edit is being made

export const saved = () => entries.map(({ text, result }) => ({ text, result }));

export function restore(list) {
  entries = Array.isArray(list) ? list.slice(0, MAX_SHOWN) : [];
  render();
}

export function render() {
  const last = history.top();
  $('cmtList').innerHTML = entries.length ? `<h5 class="label">APPLIED</h5>${entries.map(entry => `<div class="cutrow"><div><b>${esc(entry.text)}</b><br><small>${esc(entry.result)}</small></div>${
    entry.change && entry.change === last && !job.busy() ? '<button class="btn" data-click="undo">Undo</button>' : ''}</div>`).join('')}` : '';
}

function say(text, bad = false) {
  const message = $('cmtMsg');
  message.style.color = bad ? 'var(--danger)' : 'var(--primary)';
  message.textContent = text;
  return message;
}

function applied(text, result) {
  entries.unshift({ text, result, change: history.top() });
  entries.length = Math.min(entries.length, MAX_SHOWN);
  say(result);
  render();
  emit('comments');
}

export function settled(ok) {
  if (!awaiting) return;
  const { text, result } = awaiting;
  awaiting = null;
  if (ok) applied(text, result);
  else say('That change failed. The edit is as it was.', true);
}

async function applyCut(text, request) {
  const edit = job.edit();
  const list = captions.list();
  const found = resolveRequest({
    timeline: edit.timeline, request, view: timeline.view(),
    speech: list.length ? { first: list[0].s, last: list[list.length - 1].e } : null,
  });
  if (found.problem) return say(found.problem, true);
  const verb = request.action === 'KEEP' ? 'Restored' : 'Removed';
  awaiting = { text, result: `${verb} ${mmss(found.start)} to ${mmss(found.end)} of the original` };
  $('cmtIn').value = '';
  say(request.action === 'KEEP' ? 'Putting that back…' : 'Cutting that…');
  const refused = await actions.correct(found.start, found.end, request.action);
  if (!refused) return;
  awaiting = null;
  $('cmtIn').value = text;
  say(refused, true);
}

function candidates(found) {
  const box = document.createElement('div');
  for (const { index, text } of found) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'cmtcand';
    button.dataset.click = 'pickCandidate';
    button.dataset.i = Number(index);
    button.textContent = `#${Number(index) + 1}: "${text.slice(0, 30)}${text.length > 30 ? '…' : ''}"`;
    box.append(button);
  }
  return box;
}

async function ask() {
  const input = $('cmtIn'), send = $('cmtBtn');
  const text = (input.value || '').trim();
  if (!text || job.busy() || send.disabled) return;
  if (!job.editId()) return say('Generate your edit first.', true);
  send.disabled = input.disabled = true;
  $('cmtMsg').textContent = '';
  try {
    const form = new FormData();
    form.append('comment', text);
    form.append('captions', captions.wire());
    const answer = await apiJson(`/caption-command/${job.editId()}`,
      { method: 'POST', body: form, fallback: 'Could not reach the server' });
    if (!answer.ok) {
      const message = say(answer.message || "I couldn't understand that.", true);
      if (answer.candidates?.length) message.append(candidates(answer.candidates));
    } else if (answer.kind) {
      await applyCut(text, answer);
    } else {
      captions.replaceAll(answer.captions, answer.index);
      input.value = '';
      applied(text, answer.message);
    }
  } catch (error) {
    say(error.message || 'Something went wrong. Try again.', true);
  } finally {
    send.disabled = input.disabled = false;
  }
}

export function init() {
  register({
    ask,
    askKey: (el, event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      ask();
    },
    pickCandidate: el => {
      const input = $('cmtIn');
      let text = input.value.trim();
      if (!/\bcaption\s*#?\s*\d+\b/i.test(text)) text += ` in caption ${+el.dataset.i + 1}`;
      input.value = text;
      ask();
    },
  });
}
