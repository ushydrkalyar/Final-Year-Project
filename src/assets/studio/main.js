// Studio's composition root: the only module that connects models to views.
import { $, listen, register } from './dom.js';
import { on } from './bus.js';
import * as actions from './actions.js';
import * as captionEditor from './caption-editor.js';
import * as captions from './captions.js';
import * as clipPanel from './clip-panel.js';
import * as clips from './clips.js';
import * as comments from './comments.js';
import * as cuts from './cuts.js';
import * as downloads from './export.js';
import * as fonts from './fonts.js';
import * as framing from './framing.js';
import * as history from './history.js';
import * as inspector from './inspector.js';
import * as job from './job.js';
import * as layout from './layout.js';
import * as mediaPanel from './media-panel.js';
import * as pacing from './pacing.js';
import * as player from './player.js';
import * as position from './position.js';
import * as preview from './preview.js';
import * as project from './project.js';
import * as referencePanel from './reference-panel.js';
import * as report from './report.js';
import * as selection from './selection.js';
import * as status from './status.js';
import * as stylePanel from './style-panel.js';
import * as templates from './templates.js';
import * as timeline from './timeline.js';
import { kept, rebase } from './remap.js';
import { fromRecord } from './restore-clips.js';
import { media, restore, st } from './state.js';
import { PRESET_KEYS, TEMPLATE_LAYOUT, cleanStyle, persistable } from './style.js';
import { clipAt } from './timemap.js';

const TURN_MS = 900; // several presses of a turn button are one change
const LIVE_FOOT = 'Every change previews instantly. Export burns it into the video.';
const REEDIT_FOOT = 'The cut settings changed. Press Re-edit video to apply them.';
const CUT_SETTINGS = ['stylePreset', 'grade', 'lang', 'maxPause', 'compressPause', 'ratio'];
// everything that makes the look, so Undo can put it back
const LOOK = [...new Set([...PRESET_KEYS, ...Object.keys(TEMPLATE_LAYOUT), 'preset', 'y'])];
const COMMENT_HELP = view => "Fix a caption (change caption 3 to hello), or change the cut: cut 0:05 to 0:08, "
  + `keep the pause at 0:12, remove the silence at the start. Times follow the ${view === 'original' ? 'Original' : 'Edited'} timeline.`;

let live = null; // LiveCaptions, created with the first track
let placed = []; // where that track's captions sit
let carried = null; // {list, ranges}: caption edits on their way into the cut being made
let storing = Promise.resolve();
let restructured = false; // the change being made is to the clips themselves
let turnTimer = null;

// ---- saving

// Saved caption edits always come with the cut they are timed on.
function snapshot() {
  const base = { style: persistable(st), clips: clips.saved(), comments: comments.saved() };
  const pending = job.making();
  if (pending) {
    const reopened = pending.reopened?.captions ?? null;
    return {
      ...base, caps: carried?.list ?? reopened, capsFrom: carried?.ranges ?? null, capEdited: !!(carried || reopened),
    };
  }
  const caps = captions.isEdited() ? captions.list() : null;
  return { ...base, caps, capsFrom: caps ? kept(job.edit()?.timeline) : null, capEdited: !!caps };
}

function remember(state, extra = {}) {
  const target = job.making() ?? job.edit();
  project.update({
    status: state,
    ...(target?.id && { jobId: target.id, pipe: target.sig }),
    ...snapshot(),
    ...extra,
  });
}

function storeClips() {
  const list = clips.all().map(({ id, file }) => ({ id, file }));
  storing = storing.then(() => project.storeClips(list))
    .catch(error => toast(error.message || 'Could not save these clips in this browser'));
  project.update({
    dur: clips.all().reduce((sum, clip) => sum + (clip.dur || 0), 0),
    clips: clips.saved(),
  });
}

// ---- Undo: the project as it was

function projectState(captionState) {
  return {
    edit: job.edit(),
    captions: captionState ?? captions.snapshot(),
    clips: clips.snapshot(),
    settings: Object.fromEntries(CUT_SETTINGS.map(key => [key, st[key]])),
    look: lookNow(),
  };
}

