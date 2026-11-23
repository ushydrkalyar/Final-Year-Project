# CutSmart Studio: review of the audit-fix session, and the plan to make Studio a usable editor

Date: 2026-11-22. Written for an engineering session with no prior context.
No application code was changed to produce this document.

## Read this first

- The app is a FastAPI server in `src_v4/` (run from `src_v4` with `../.venv/bin/uvicorn app:app --port 8000`). The UI in use is `studio.html` plus ES modules in `assets/studio/`. `editor.html` is the legacy "advanced editor".
- `DATABASE_URL` in `../.env` is a REMOTE Supabase database. Never test against it. The owner runs their own server on port 8000 from the live tree. Test on a copy of the tree, a local Postgres database, a separate `CUTSMART_WORK_DIR` and another port, and stop only processes you started, by PID. Never bulk-delete `../work`.
- The whole working tree is uncommitted (`git status` in `src_v4`). Commit a baseline before changing anything. No AI attribution in commits.
- Owner decisions already made, do not re-ask: Studio is the only editor (retire `editor.html`); layout follows CapCut (large preview on top, timeline with frames below); clips can be added inside Studio; caption style changes apply instantly with no Apply button and no regeneration; "Library" goes to Home; controls are grouped behind dropdowns and step flows so the screen is not cluttered; the product must satisfy `../AIBasedVideoEditingSoftware.pdf` (the FYP proposal).
- Do not edit `locked_templates/` or the `CUTSMART_LOCKED_TEMPLATES` literal in `studio.html`; a lock check compares that text.

## Part A. What the audit-fix session did

Source: `../.audit-baseline/AUDIT_FIXES.md`, the diff against `../.audit-baseline/src_v4-pre-audit-20261122-131356.tar.gz`, and a run of the current tree in Chrome.

1. It fixed what the audit listed: 55 findings about security, correctness, dead code and structure. `app.py` went from 2,526 to 1,166 lines (`pipeline.py`, `jobs.py`, `db.py`, `templates.py` extracted), `studio.html` from a 94 KB single script to 335 lines of markup plus 21 modules, and a test suite was added (`tests/`, 10 files).
2. It did not change how the product looks or flows, and was not asked to. The audit brief was "keep behaviour unless the finding is about wrong behaviour", so the three-column layout, panel contents and upload flow are the same as before. That is why "not much changed in usage" is expected. The UI problems below were out of the audit's scope, not regressions.
3. Its claims that matter for this plan hold up in a real run (isolated copy, local Postgres, Chrome, `test_video.mp4`):
   - one `POST /edit-video/`, then colour, line spacing and two template switches produced four `POST /captions/` calls and no further `/edit-video/`;
   - exactly one caption layer was on screen at every step (one JASSUB canvas, `#ovimg` and `#ovl` hidden);
   - the timeline has a filmstrip and a waveform.
4. Still open from its own list (`AUDIT_FIXES.md`, "Known and left open"): no guess limit on `/auth/verify-password`; Google-only accounts cannot set a password in Profile; crossfade length is over-reported; the remote-database latency of about 200 ms per authenticated request.
5. It kept `editor.html` (its default for open question Q2). The owner has now decided to retire it.

## Part B. Findings

Severity: Critical / High / Medium / Low. Confidence: High / Medium / Low.

### S-01. The owner's browser is running an old Studio page, not the current one
- **Severity:** Critical (it hides all current behaviour). **Confidence:** High that the page is stale; Medium on why the browser kept it.
- **Evidence:**
  - The owner's server log contains `POST /recaption/<job> 404`, followed by a fresh `POST /edit-video/`. No file in the current tree or in the pre-audit archive calls `/recaption/`. Only the committed version does (`git show HEAD:studio.html`), which also contains the strings "Apply style", "Generate captions" and "Advanced editor". None of those exist in the current `studio.html` or `assets/studio/`.
  - The same log has no `GET /studio.html`, no `GET /home.html` and no `GET /assets/...` lines at all, only API calls. A run of the current tree logs 1 `GET /studio`, 1 `GET /home` and 45 `GET /assets`.
  - `curl http://127.0.0.1:8000/studio.html` on the owner's running server returns the current page.
