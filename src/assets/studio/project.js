// The open project's record in this browser (see /assets/projects.js), saved as you work.

const SAVE_DELAY_MS = 300;

let store = null;
let id = null;
let timer = null;
let snapshot = () => ({});

export function open(userId, requested) {
  store = CutSmartProjects.forUser(userId);
  id = requested || store.list()[0]?.id || null;
}

export const record = () => (id ? store?.get(id) : null);
export const all = () => store?.list() ?? [];
export const thumb = projectId => store?.thumb(projectId) || '';
export const presets = () => store?.presets.list() ?? [];
export const savePresets = list => store?.presets.save(list);
export const files = async () => (id ? store.files(id) : []);
export const storeClips = list => store.setFiles(id, list);

export function update(patch) {
  if (id) store?.update(id, patch);
}

// `read` returns what autosave writes.
export function autosave(read) {
  snapshot = read;
}

// Debounced: a slider would otherwise rewrite the project list on every tick.
export function save() {
  if (!id) return;
  clearTimeout(timer);
  timer = setTimeout(flush, SAVE_DELAY_MS);
}

export function flush() {
  if (timer === null) return;
  cancel();
  update(snapshot());
}

export function cancel() {
  clearTimeout(timer);
  timer = null;
}

window.addEventListener('pagehide', flush);