const lookNow = () => Object.fromEntries(LOOK.map(key => [key, st[key]]));
// The look as it was when nothing was changing: controls change a setting before saying so.
let settledLook = null;
on('style:before', () => history.restyle({ ...projectState(), look: settledLook ?? lookNow() }));

on('project:before', change => {
  if (change?.captions) history.push(projectState(change.captions));
  else history.hold(projectState());
});

function goTo(state) {
  if (!state) return;
  const before = job.edit();
  const replaced = !!state.edit && state.edit !== before;
  const reordered = state.clips.map(clip => clip.id).join() !== clips.all().map(clip => clip.id).join();
  framing.closeEditor();
  captions.invalidate();
  Object.assign(st, state.settings, state.look);
  position.settled();
  clips.reset(state.clips.map(clip => ({ ...clip, frame: { ...clip.frame } })));
  if (reordered) storeClips();
  if (replaced) {
    job.show(state.edit);
    status.clear();
    showResult(state.edit);
  }
  captions.adopt(state.captions);
  selection.select(null);
  pacing.render();
  templates.renderList();
  preview.update();
  captionEditor.render();
  if (job.hasResult()) timeline.build();
  renderPanels();
  placeOverlays();
  refreshControls();
  settledLook = lookNow();
  remember(job.editId() ? 'done' : 'draft', replaced ? { editedDur: state.edit.timeline?.edited_duration } : {});
  if (job.editId()) {
    status.foot(status.needsReedit() ? REEDIT_FOOT : LIVE_FOOT);
    captions.refresh(true);
  }
}

// ---- drawing

// Everything that sits on top of the picture follows it.
function placeOverlays() {
  player.fit();
  framing.draw();
  preview.update();
  position.changed();
}

// A clip is turned differently from the edit on screen: that edit's captions would sit wrong on it.
function turning() {
  const spans = job.edit()?.timeline?.clips ?? [];
  return spans.some(span => {
    const clip = clips.byId(span.key);
    return clip && clip.rotate !== span.rotate;
  });
}

// What the captions are drawn on, while they can be dragged.
function captionLayer() {
  if (job.busy() || !clips.count() || $('fedit').style.display !== 'none') return null;
  if (!job.hasResult()) return $('ovimg').style.display === 'none' ? null : $('ovimg');
  return turning() ? null : live?.canvas() ?? null;
}

// On the clip being watched: your captions are hidden because it came with its own.
function burnNote() {
  const span = job.hasResult() && !job.busy() ? clipAt(job.edit().timeline, player.video.currentTime || 0) : null;
  $('burnNote').hidden = !span?.burned_captions || clips.byId(span.key)?.captions !== 'auto';
}

function historyButtons() {
  $('tUndo').disabled = job.busy() || !(turnTimer || history.canUndo());
  $('tRedo').disabled = job.busy() || !history.canRedo();
  comments.render();
}

function renderPanels() {
  inspector.render();
  pacing.render();
  mediaPanel.render();
}

function showResult(edit) {
  player.showEdit(edit.blob);
  cuts.render();
  report.render(edit.report);
  referencePanel.render(edit.timeline?.reference);
}

// ---- settings

async function applyTurn() {
  turnTimer = null;
  if (!job.editId() || job.busy()) return;
  if (status.needsReedit()) {
    restructured = true;
    await actions.generate();
  } else { // turned all the way round
    history.settle(false);
    settingsChanged();
    placeOverlays();
  }
}

function refreshControls() {
  status.controls({ exporting: downloads.exporting() });
  historyButtons();
}

// Saved; and once there is an edit, shown on it (unless the cut itself is out of date).
function settingsChanged() {
  project.save();
  refreshControls();
  if (!job.editId() || job.busy()) return;
  status.foot(status.needsReedit() ? REEDIT_FOOT : LIVE_FOOT);
  // the look is live even while a cut waits for Re-edit: it is drawn on the edit being shown
  captions.refresh();
}