- **Why it matters:** this explains both reported symptoms exactly. The old page plays `/download/` (captions burned in) and draws a `/caption-preview` image on top, so the old and new styles overlap. Its "Apply style" button calls `/recaption/`, which no longer exists, so it falls back to a full re-edit.
- **Likely cause:** the committed server sent pages with no `Cache-Control` (`git show HEAD:app.py`, `FileResponse("studio.html")`), so the browser may reuse its copy without asking. The current server sends `no-cache`, but a browser that never asks never sees that header.
- **Fix:**
  1. Owner, now: open `http://localhost:8000/studio.html`, DevTools, Application, Storage, "Clear site data" with "Unregister service workers" and cache ticked but "IndexedDB" and "Local storage" UNTICKED (projects live there), then hard reload (Cmd+Shift+R). Do the same on `home.html`. The current page has no "Apply style" button and its footer reads "Every change previews instantly."
  2. Code: serve the app shell from new paths so no old copy can match (for example `/app` for Studio and `/library` for Home), redirect the old `.html` paths with `302` and `Cache-Control: no-store`, and add a `GET /recaption/...`-style tombstone that returns `410` with a "reload the page" message for the removed endpoints.
  3. Add `<meta name="cutsmart-build">` and have `main.js` compare it with `/version` on load, showing a "reload to update" banner on mismatch.
- **Verify before fixing:** ask the owner whether the page they test shows an "Apply style" button. If it does, S-01 is confirmed without further work.
- **Verify after fixing:** request the old URL with a stale cached copy (serve the `HEAD` version once without headers, then switch servers) and confirm the browser lands on the new shell.

### S-02. The preview is about 6% of the screen
- **Severity:** High. **Confidence:** High (measured).
- **Location:** `studio.html:L35-L36` (`.work{grid-template-columns:minmax(0,1.5fr) minmax(300px,.82fr) 360px}`, `.left{grid-template-rows:minmax(0,1fr) 250px}`).
- **Evidence:** at 1366x768 the stage is 336x614 px and a 16:9 video inside it is 334x188 px. At 1680x1000 it is 445x250 px. The caption list gets the largest area on screen; the timeline is 614 px wide and shares a column with it.
- **Fix:** the layout in Part C.

### S-03. Clips cannot be added, removed or reordered in Studio
- **Severity:** High. **Confidence:** High.
- **Location:** `studio.html:L275` (the only file input is "Replace", single file); `home.html:L137`, `L208-L219` (multi-select exists only at upload time on Home); `assets/studio/timeline.js:L49-L55` (one "Video 1" lane, no clip boundaries); `assets/studio/cuts.js:L45-L49` and `app.py:L567-L569` (restore and remove are refused for multi-clip jobs).
- **Why it matters:** the owner requires multi-video editing in Studio. Today a project's clip set is fixed at upload, and a multi-clip project loses the correction feature, which is a core proposal item.
- **Fix:** Part C, "Media" and "Timeline", and batch 3.

### S-04. The legacy editor is still routed
- **Severity:** Medium. **Confidence:** High.
- **Location:** `app.py` `PAGES` (`"/editor"`, `"/editor.html"`), `editor.html` (4,645 lines).
- **Why it matters:** it has what Studio lacks (large preview, full-width timeline, a Cut/Compress track, a stats strip), which is why the owner keeps opening it, and it bypasses Studio's live captions.
- **Fix:** port those four things into Studio (Part C), then delete `editor.html` and its routes. `/download`, `/srt` and `/combine-clips/` lose their only caller; delete them with it unless Studio's export needs them (it does not: it uses `/export/`, `/export-result/`). Blast radius: `tests/test_frontend.py` and any test that loads `/editor`.

### S-05. The timeline does not show the AI's decisions
- **Severity:** High for the FYP, because pause classification is the project's contribution. **Confidence:** High.
- **Location:** `assets/studio/timeline.js` draws captions, filmstrip and waveform only. `GET /timeline/{id}` already returns `original_timeline` with `type`, `reason` and `colour_map` (`app.py:L650-L740`); only the Cuts list uses it.
- **Fix:** a "Cuts" lane on the timeline showing kept, cut, compressed and filler ranges in their colours, with the reason on hover and Restore / Remove on click. A toggle switches the timeline between "Edited" and "Original" time.

### S-06. The comment system covers captions only
- **Severity:** Medium. **Confidence:** Medium (read `comment_parser.py` docstring and `/caption-command`; did not exercise every phrase).
- **Evidence:** proposal Step 6 says "Web interface with comment system. Users can specify timestamps for corrections." The comment box (`studio.html:L225-L229`) edits one caption. Cut corrections exist only as Mark in / Mark out buttons in the Cuts panel.
- **Fix:** let the comment box accept cut requests with timestamps ("keep the pause at 0:12", "cut 0:05 to 0:08", "remove the silence at the start") and route them to `/feedback/`. Keep it rule-based like the caption parser. Show each applied comment in a small history list with Undo.

