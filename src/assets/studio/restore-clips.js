// A saved project's clips, with the settings saved for each.
import * as clips from './clips.js';

export function fromRecord(stored, record) {
  const style = record?.style ?? {};
  const settings = new Map((record?.clips ?? []).map(clip => [clip.id, clip]));
  // projects saved before clips had their own settings kept one of each in the style
  const older = {
    rotate: style.rotate,
    frame: style.frame?.mode === 'custom'
      ? { mode: 'custom', cx: +style.frame.cx || 0.5, cy: +style.frame.cy || 0.5, zoom: +style.frame.zoom || 1 } : undefined,
    trans: style.trans,
  };
  return stored.map(({ id, file }) => {
    const saved = settings.get(id);
    return clips.make({ id, file, name: saved?.name, saved: saved ?? older });
  });
}