on('style', ({ rerender }) => {
  if (rerender) inspector.render();
  preview.update();
  settingsChanged();
});
on('template', ({ rerender }) => {
  if (rerender) inspector.render();
  pacing.render();
  preview.update();
  settingsChanged();
});
on('positioned', () => {
  inspector.render();
  if (job.editId()) captions.refresh(true);
});
on('preview:painted', () => {
  position.settled();
  position.changed();
});
on('clips:captions', () => {
  project.save();
  burnNote();
  inspector.render();
  timeline.drawBlocks();
  if (job.editId() && !job.busy()) captions.refresh(true);
});
on('panel', renderPanels);
on('inspect', kind => inspector.show(kind));
on('cut-settings', settingsChanged);
// Turning applies by itself once the presses stop.
on('rotation', () => {
  clearTimeout(turnTimer);
  turnTimer = null;
  project.save();
  position.settled();
  renderPanels();
  refreshControls();
  if (!job.editId()) {
    history.settle(true);
    placeOverlays();
    return;
  }
  live?.hide();
  placeOverlays();
  status.foot('Turning your video…');
  turnTimer = setTimeout(applyTurn, TURN_MS);
  historyButtons();
});
on('layout', () => {
  placeOverlays();
  timeline.drawWave();
  timeline.drawPlayhead();
});
on('frame:moving', () => {
  framing.draw();
  preview.update();
});
on('frame', () => {
  inspector.render();
  placeOverlays();
  settingsChanged();
});
on('history', () => {
  settledLook = lookNow();
  historyButtons();
});
on('comments', () => project.save());
on('timeline:view', view => { $('cmtHelp').textContent = COMMENT_HELP(view); });
on('range:selected', range => {
  timeline.markRange();
  captionEditor.markSelection(captions.selection());
  inspector.render();
  if (range) inspector.show('cut');
  else inspector.leave('cut');
});

// ---- clips

async function measure(clip) {
  clip.url ||= URL.createObjectURL(clip.file);
  if (clip.w && clip.thumb) return;
  const size = new Promise(resolve => {
    const probe = document.createElement('video');
    probe.preload = 'metadata';
    probe.muted = true;
    probe.onloadedmetadata = () => resolve({ w: probe.videoWidth, h: probe.videoHeight, dur: probe.duration });
    probe.onerror = () => resolve({});
    probe.src = clip.url;
  });
  clips.measured(clip.id, await size);
  if (clips.current()?.id === clip.id) {
    inspector.render();
    placeOverlays();
    refreshControls();
  }
  CutSmartProjects.videoMeta(clip.file).then(({ thumb }) => {
    clips.measured(clip.id, { thumb });
    mediaPanel.render();
  });
}

function showUpload() {
  const clip = clips.current();
  if (!clip || job.hasResult()) return;
  clip.url ||= URL.createObjectURL(clip.file);
  player.showUpload(clip.url);
}

on('clips:selected', () => {
  framing.closeEditor();
  showUpload();
  renderPanels();
  placeOverlays();
});
on('clip:chosen', id => {
  inspector.show('clip');
  const span = job.edit()?.timeline?.clips.find(clip => clip.key === id);
  if (span) player.seek(span.edited_start + 0.01);
});
on('clip:picked', id => {
  clips.select(id);
  inspector.show('clip');
});
// Once there is an edit, adding, removing or reordering applies right away: only what changed is made again.
on('clips:structure', async () => {
  restructured = true;
  await Promise.all(clips.all().map(measure));
  const turned = clips.inheritTurn();
  if (turned) toast('Turned the same way as your other clips. Turn it back in Media if that is wrong.');
  storeClips();
  showUpload();
  renderPanels();
  refreshControls();
  if (job.editId()) await actions.generate();
  else {
    restructured = false;
    history.settle(true);
  }
});

// ---- the job

