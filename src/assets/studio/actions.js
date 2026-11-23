// What asks the server for a new cut: generate, restore or remove a range, match a reference.
import { $, register } from './dom.js';
import * as clips from './clips.js';
import * as framing from './framing.js';
import * as job from './job.js';
import { st } from './state.js';

const TRANSITION_S = 0.5;

// The finished clip each clip of the project can build on, by its key.
function finished(edit) {
  return new Map(edit?.id ? (edit.timeline?.clips ?? []).map(clip => [clip.key, clip.job_id]) : []);
}

function request({ base, link, copyStyle, match, preset }) {
  const body = new FormData();
  const built = finished(base);
  let uploads = 0;
  const plan = clips.all().map(clip => {
    const entry = { key: clip.id, rotate: clip.rotate, frame: clips.frameFields(clip), transition: clip.trans };
    if (built.has(clip.id)) return { ...entry, job: built.get(clip.id) };
    body.append('video_file', clip.file, clip.name);
    return { ...entry, upload: uploads++ };
  });
  body.append('clips', JSON.stringify(plan));
  if (base?.id) body.append('base_job', base.id);
  body.append('style_preset', preset);
  if (st.maxPause !== null) body.append('max_pause', st.maxPause);
  if (st.compressPause !== null) body.append('compress_pause', st.compressPause);
  body.append('language', st.lang);
  body.append('color_grade', st.grade);
  body.append('cv_analysis', '1');
  body.append('transition_duration', TRANSITION_S);
  body.append('reference_link', link);
  body.append('require_reference', match ? 1 : 0);
  body.append('copy_caption_style', copyStyle ? 1 : 0);
  return body;
}

export function generate({ link = '', copyStyle = false } = {}) {
  if (job.busy() || !clips.count()) return;
  framing.closeEditor();
  const base = job.edit();
  const options = { link, copyStyle, match: !!link && !!base?.id, preset: st.stylePreset };
  return job.submit({
    path: '/edit-video/',
    body: request({ ...options, base }),
    resend: () => request({ ...options, base: null }),
    target: { sig: clips.signature(), copyLook: copyStyle, matched: !!link },
    label: link ? 'Matching reference pacing…' : base?.id ? 'Updating what changed…' : undefined,
    fallback: 'Submission failed',
  });
}

// `start` and `end` are on the original timeline. A correction keeps the edit's other settings.
export function correct(start, end, kind) {
  const id = job.editId();
  if (!id || job.busy()) return;
  const body = new FormData();
  body.append('job_id', id);
  body.append('timestamp_start', start.toFixed(3));
  body.append('timestamp_end', end.toFixed(3));
  body.append('correction', kind);
  let refused = '';
  return job.submit({
    path: '/feedback/', body, target: { sig: job.edit().sig }, startAt: 10,
    label: kind === 'KEEP' ? 'Restoring that section…' : 'Removing that section…',
    fallback: 'Could not apply that change',
    onRefused: message => {
      refused = message;
      toast(message);
    },
  }).then(() => refused);
}

function matchReference() {
  const link = ($('refLink').value || '').trim();
  const note = $('refStatus');
  if (!link) {
    note.style.color = 'var(--danger)';
    note.textContent = 'Paste a reference video link first.';
    return;
  }
  note.textContent = '';
  return generate({ link, copyStyle: $('refCopyStyle').checked });
}

export function init() {
  register({
    generate: () => generate(),
    retry: () => generate(),
    matchReference,
    clearReferenceNote: () => { $('refStatus').textContent = ''; },
  });
}