### S-07. A correction re-renders the whole video
- **Severity:** Medium. **Confidence:** High.
- **Location:** `app.py:L546-L594`, `pipeline.run_correction`. The docstring says the video "is re-rendered from the original upload".
- **Evidence:** proposal Step 6: "System reprocesses only selected segments."
- **Fix:** cache each kept segment's encoded clip by (source file, start, end, frame, grade) and re-encode only segments whose range changed, then stream-copy join (the join is already stream-copy after C-05). Transcription and analysis are already reused. If this is judged too risky, the report must say so explicitly; do not leave the proposal claim unaddressed.

### S-08. Keyword-pop templates render tiny on a landscape clip
- **Severity:** Medium. **Confidence:** Medium (seen once; cause not traced).
- **Evidence:** with `test_video.mp4` (landscape) and template "Lime Pop", the caption "ads was" drew about 8 px tall in a 445 px wide preview, far smaller than the template card suggests.
- **Verify before fixing:** render the same template on a 9:16 clip and on a 16:9 clip and compare text height as a fraction of frame height. Check how `subtitle_size` (`assets/studio/style.js:L88-L92`) relates to `PlayResY` in `subtitle_service.py`.

### S-09. The footer overflows before the first edit
- **Severity:** Low. **Confidence:** High.
- **Location:** `studio.html:L310-L313`, `.rfoot`.
- **Evidence:** at 1680x1000 in the draft state the hint text wraps one word per line and the Export button extends past the panel's right edge.
- **Fix:** removed by Part C (actions move to the top bar).

### S-10. "Library" opens a side list instead of Home
- **Severity:** Low. **Confidence:** High.
- **Location:** `studio.html:L216`, `assets/studio/library.js`.
- **Fix:** make it a link to Home and delete `library.js` and `#libPanel`.

### S-11. Rotation is a manual setting on a second tab
- **Severity:** Low. **Confidence:** Medium.
- **Evidence:** `test_video.mp4` shows sideways in both editors until "Rotate video" is found under Cutting.
- **Fix:** put a rotate button on the preview itself. Auto-detecting orientation is an open question (Q3).

## Part C. Target design for Studio

One screen, three rows. Reference: CapCut desktop.

```
+-----------------------------------------------------------------------+
| Home | project name            Undo Redo        [Ratio v] [ Export ]  |  48 px
+----------+--------------------------------------+---------------------+
| tool     |                                      | Inspector           |
| rail +   |            PREVIEW                   | (for the selected   |
| drawer   |     (largest area, centred,          |  clip or caption,   |
| 300 px   |      fits video at any ratio)        |  or the open tool)  |
|          |      transport bar under it          | 320 px              |
+----------+--------------------------------------+---------------------+
| Split  Delete  Restore   | Edited / Original |        zoom  - o +     |
| Captions  [..][..][..]                                                |  timeline:
| Video     [clip 1 frames][clip 2 frames]                 [+ Add clip] |  full width,
| Cuts      [kept][cut][compress][filler]                               |  resizable,
| Audio     waveform                                                    |  default 34%
+-----------------------------------------------------------------------+
```

- **Top bar:** Home, project name, Undo/Redo, aspect ratio dropdown (Original, 9:16, 16:9, 1:1; this replaces the Format section), Export (a dropdown: video, `.srt`).
- **Tool rail (left):** Media, Captions, Templates, Pacing, Reference, Comments. One drawer is open at a time and it can collapse to icons only, which gives the preview the width.
  - Media: the project's clips as thumbnails, "+ Add clips", drag to reorder, remove, replace.
  - Captions: the caption list with inline edit and search.
  - Templates: built-in, my presets, "from reference video".
  - Pacing: preset (Natural, Balanced, Aggressive), language, colour grade. A sticky "Re-edit" button appears only when one of these differs from the current edit.
  - Reference: link, "copy caption style" checkbox, Match.
  - Comments: the comment box, examples, and history (S-06).
- **Inspector (right):** context-sensitive, collapsed sections (accordion), one open at a time.
  - Caption or nothing selected: Text (font, size, colour), Layout (words per caption, lines, spacing), Emphasis, Position. Every control applies live (already true via `captions.refresh`).
  - Clip selected: rotate, framing, transition into the next clip.
  - Cut range selected: why it was cut, Restore or Remove.
- **Preview:** transport directly under the video (play, time, mute, fullscreen, rotate). Position can also be set by dragging the caption on the preview.
- **Timeline:** full width, vertical splitter against the top row. Lanes: Captions, Video (one block per clip with its own filmstrip and a "+" at the end), Cuts (S-05), Audio. Zoom, snap to playhead, keyboard: Space, S, Delete, Cmd+Z, Cmd+Shift+Z.
- **First run flow (replaces "pick settings, then Generate"):** on opening a new project, a three-step strip over the preview: 1 Pacing preset, 2 Caption template, 3 Generate. After the first edit it disappears and never returns for that project.
- **Stats:** grade, time saved and percent shorter as one compact chip in the timeline toolbar, opening the full evaluation report in a popover.