on('job:state', () => {
  refreshControls();
  mediaPanel.render();
  timeline.markRange();
});
on('job:progress', status.progress);
on('job:submitting', ({ label, replacesView }) => {
  clearTimeout(turnTimer);
  turnTimer = null;
  $('burnNote').hidden = true;
  $('grab').hidden = true;
  position.settled();
  if (job.hasResult()) {
    history.hold(projectState());
    carried = captions.isEdited() ? { list: captions.snapshot().items, ranges: kept(job.edit().timeline) } : null;
  }
  captions.invalidate();
  if (!replacesView) return;
  live?.hide();
  status.foot('Working…');
  status.working(label);
  framing.draw();
});
on('job:accepted', ({ label, startAt, replacesView }) => {
  project.cancel();
  remember('processing');
  if (!replacesView) return;
  cuts.clearMarks();
  $('refStatus').textContent = '';
  status.working(label, startAt);
});
on('job:reopening', ({ done }) => {
  status.working(done ? 'Opening your edit…' : 'Still working on your edit…', done ? 60 : 30);
  renderPanels();
});
on('job:ready', async ({ edit, previous, made, captions: saved }) => {
  const moving = saved ? null : carried;
  if (made.copyLook && edit.timeline?.caption_look) {
    Object.assign(st, cleanStyle(edit.timeline.caption_look), { preset: 'custom', em: false });
    renderPanels();
  }
  history.settle(!!previous);
  restructured = false;
  status.clear();
  showResult(edit);
  selection.select(null);
  captions.reset(saved);
  preview.update();
  status.foot(status.needsReedit() ? REEDIT_FOOT : LIVE_FOOT);
  await captions.refresh(true);
  if (job.edit() !== edit || job.busy()) return; // replaced while the captions loaded
  if (moving) {
    const items = rebase(moving.list, moving.ranges, kept(edit.timeline), captions.list());
    captions.adopt({ items, edited: true });
    await captions.refresh(true);
    if (job.edit() !== edit || job.busy()) return;
  }
  carried = null;
  remember('done', { editedDur: edit.timeline?.edited_duration || media.duration });
  CutSmartProjects.videoMeta(edit.blob).then(({ thumb }) => thumb && project.update({ thumb })); // upright, reframed
  placeOverlays();
  refreshControls();
  captionEditor.render();
  timeline.build();
  comments.settled(true);
  if (made.matched && !edit.timeline?.reference_used) toast("Couldn't read that reference, so the preset's pacing was used");
  else if (made.copyLook && !edit.timeline?.caption_look) toast("Pacing matched. That reference's captions couldn't be read");
  else toast('Edit ready: caption changes show instantly');
});
on('job:refused', () => {
  history.settle(false);
  carried = null;
});
on('job:failed', failure => {
  history.settle(restructured); // the clips did change, even if their edit failed
  restructured = false;
  comments.settled(false);
  carried = null;
  if (failure.previous) remember('done'); // the edit from before this attempt is still good
  else project.update({ status: 'error', jobId: null });
  status.failed(failure);
  framing.draw();
});
on('job:lost', () => {
  carried = null;
  project.update({ status: 'draft', jobId: null });
  status.ready();
  toast('Your previous edit has expired on the server. Press Generate to edit this video again.');
});
on('job:unreachable', status.unreachable);

// ---- captions

on('notice', message => toast(message));
on('captions:selected', index => {
  captionEditor.markSelection(index);
  if (index < 0) return;
  if (selection.selected()) selection.select(null);
  inspector.show('text');
});
on('captions:expired', () => toast('This edit has expired on the server. Press Generate edit to make it again.'));
on('captions:track', ({ ass, boxes, regrouped }) => {
  placed = boxes;
  if (!live) {
    live = LiveCaptions.create(player.video, {
      onPaint: fresh => {
        if (fresh) position.settled();
        position.changed();
      },
    });
    player.fit(); // its layer exists now: put it on the picture
  }
  if (turning()) live.hide();
  else live.show(ass);
  burnNote();
  if (regrouped) {
    captionEditor.render();
    timeline.drawBlocks();
    timeline.drawStat();
  }
  project.save();
});
on('captions:edited', () => {
  captionEditor.render();
  timeline.drawBlocks();
  timeline.drawStat();
  historyButtons();
  project.save();
  if (!job.busy()) captions.refresh();
});

// ---- the page

function reopen() {
  const record = project.record();
  if (!record?.jobId || !['done', 'processing'].includes(record.status)) return;
  const edits = record.capEdited && record.caps ? record.caps : null;
  const making = record.status === 'processing';
  // saved while a cut was being made: the edits are timed on the cut before it
  carried = making && edits && record.capsFrom ? { list: edits, ranges: record.capsFrom } : null;
  return job.reopen({
    id: record.jobId,
    captions: making ? null : edits,
    target: () => ({ sig: record.pipe || clips.signature() }),
  });
}

