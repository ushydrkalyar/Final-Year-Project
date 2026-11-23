// Live captions: draws an ASS caption track over a <video> with libass (WebAssembly, JASSUB),
// the same renderer and the same ASS file the export burns, so the preview matches the export.
// Needs /assets/vendor/jassub-1.8.6/jassub.umd.js loaded first.
const LiveCaptions = (() => {
  const LIB = '/assets/vendor/jassub-1.8.6/';
  let catalog = null; // family (lower case) -> [font file URLs], shared by every instance

  const fontCatalog = () => (catalog ??= fetch('/font-catalog')
    .then(r => { if (!r.ok) throw new Error('font catalog ' + r.status); return r.json(); })
    .catch(() => { catalog = null; return null; })); // not cached: the next show() tries again

  // Font families a track uses: each Style's Fontname plus any \fn override.
  function families(ass) {
    const out = new Set();
    for (const m of ass.matchAll(/^Style:\s*[^,]*,([^,]+)/gm)) out.add(m[1].trim().toLowerCase());
    for (const m of ass.matchAll(/\\fn([^\\}]+)/g)) out.add(m[1].trim().toLowerCase());
    return out;
  }

  // JASSUB 1.8.6 divides by the video's size in resize(); while a source is being swapped that
  // is 0, the worker gets NaN and throws. The video's own "resize" event redraws once it is known.
  const safeRenderer = painted => class extends JASSUB {
    _render(frame) {
      super._render(frame);
      painted();
    }
    resize(...args) {
      if (this._video && this._video.videoWidth === 0) return;
      return super.resize(...args);
    }
  };

  // onPaint(fresh): captions were drawn. fresh = it is the first paint of the track last shown.
  function create(video, { onPaint = () => {} } = {}) {
    let renderer = null, shown = 0, unpainted = false;
    const loaded = new Set();

    async function newFonts(ass) {
      const cat = await fontCatalog();
      if (!cat) return { fams: [], urls: [] };
      const fams = [...families(ass)].filter(fam => !loaded.has(fam));
      return { fams, urls: fams.flatMap(fam => cat[fam] || []) };
    }

    // Show this track; a newer call made while fonts load wins.
    async function show(ass) {
      const ticket = ++shown;
      const { fams, urls } = await newFonts(ass);
      if (ticket !== shown) return;
      fams.forEach(fam => loaded.add(fam)); // only once they are really handed to the renderer
      if (!renderer) {
        renderer = new (safeRenderer(() => {
          const fresh = unpainted;
          unpainted = false;
          onPaint(fresh);
        }))({
          video, subContent: ass, fonts: urls,
          offscreenRender: false, // Firefox: JASSUB resizes the canvas after handing it to the worker, and throws
          workerUrl: LIB + 'jassub-worker.js', wasmUrl: LIB + 'jassub-worker.wasm',
          modernWasmUrl: LIB + 'jassub-worker-modern.wasm',
          availableFonts: { 'liberation sans': LIB + 'default.woff2' },
        });
        return;
      }
      urls.forEach(u => renderer.addFont(u));
      renderer.setTrack(ass);
      // the worker answers in order: whatever it paints after this answer is the new track
      renderer.getEvents?.(() => {
        if (ticket !== shown) return;
        unpainted = true;
        repaint();
      });
      repaint();
    }

    // JASSUB paints on video frames, so a paused video would keep showing the old track. A forced
    // resize renders the current frame again; Safari ignores the tiny seek that used to be the nudge.
    function repaint() {
      if (!video.paused || video.readyState < 2 || !renderer) return;
      renderer.resize(0, 0, 0, 0, true);
    }

    video.addEventListener('loadeddata', () => repaint()); // first frame of a new source: draw the track on it

    function hide() { shown++; renderer?.freeTrack(); }
    const canvas = () => renderer?._canvas ?? null;
    return { show, hide, canvas };
  }

  return { create };
})();
