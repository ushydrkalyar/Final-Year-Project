// The preview video: playback, the seek bar and the clock.
import { $, fmt, register } from './dom.js';
import * as captions from './captions.js';
import * as clips from './clips.js';
import * as job from './job.js';
import { media } from './state.js';
import { clipAt } from './timemap.js';

export const video = $('vid');

const PLAY = '<path d="M7 4l13 8-13 8z" fill="currentColor"/>';
const PAUSE = '<path d="M7 4h4v16H7zM13 4h4v16h-4z" fill="currentColor"/>';

let resultUrl = '';

export const length = () => video.duration || media.duration || 0;

export function showUpload(url) {
  video.src = url;
  video.style.display = 'block';
  $('noVid').style.display = 'none';
}

export function showEdit(blob) {
  const previous = resultUrl;
  resultUrl = URL.createObjectURL(blob);
  video.src = resultUrl;
  if (previous) URL.revokeObjectURL(previous);
}

export function seek(seconds) {
  if (video.duration) video.currentTime = seconds;
}

export function updateTime() {
  $('time').textContent = `${fmt(video.currentTime)} / ${fmt(length())}`;
  const played = video.duration ? video.currentTime / video.duration : 0;
  $('seekI').style.width = `${played * 100}%`;
  const now = captions.list().findIndex(c => video.currentTime >= c.s && video.currentTime < c.e);
  document.querySelectorAll('.crow').forEach(row => row.classList.toggle('act', +row.dataset.i === now));
}

// The picture fills the stage, turned by whatever rotation is pending
// (a finished edit already comes back upright).
export function fit() {
  if (!video.videoWidth) return;
  const span = job.hasResult() ? clipAt(job.edit().timeline, video.currentTime || 0) : null;
  const clip = span ? clips.byId(span.key) : clips.current();
  const degrees = ((clip?.rotate ?? 0) - (span?.rotate ?? 0) + 360) % 360;
  const { videoWidth: w, videoHeight: h } = video;
  const stage = $('stage');
  const turned = degrees % 180 !== 0;
  const scale = Math.min(stage.clientWidth / (turned ? h : w), stage.clientHeight / (turned ? w : h));
  Object.assign(video.style, {
    width: `${w * scale}px`, height: `${h * scale}px`, transform: degrees ? `rotate(${degrees}deg)` : '',
  });
  // the caption layer sits on the picture: JASSUB places its canvas from its container's left edge,
  // so the container must start where the picture does (it doesn't when the picture is narrower)
  const layer = stage.querySelector(':scope > .JASSUB');
  if (layer) {
    Object.assign(layer.style, {
      left: `${(stage.clientWidth - w * scale) / 2}px`, top: `${(stage.clientHeight - h * scale) / 2}px`,
      width: `${w * scale}px`, height: `${h * scale}px`,
    });
    // JASSUB worked out its canvas offset from where the container was; it recomputes only on resize
    for (const canvas of layer.querySelectorAll('canvas')) Object.assign(canvas.style, { top: '0px', left: '0px' });
  }
  const source = $('vsrc');
  if (!source.videoWidth) return;
  const k = Math.min(stage.clientWidth / source.videoWidth, stage.clientHeight / source.videoHeight);
  Object.assign(source.style, { width: `${source.videoWidth * k}px`, height: `${source.videoHeight * k}px` });
}

function togglePlay() {
  if (!video.src) return;
  if (video.paused) video.play();
  else video.pause();
}

export function init() {
  video.addEventListener('timeupdate', updateTime);
  const icon = () => { $('playI').innerHTML = video.paused ? PLAY : PAUSE; };
  video.addEventListener('play', icon);
  video.addEventListener('pause', icon);
  $('vsrc').addEventListener('loadedmetadata', fit);
  document.addEventListener('keydown', event => {
    if (event.code !== 'Space' || event.repeat || event.metaKey || event.ctrlKey) return;
    if (event.target.closest?.('input, textarea, select, button, summary, a, [contenteditable], [role=button]')) return;
    event.preventDefault();
    togglePlay();
  });
  register({
    togglePlay,
    toggleMute: () => { video.muted = !video.muted; },
    fullscreen: () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else $('stage').requestFullscreen?.(); // the stage, so the live captions come along
    },
    seekBar: (el, event) => {
      const box = el.getBoundingClientRect();
      seek((event.clientX - box.left) / box.width * video.duration);
    },
  });
}