State and module rules, so this does not become a second monolith: keep the existing split (models with no DOM: `job.js`, `captions.js`, `state.js`; views; `main.js` as the only wiring point). Add `clips.js` (model) and `layout.js` (drawer, inspector and splitter state, saved per user in localStorage).

## Part D. Remediation plan

Each batch ends with the app runnable and the tests green.

- **Batch 0. Baseline.** Commit the current tree. Owner clears the stale page (S-01 step 1) and confirms the current Studio behaves as Part A item 3 describes.
- **Batch 1. Stale-page protection (S-01).** New shell routes, redirects, tombstones for removed endpoints, build check. Blocks nothing, but do it first so every later change is actually seen.
- **Batch 2. Layout (S-02, S-09, S-10, S-11).** Rebuild `studio.html` markup and CSS to Part C with existing features only. No behaviour change in the models. Check 1280x720, 1366x768, 1680x1000 and 1920x1080: preview is at least 40% of the viewport area with drawer and inspector open.
- **Batch 3. Clips (S-03).** Backend first: a project-level job that owns ordered clip jobs (the master job in `pipeline.run_multi_clip` is the starting point), with add, remove and reorder re-running only the affected clip and the join. `/timeline` for a master job returns per-clip boundaries. Corrections on multi-clip jobs target one clip (`clip index` plus original timestamps), lifting the refusal at `app.py:L567`. Then the Media drawer and per-clip timeline blocks.
- **Batch 4. Decisions on the timeline and comments (S-05, S-06).**
- **Batch 5. Partial re-render (S-07).** Depends on batch 3's segment handling.
- **Batch 6. Retire the legacy editor (S-04).** Only after batches 2 and 4, so nothing it offers is lost. Delete `editor.html`, its routes and the endpoints only it used.
- **Batch 7. S-08**, after its "verify before fixing" step.

Coordination: batch 3 changes the shape of stored projects in the browser (`assets/projects.js`, IndexedDB). Migrate existing single-clip records in place and keep reading the old shape. No database migration is needed in any batch.

Leave alone: auth, the audit's security fixes, the caption rendering engine (`subtitle_service.py`), the locked templates.

## Part E. Proposal coverage

| Proposal item | State | Gap |
|---|---|---|
| Whisper transcription with timestamps | Done | |
| RMS silence detection, pause features | Done | |
| KEEP / CUT / COMPRESS engine | Done | Not visible on the Studio timeline (S-05) |
| Pacing presets Aggressive, Balanced, Natural | Done | On a second tab; moves to the Pacing drawer |
| Configurable thresholds | Presets only | Q2 |
| Reference video link, pacing match | Done | Real YouTube download was never tested end to end by the fix session |
| FFmpeg render, subtitles | Done | |
| Web front-end | Done | Layout (S-02), multi-clip (S-03) |
| Comment system with timestamps | Partial | S-06 |
| Reprocess only selected segments | Not done | S-07 |

## Part F. Tests to add

End to end in a real browser against an isolated server, since these are the behaviours that were in dispute:

1. After one edit, changing colour, size, spacing, position and template sends only `/captions/` requests and never `/edit-video/`; exactly one caption layer is visible after each change.
2. Preview area is at least 40% of the viewport at four window sizes; nothing overflows its panel in the draft, processing, done and error states.
3. Add a second clip in Studio, reorder, remove: the timeline shows the right blocks and the export matches the order.
4. Restore and remove work on a two-clip project.
5. A typed comment with a timestamp changes the cut; Undo reverts it.
6. A browser holding the committed `studio.html` in cache reaches the new shell.
7. A correction on a 3-minute clip re-encodes only the changed segments (count ffmpeg invocations).

## Part G. Open questions

- **Q1.** Is the page you test showing an "Apply style" button? This confirms S-01 in one look.
- **Q2.** Does "configurable thresholds" in the proposal need sliders (minimum pause to cut, compress length), or are the three presets enough for the evaluators?
- **Q3.** Should sideways footage be rotated automatically (face orientation check), or is a rotate button on the preview enough?
- **Q4.** Limit on clips per project. The server allows 10 today (`app.py:L120`).
- **Q5.** If partial re-render (S-07) proves unsafe for crossfade transitions, is "re-render only the affected clip" acceptable as the definition of "selected segments"?
