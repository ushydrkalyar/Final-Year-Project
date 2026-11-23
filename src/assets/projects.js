// Recent videos for ONE signed-in account.
// Metadata lives in localStorage, the video bytes in IndexedDB; both are keyed by
// user id, so two accounts on the same browser never see each other's videos.
// Used by home.html (list + upload) and studio.html (open, save, clips).
const CutSmartProjects = (() => {
  const DB_NAME = 'cutsmart', DB_VERSION = 2, STORE = 'projects', MAX = 30;
  const MAX_FILE_BYTES = 1024 ** 3; // the server refuses more

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        // v1 kept one shared 'pending' slot for every account: drop it
        if (db.objectStoreNames.contains('pending')) db.deleteObjectStore('pending');
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  let connection = null; // one per page
  function db() {
    connection ??= openDb().then(d => {
      const forget = () => { connection = null; };
      d.onversionchange = () => { d.close(); forget(); };
      d.onclose = forget;
      return d;
    }).catch(err => { connection = null; throw err; });
    return connection;
  }

  async function idb(mode, fn) {
    const d = await db();
    return new Promise((resolve, reject) => {
      const tx = d.transaction(STORE, mode);
      const out = fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(out && 'result' in out ? out.result : undefined);
      tx.onerror = () => reject(tx.error || new Error('Could not save in this browser'));
      tx.onabort = () => reject(tx.error || new Error('Storage is full'));
    });
  }

  function checkSizes(files) {
    if (files.some(f => f.size > MAX_FILE_BYTES)) throw new Error('Each video must be 1 GB or smaller.');
  }

  // `clips` is [{id, file}]: the stored order is the playing order.
  async function storeFiles(key, clips) {
    const put = items => idb('readwrite', s => { s.put(items, key); });
    try {
      await put(clips.map(({ id, file }) => ({ id, name: file.name, type: file.type, file })));
    } catch (err) {
      if (err?.name === 'QuotaExceededError') throw err; // else: private windows cannot store a File (Safari doesn't say why)
      await put(await Promise.all(clips.map(async ({ id, file: f }) =>
        ({ id, name: f.name, type: f.type, buf: await f.arrayBuffer() }))));
    }
  }

  function forUser(userId) {
    const listKey = `cs_projects:${userId}`, presetKey = `cs_presets:${userId}`;
    const fileKey = id => `${userId}:${id}`;
    const read = key => { try { return JSON.parse(localStorage.getItem(key) || '[]'); } catch (_) { return []; } };
    const write = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (_) { return false; } };
    try { localStorage.removeItem('cs_projects'); localStorage.removeItem('cs_presets'); } catch (_) {} // v1 shared keys

    const thumbKey = id => `cs_thumb:${userId}:${id}`;
    const thumb = id => { try { return localStorage.getItem(thumbKey(id)) || ''; } catch (_) { return ''; } };
    const setThumb = (id, image) => {
      try {
        if (image) localStorage.setItem(thumbKey(id), image);
        else localStorage.removeItem(thumbKey(id));
        return true;
      } catch (_) { return false; } // full: a missing picture is harmless
    };
    const list = () => read(listKey);

    // Older versions kept the picture inside the list record: move it out.
    (() => {
      const records = list();
      let moved = false;
      for (const p of records) {
        if ('thumb' in p && setThumb(p.id, p.thumb)) { delete p.thumb; moved = true; }
      }
      if (moved) write(listKey, records);
    })();
    const get = id => list().find(p => p.id === id) || null;

    function update(id, patch) {
      const { thumb: image, ...fields } = patch;
      const l = list(), p = l.find(x => x.id === id);
      if (!p) return p;
      if ('thumb' in patch) setThumb(id, image);
      if (Object.keys(fields).length) { Object.assign(p, fields); write(listKey, l); }
      return p;
    }

    async function remove(id) {
      write(listKey, list().filter(p => p.id !== id));
      setThumb(id, '');
      try { await idb('readwrite', s => { s.delete(fileKey(id)); }); } catch (_) {}
    }

    async function clear() {
      const ids = list().map(p => p.id);
      write(listKey, []);
      ids.forEach(id => setThumb(id, ''));
      try { await idb('readwrite', s => ids.forEach(id => s.delete(fileKey(id)))); } catch (_) {}
    }

    // Store the files and add a project at the top of the list. Returns its id.
    async function create(files, meta, onProgress = () => {}) {
      checkSizes(files);
      const id = 'p' + Date.now();
      onProgress(0.3);
      await storeFiles(fileKey(id), files.map((file, i) => ({ id: `c${i}`, file })));
      const { thumb: image, ...fields } = meta;
      const l = [{ id, ts: Date.now(), status: 'draft', ...fields }, ...list()];
      if (!write(listKey, l.slice(0, MAX))) {
        await idb('readwrite', s => { s.delete(fileKey(id)); });
        throw new Error('Browser storage is full. Remove some videos from the list and try again.');
      }
      setThumb(id, image);
      for (const old of l.slice(MAX)) {
        setThumb(old.id, '');
        try { await idb('readwrite', s => { s.delete(fileKey(old.id)); }); } catch (_) {}
      }
      onProgress(1);
      return id;
    }

    // The clips of an existing project, as Studio added, removed or reordered them.
    async function setFiles(id, clips) {
      checkSizes(clips.map(clip => clip.file));
      await storeFiles(fileKey(id), clips);
    }

    async function files(id) {
      const items = await idb('readonly', s => s.get(fileKey(id)));
      // `buf` is how older versions stored videos; they had no clip ids
      return (items || []).map((i, n) =>
        ({ id: i.id || `c${n}`, file: new File([i.file || i.buf], i.name, { type: i.type || 'video/mp4' }) }));
    }

    const presets = { list: () => read(presetKey), save: l => write(presetKey, l.slice(0, MAX)) };
    return { list, get, thumb, update, remove, clear, create, setFiles, files, presets };
  }

  // A thumbnail and the length of a video file, for the project list.
  function videoMeta(file) {
    return new Promise(resolve => {
      const v = document.createElement('video'), url = URL.createObjectURL(file);
      let timer;
      const done = (thumb, dur) => {
        clearTimeout(timer);
        v.onloadeddata = v.onseeked = v.onerror = null;
        v.removeAttribute('src'); v.load();
        URL.revokeObjectURL(url);
        resolve({ thumb, dur });
      };
      v.preload = 'auto'; v.muted = true;
      v.onloadeddata = () => { v.currentTime = Math.min(1, (v.duration || 1) / 3); };
      v.onseeked = () => {
        try {
          const c = document.createElement('canvas');
          const k = Math.min(320 / v.videoWidth, 200 / v.videoHeight); // keep its shape
          c.width = Math.round(v.videoWidth * k); c.height = Math.round(v.videoHeight * k);
          c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
          done(c.toDataURL('image/jpeg', 0.7), v.duration);
        } catch (_) { done('', v.duration); }
      };
      v.onerror = () => done('', 0);
      timer = setTimeout(() => done('', v.duration || 0), 4000);
      v.src = url;
    });
  }

  return { forUser, checkSizes, videoMeta };
})();
