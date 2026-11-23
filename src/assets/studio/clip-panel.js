// The inspector for a clip: turning it, framing it, and how it meets the next one.
import { options, register } from './dom.js';
import { emit } from './bus.js';
import * as clips from './clips.js';
import * as framing from './framing.js';
import * as job from './job.js';
import { TRANSITIONS } from './style.js';

export function sections() {
  const clip = clips.current();
  if (!clip) return [];
  const index = clips.all().indexOf(clip);
  const turns = [0, 90, 180, 270].map(d => `<button class="${clip.rotate === d ? 'on' : ''}" data-click="rotate" data-value="${d}">${d}°</button>`).join('');
  const out = [
    ['', '', `<p class="note">Clip ${index + 1}: ${esc(clip.name)}</p>`],
    ['rotate', 'ROTATION', `<div class="row"><div class="sq">${turns}</div></div>${
      clips.count() > 1 ? '<button class="btn wide" data-click="rotateSame">Apply to all clips</button>' : ''
    }<p class="note">The arrows under the preview turn every clip. These turn only this one.</p>`],
    ['frame', 'FRAMING', framing.section()],
  ];
  if (index < clips.count() - 1) {
    out.push(['join', 'TRANSITION', `<div class="row">Into the next clip<select class="in w" id="clipTrans" data-change="setTransition">${options(TRANSITIONS, clip.trans)}</select></div>`]);
  }
  const span = job.edit()?.timeline?.clips.find(each => each.key === clip.id);
  const shown = !clips.hidesCaptions(span ?? { key: clip.id });
  const choice = (value, label, on) => `<button class="${on ? 'on' : ''}" data-click="clipCaptions" data-value="${value}">${label}</button>`;
  out.push(['caps', 'CAPTIONS ON THIS CLIP', `<div class="row"><div class="sq">${choice('on', 'Show', shown)}${choice('off', 'Hide', !shown)}</div></div>${
    span?.burned_captions ? '<p class="note">This clip already has captions in the picture. They are part of the video and can\'t be restyled or removed.</p>' : ''}`]);
  return out;
}

// Turn some clips: [[clip, degrees]].
function turn(changes) {
  if (job.busy()) return;
  const wanted = changes.filter(([clip, degrees]) => clip && clip.rotate !== degrees % 360);
  if (!wanted.length) return;
  emit('project:before');
  for (const [clip, degrees] of wanted) clips.rotate(clip.id, degrees);
  emit('rotation');
}

export const turnAll = by => turn(clips.all().map(clip => [clip, clip.rotate + by]));

export function init() {
  register({
    rotate: el => turn([[clips.current(), +el.dataset.value]]),
    rotateSame: () => turn(clips.all().map(clip => [clip, clips.current().rotate])),
    rotateOne: el => {
      const clip = clips.byId(el.dataset.id);
      if (clip) turn([[clip, clip.rotate + 90]]);
    },
    clipCaptions: el => clips.setCaptions(clips.current().id, el.dataset.value),
    setTransition: el => {
      if (!job.busy()) clips.setTransition(clips.current().id, el.value);
    },
  });
}