async function openProject() {
  let stored = [];
  try {
    stored = await project.files();
  } catch (error) {
    console.warn(error);
  }
  const record = project.record();
  if (record?.lang) st.lang = record.lang;
  if (record?.style) restore(record.style);
  clips.reset(fromRecord(stored, record));
  settledLook = lookNow();
  comments.restore(record?.comments);
  renderPanels();
  cuts.render();
  refreshControls();
  if (!clips.count()) return status.empty();
  $('title').value = (record?.name || clips.all()[0].name).replace(/\.[^.]+$/, '') || 'Untitled';
  status.ready();
  showUpload();
  preview.update();
  clips.all().forEach(measure);
  await reopen();
}

// The clip being watched: the one to turn, and whose pending turn is previewed.
function watchedClip() {
  if (!job.hasResult()) return clips.current();
  const span = clipAt(job.edit().timeline, player.video.currentTime || 0);
  return clips.byId(span?.key) ?? clips.current();
}

function chooseCaptions(choice) {
  const clip = watchedClip();
  if (clip) clips.setCaptions(clip.id, choice);
}

function registerPageActions() {
  register({
    logout: () => logout(),
    goHome: () => {
      project.flush();
      location.href = '/home.html';
    },
    toggleMenu: el => { $(el.dataset.value).hidden = !$(el.dataset.value).hidden; },
    reopen,
    backToEdit: () => {
      status.clear();
      captionEditor.render();
      captions.refresh(true);
    },
    renameProject: el => {
      const name = el.value.trim() || 'Untitled';
      el.value = name;
      project.update({ name });
    },
    undo: () => undo(),
    redo: () => redo(),
    rotateRight: () => clipPanel.turnAll(90),
    rotateLeft: () => clipPanel.turnAll(270),
    burnShow: () => chooseCaptions('on'),
    burnKeep: () => chooseCaptions('off'),
  });
  document.addEventListener('click', event => {
    for (const menu of document.querySelectorAll('.menu')) {
      const inside = event.target.closest('[data-menu]') === menu.parentElement;
      if (!inside || event.target.closest('.menu')) menu.hidden = true;
    }
  });
}

function undo() {
  if (turnTimer) { // undoing the turn that hasn't been applied yet
    clearTimeout(turnTimer);
    turnTimer = null;
    history.settle(true);
  }
  if (!job.busy() && history.canUndo()) goTo(history.undo(projectState()));
}

function redo() {
  if (!job.busy() && history.canRedo()) goTo(history.redo(projectState()));
}

function followVideo() {
  const { video } = player;
  video.addEventListener('loadedmetadata', () => {
    media.duration = video.duration;
    player.updateTime();
    timeline.build();
    placeOverlays();
  });
  video.addEventListener('loadeddata', placeOverlays);
  video.addEventListener('timeupdate', () => {
    if (clips.count() > 1 && job.hasResult()) player.fit();
    burnNote();
  });
  new ResizeObserver(placeOverlays).observe($('stage'));
  new ResizeObserver(() => {
    timeline.drawWave();
    timeline.drawPlayhead();
  }).observe($('tlw'));
}

async function start() {
  for (const view of [layout, inspector, player, framing, position, stylePanel, clipPanel, mediaPanel, pacing, report,
    templates, captionEditor, comments, timeline, cuts, actions, downloads]) {
    view.init();
  }
  registerPageActions();
  captionEditor.shortcuts({ undo, redo });
  followVideo();
  position.follow(captionLayer, () => {
    if (!job.hasResult()) return null;
    const at = player.video.currentTime || 0;
    return placed.find(box => at >= box.s && at <= box.e) ?? null;
  });
  listen();
  project.autosave(snapshot);
  inspector.provide('text', stylePanel.sections);
  inspector.provide('clip', clipPanel.sections);
  inspector.provide('cut', cuts.sections);
  $('cmtHelp').textContent = COMMENT_HELP('edited');
  renderPanels();
  templates.render();

  const user = await requireUser();
  $('av').textContent = user.initials;
  $('av').style.background = user.avatar_color || '#4b8';
  $('uname').textContent = user.name;
  layout.load(user.id);
  project.open(user.id, new URLSearchParams(location.search).get('p'));
  fonts.load().then(families => families.length && stylePanel.setFonts(families));
  templates.loadCustom();
  await openProject();
}

start();
