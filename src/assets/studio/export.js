// Downloads: the captioned MP4 (burned on the server) and the .srt.
import { $, register, saveAs } from './dom.js';
import * as captions from './captions.js';
import * as job from './job.js';
import * as status from './status.js';

const MAX_MISSES = 5;
const POLL_MS = 700;
const RETRY_MS = 2500;

let running = false;

export const exporting = () => running;

export const fileName = extension =>
  ($('title').value.trim() || 'video').replace(/[\\/:*?"<>|]+/g, '-') + extension;

async function waitFor(rid) {
  for (let misses = 0; ;) {
    await new Promise(resolve => setTimeout(resolve, misses ? RETRY_MS : POLL_MS));
    let state;
    try {
      state = await apiJson(`/export-status/${rid}`, { signal: AbortSignal.timeout(20000) });
    } catch {
      if (++misses >= MAX_MISSES) throw new Error('Lost contact with the server during export. Try again.');
      continue;
    }
    misses = 0;
    status.foot(`Exporting… ${Math.max(2, state.progress || 0)}%`);
    if (state.status === 'error') throw new Error(state.error || 'Export failed');
    if (state.status === 'done') return;
  }
}

async function exportVideo() {
  const id = job.editId();
  if (!id || job.busy() || running) return;
  running = true;
  status.controls({ exporting: true });
  try {
    const { rid } = await apiJson(`/export/${id}`,
      { method: 'POST', body: captions.fields(), fallback: 'Export failed' });
    await waitFor(rid);
    // streamed straight to disk by the browser, never held in the tab
    const name = fileName('.mp4');
    saveAs(`/export-result/${rid}?name=${encodeURIComponent(name)}`, name);
    status.foot('Exported');
    toast('Your video is downloading');
  } catch (error) {
    toast(error.message);
    status.foot('Export failed');
  } finally {
    running = false;
    status.controls();
  }
}

function timestamp(seconds) {
  const ms = Math.round(seconds * 1000);
  const parts = [Math.floor(ms / 3600000), Math.floor(ms / 60000) % 60, Math.floor(ms / 1000) % 60];
  return `${parts.map(n => String(n).padStart(2, '0')).join(':')},${String(ms % 1000).padStart(3, '0')}`;
}

function downloadSrt() {
  const list = captions.list();
  if (!list.length) return;
  const text = list.map((c, i) => `${i + 1}\n${timestamp(c.s)} --> ${timestamp(c.e)}\n${c.t}\n`).join('\n');
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  saveAs(url, fileName('.srt'));
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

export function init() {
  register({ exportVideo, downloadSrt });
}
