# CutSmart code audit

Audit date: 2026-11-22. Read-only audit, no code was changed.
Scope: the git repository at `src_v4/` (all paths below are relative to it) plus the two files it reads from its parent directory (`../.env`, `../requirements.txt`).
Tools run: `pyflakes` (45 warnings), `vulture --min-confidence 60`, `rg` reference checks, manual review. There is no TypeScript, no `package.json`, no ESLint config and no CI config, so `tsc`, `knip`, `ts-prune` and `depcheck` do not apply.

## 1. Executive summary

### Codebase overview
- **Stack:** Python 3.11, FastAPI + uvicorn, Postgres (psycopg2) for accounts, JWT cookie sessions, Google OAuth (authlib). Media work is done by shelling out to ffmpeg/ffprobe, plus Whisper, yt-dlp, OpenCV, PySceneDetect, Tesseract, librosa.
- **Size:** about 7,700 lines of Python in 27 modules, about 12,300 lines of HTML with inline JS in 11 pages, 4 small shared JS modules in `assets/`.
- **Entry point:** `app.py` (2,526 lines): every route, the in-memory `job_store`, and the three background pipelines. Run with `uvicorn app:app` from `src_v4`; the process then `chdir`s into `../work` (`workspace.py`).
- **Main modules:** `auth.py` (accounts), `edit_render.py` + `render_engine.py` + `subtitle_service.py` (render and captions), `cut_list.py` + `decision_engine.py` + `feature_extractor.py` + `filler_remover.py` (edit decisions), `style_extractor.py` + `scene_detector.py` + `reference_caption_style.py` (reference video analysis), `clip_combiner.py` (multi-clip).
- **UI:** `studio.html` is the current editor, `home.html` the dashboard, `editor.html` a legacy editor that is still routed, `index.html` an unrouted older copy.

### Finding counts

| Category | Critical | High | Medium | Low | Total |
|---|---|---|---|---|---|
| Logic (backend `L-01` to `L-22`) | 1 | 6 | 11 | 4 | 22 |
| Logic (front-end `FE-01` to `FE-16`) | 1 | 2 | 8 | 5 | 16 |
| Dead code (`D-01` to `D-06`, `FE-17`, `FE-18`) | 0 | 0 | 2 | 6 | 8 |
| Complexity (`C-01` to `C-07`, `FE-19`, `FE-20`) | 0 | 0 | 5 | 4 | 9 |
| **Total** | **2** | **8** | **26** | **19** | **55** |

### Top 5 highest-impact issues

1. **L-01** - Signing up with someone else's email before they do leaves the attacker's password on the account after the real owner verifies it or links Google.
2. **FE-01 + L-02** - `/login.html?next=javascript:...` runs script for a logged-in user, and `PUT /auth/me` changes the password without the current one: one link takes over an account.
3. **L-03** - Password-reset emails take their link host from the request's `Host` header, so an attacker can make the victim's reset link point at the attacker's server.
4. **L-07** - Filler removal deletes every standalone "like", "so", "right", "well" (and Urdu/Hindi "aur", "lekin", "agar") from the user's speech in every edit.
5. **L-05 + L-06** - One SQL error leaks a pooled connection (10 leaks stop all authenticated requests), and a rotated upload transcodes on the event loop, freezing the server for every user until it finishes.

### Read this first: the working tree is not committed
`git status` shows 17 modified tracked files, 1 tracked file deleted (`feedback_service.py`) and several core modules that have never been committed (`edit_render.py`, `cut_list.py`, `workspace.py`, `media_probe.py`, `font_catalog.py`, `caption_preview.py`, `reframe.py`, `requirements.txt`, `assets/`, `images/`, `models/`). The only two commits predate the current architecture. The fix session has no rollback point until this is committed. See Batch 0.

---

## 2. Findings

IDs: `L-` logic, `D-` dead code, `C-` complexity, `FE-` front-end (any category).

### 2.1 Logic defects - backend

#### [L-01] Account pre-hijack through an unverified signup
- **Category:** Logic
- **Severity:** Critical
- **Confidence:** High
- **Location:** `auth.py:L413-L424` (re-signup), `auth.py:L678-L687` (Google link), `auth.py:L670-L673` (no `email_verified` check)
- **Evidence:** When `/auth/signup` is called for an email that exists but is unverified, the new name and password are thrown away and only a new OTP is sent:
  ```python
  if existing:
      if existing["email_verified"]: ... raise 409
      conn.close()
      otp = _generate_otp(); _store_otp(email, otp)
      email_service.send_otp_email(email, otp)
      return {"message": "Verification code resent. ..."}
  ```
  The Google callback links to any existing row by email and sets `email_verified = TRUE` without touching `password_hash`.
- **Why it's a problem:** An attacker signs up with the victim's email and a password the attacker knows, and never verifies. Later the victim signs up (their password is silently discarded) and enters the OTP, or signs in with Google. The account is now verified and still carries the attacker's password, so the attacker can log in with email + password and see the victim's jobs and videos. A weaker variant hits honest users too: anyone who mistyped their password on the first attempt cannot fix it by signing up again.
- **Proposed fix:**
  1. In `signup`, in the `existing and not email_verified` branch, run `UPDATE users SET name = ?, password_hash = ? WHERE id = ?` with the new request's values before sending the OTP.
  2. In `google_callback`, when linking to an existing row whose `email_verified` is false, also set `password_hash = NULL` in the same `UPDATE`.
  3. In `google_callback`, reject the login (redirect to `/login.html?google_error=failed`) unless `user_info.get("email_verified")` is true, and use `user_info.get("email")` with the same redirect when it is missing (today a missing key raises `KeyError`, a 500).
- **Blast radius:** `signup`, `google_callback`. Behaviour change: re-signing up before verification now replaces the pending name/password (intended).
- **Verify before fixing:** Confirm no other code path creates unverified rows (`rg "INSERT INTO users" auth.py` should show exactly 2 hits).
- **Verify after fixing:** With `DEV_MODE=1`: sign up `a@x.com` / `Passw0rd1`, do not verify, sign up again with `Passw0rd2`, verify with the printed OTP, log out, confirm `Passw0rd1` is rejected and `Passw0rd2` works. Add this as an automated test (see section 4).
- **Depends on / blocks:** Easier after L-05 (connection helper).

#### [L-02] Password can be changed without the current password
- **Category:** Logic
- **Severity:** High
- **Confidence:** High
- **Location:** `auth.py:L546-L562` (`PUT /auth/me`), `auth.py:L573-L587` (`POST /auth/verify-password`), `auth.py:L378-L380`
- **Evidence:** `update_me` writes a new `password_hash` for any authenticated request that includes `password`. The "current password" check is a separate endpoint that the page calls first, so it is enforced only by the browser. The length rule here (8 chars) is also weaker than signup's (letters and digits).
- **Why it's a problem:** Anyone holding a session (shared computer, stolen cookie) can set a new password with one request and lock the owner out. The verify step can be skipped by calling the API directly.
- **Proposed fix:**
  1. Add `current_password: str | None = None` to `ProfileUpdateRequest`.
  2. In `update_me`, when `body.password` is set: load `password_hash`; if it is non-null, require `body.current_password` and `_verify_password(...)`, else 401 `wrong_password`. Google-only accounts (null hash) may set a first password without it.
  3. Apply the same strength rule as signup by moving the regex check into one module-level function `_check_password_strength(v)` used by `SignupRequest`, `ResetPasswordRequest` and `ProfileUpdateRequest`.
  4. Update the profile page to send `current_password` in the `PUT /auth/me` body (see FE findings for the call site). `/auth/verify-password` can stay for inline UI feedback.
- **Blast radius:** `profile.html` change-password flow. API behaviour change: `PUT /auth/me` with `password` now needs `current_password`.
- **Verify before fixing:** `rg "auth/me" *.html` to find every `PUT` caller and what body it sends.
- **Verify after fixing:** `curl -X PUT /auth/me -d '{"password":"Newpass123"}'` with a valid cookie returns 401; the profile page flow still works end to end.
- **Depends on / blocks:** none

#### [L-03] Password-reset link is built from the request's Host header
- **Category:** Logic
- **Severity:** High
- **Confidence:** High
- **Location:** `auth.py:L615-L616`, `email_service.py:L108`, `email_service.py:L115`
- **Evidence:** `base_url = str(request.base_url).rstrip("/")` is passed to `send_reset_email`, which emails `{base_url}/reset_password.html?token=...`. `request.base_url` comes from the client-supplied `Host` header and uvicorn does not validate it.
- **Why it's a problem:** An attacker posts `/auth/forgot-password` for the victim's email with `Host: evil.example`. The victim receives a genuine CutSmart email whose button points at the attacker's site with a valid token in the URL. One click hands over the account.
- **Proposed fix:** Read a new env var `APP_BASE_URL` (default `http://localhost:8000`) once at import in `auth.py` and pass that instead of `request.base_url`. Drop the now-unused `request` parameter from `forgot_password`. Document the variable next to `DATABASE_URL` in `../.env`.
- **Blast radius:** Reset emails only. Deployments must set `APP_BASE_URL`.
- **Verify before fixing:** `rg "base_url" *.py` (expected: only the hits listed above).
- **Verify after fixing:** With `DEV_MODE=1`, `curl -H 'Host: evil.example' -X POST localhost:8000/auth/forgot-password -d '{"email":"<verified user>"}' -H 'content-type: application/json'` and confirm the link printed in the server log uses `APP_BASE_URL`.
- **Depends on / blocks:** none

#### [L-04] Signing secrets fall back to hardcoded public strings
- **Category:** Logic
- **Severity:** High
- **Confidence:** High
- **Location:** `auth.py:L68`, `app.py:L104-L107`
- **Evidence:** `JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-secret-change-before-deploying")` and `secret_key=os.environ.get("JWT_SECRET", "dev-session-secret")`.
- **Why it's a problem:** If the env var is missing (new machine, wrong working directory so `../.env` is not found, typo), the server starts normally and signs sessions with a string that is in the source code. Anyone can then forge a session cookie for any user id. `../.env` does define `JWT_SECRET` today, so this is latent, but it fails open and silently.
- **Proposed fix:** In `auth.py` raise `RuntimeError("JWT_SECRET is not set")` when the variable is missing or empty, exactly like the existing `DATABASE_URL` check at `auth.py:L48-L52`. In `app.py` use `auth.JWT_SECRET` for `SessionMiddleware` instead of reading the env a second time with a different default.
- **Blast radius:** Server refuses to start without `JWT_SECRET`. No runtime behaviour change when it is set.
- **Verify before fixing:** Confirm `JWT_SECRET` is present in every environment that runs the app.
- **Verify after fixing:** Start with `JWT_SECRET` unset and expect a clear startup error; start normally and confirm an existing session cookie still works.
- **Depends on / blocks:** none

#### [L-05] DB pool is not thread-safe and leaks a connection on every SQL error
- **Category:** Logic
- **Severity:** High
- **Confidence:** High (leak), Medium (thread-safety impact under real load)
- **Location:** `auth.py:L55` (pool), `auth.py:L99-L118` (`_PooledConn`), every call site of `_get_db()` (20 of them, `auth.py:L123-L704`)
- **Evidence:** The pool is `psycopg2.pool.SimpleConnectionPool(1, 10, ...)`, which psycopg2 documents as not safe to share between threads. All sync routes and the `get_current_user` dependency run in FastAPI's threadpool. Every call site is `conn = _get_db(); ...; conn.close()` with no `try/finally`, so any exception between the two (for example the unique-constraint error when two signups for one email race at `auth.py:L427-L439`) never returns the connection.
- **Why it's a problem:** After 10 leaked connections `getconn()` raises `PoolError` for every request. Because `get_current_user` guards every job endpoint, the whole app is down until restart. Concurrent `getconn`/`putconn` on a non-thread-safe pool can also hand the same connection to two requests.
- **Proposed fix:**
  1. Switch to `psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)`.
  2. Replace `_PooledConn`/`_get_db` with one context manager and convert every call site to `with _db() as cur:`:
     ```python
     @contextmanager
     def _db():
         conn = _pool.getconn()
         try:
             with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                 yield cur
             conn.commit()
         except Exception:
             conn.rollback()
             raise
         finally:
             _pool.putconn(conn)
     ```
  3. Write SQL with `%s` placeholders directly and delete the `sql.replace("?", "%s")` shim (`auth.py:L106`), which would corrupt any query containing a literal `?`.
  4. In `signup`, catch `psycopg2.errors.UniqueViolation` on the insert and return the same 409 as the pre-check.
- **Blast radius:** Every function in `auth.py`. Pure refactor apart from step 4.
- **Verify before fixing:** `rg -n "_get_db\(\)|conn2?\.(execute|commit|close)" auth.py` to enumerate call sites; note `init_db` uses `conn._conn.rollback()` (`auth.py:L148`).
- **Verify after fixing:** Auth test suite (section 4). Fire 30 parallel signups for one email and confirm one 200, the rest 409, and that `/auth/me` still answers afterwards.
- **Depends on / blocks:** Blocks C-06. Do before L-01, L-02, L-13, L-14 to avoid editing the same lines twice.

#### [L-06] Blocking ffmpeg/ffprobe/shell calls run on the event loop
- **Category:** Logic
- **Severity:** High
- **Confidence:** High
- **Location:** `app.py:L1169-L1171` and `app.py:L1217-L1227` (`_reject_unusable`, `_upright` inside `async def submit_video`), `app.py:L186-L198` (`_upright` runs a full libx264 transcode via `edit_render.rotate`, `edit_render.py:L190-L202`), `app.py:L1142-L1153` (sync file writes), `app.py:L1343-L1349` (`/version` runs `yt-dlp --version` through a shell)
- **Evidence:** `submit_video` is `async def`, so these synchronous subprocess calls execute on the single event-loop thread:
  ```python
  written = await _save_upload(vf, input_video_path)
  _reject_unusable([(input_video_path, safe_name)])   # ffprobe, up to 30 s
  input_video_path = _upright(input_video_path, rotate)  # full re-encode
  ```
- **Why it's a problem:** While one user's rotated upload is being transcoded (minutes for a large file), the server answers nobody: every other user's `/status` poll, page load and login hangs. Front-end polls can time out and show errors for healthy jobs.
- **Proposed fix:**
  1. In `submit_video` wrap both calls: `await run_in_threadpool(_reject_unusable, [...])` and `input_video_path = await run_in_threadpool(_upright, input_video_path, rotate)` (`from starlette.concurrency import run_in_threadpool`). Do the same in the multi-clip loop. `HTTPException` raised inside propagates unchanged.
  2. In `_save_upload` replace `buf.write(chunk)` with `await run_in_threadpool(buf.write, chunk)`; same for `extract_reference_style` (`app.py:L2048-L2056`).
  3. `/version`: compute the yt-dlp version once at import (it is already computed at `app.py:L57-L63` as `_ytdlp_ver`) and return the stored value. See also L-22.
- **Blast radius:** Upload path only. No response-shape change.
- **Verify before fixing:** none
- **Verify after fixing:** Upload a long clip with `rotate=90`; while the request is pending, `curl localhost:8000/auth/me` from another terminal must answer immediately.
- **Depends on / blocks:** none

#### [L-07] Filler removal cuts ordinary words; multi-word fillers can never match
- **Category:** Logic
- **Severity:** High
- **Confidence:** High on the mechanism, Medium on intent (needs a product decision, see section 5)
- **Location:** `filler_remover.py:L7-L14`, `filler_remover.py:L48-L56`, `transcription_service.py:L49-L70`, `decision_engine.py:L81-L85`, `decision_engine.py:L100`, `decision_engine.py:L125`, `evaluator.py:L255`
- **Evidence:** `detect_fillers` compares each single Whisper token to the set and cuts every match:
  ```python
  word_clean = word_text.lower().strip('.,!?;:"\'-()[]')
  if word_clean in active_fillers:
      ... raw_cuts.append((round(start, 3), round(end, 3)))
  ```
  The English set contains `like, so, right, well, actually, okay, honestly, basically`. The Urdu and Hindi sets contain `aur` (and), `lekin` (but), `agar` (if), `bhi`, `hi`, `woh`, `phir`, `toh`, `bas`, `na`. The entries `you know`, `i mean`, `kind of`, `sort of`, `i guess` contain a space and a token never does, so they are unreachable. The same list exists three times with different contents.
- **Why it's a problem:** "I like this so much" is rendered as "I this much". For Urdu/Hindi nearly every sentence loses its conjunctions. This is the default behaviour of the main pipeline and cannot be turned off from the UI.
- **Proposed fix:**
  1. Create one module-level source of truth in `filler_remover.py`: `HESITATIONS_BY_LANGUAGE` (pure hesitation sounds: `um, uh, umm, uhh, hmm, hm, er, err, ah, ahh` and their per-language equivalents) and `PHRASE_FILLERS_BY_LANGUAGE` (multi-word phrases).
  2. `detect_fillers` cuts hesitation tokens always, and cuts a phrase only when consecutive tokens match it (slide a window of `len(phrase.split())` tokens). Words that are also ordinary vocabulary (`like`, `so`, `aur`, ...) are removed from the automatic lists unless the product owner decides otherwise.
  3. Delete `FILLER_WORDS_BY_LANGUAGE` from `transcription_service.py` and the local `FILLER_WORDS` in `decision_engine.classify_pauses`; make `get_filler_words` (move it to `filler_remover.py`), `classify_pauses` and `evaluator.evaluate` read the shared lists.
- **Blast radius:** Changes which words are cut in every edit (intended behaviour change). `evaluator` filler counts change accordingly. `app.py:L29`, `L407-L408`, `L2177-L2180` import/call sites.
- **Verify before fixing:** Get the product decision in section 5 (Q1). `rg -n "FILLER_WORDS|get_filler_words|detect_fillers" *.py`.
- **Verify after fixing:** Unit tests for `detect_fillers` with a synthetic transcript: "I like this" keeps all words; "um I mean it" cuts `um` and `I mean`. Run the 8-second sample clip end to end and read the cut list in the log.
- **Depends on / blocks:** Blocks L-20.

#### [L-08] A clip that fails to extract is silently dropped and captions drift
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `render_engine.py:L329`, `render_engine.py:L335-L337`, `render_engine.py:L358-L369`
- **Evidence:**
  ```python
  real_durations = [max(0.0, end - start) for (start, end) in segments]
  ...
  if duration < 0.05: ... continue          # not in the video, duration still counted
  ...
  except subprocess.CalledProcessError as e:
      print(f"   ❌ Clip {i+1} failed: ...")   # not in the video, duration still counted
  if not clip_files: raise RuntimeError(...)  # only fails when ALL clips failed
  ```
- **Why it's a problem:** `real_durations[i]` is what the caption generators use to place every later caption (`subtitle_service._seg_duration`). A segment that is not in the stitched video but still counts for its theoretical length shifts every following caption late by that amount, and part of the user's video is missing with the job reported as successful. Slivers under 0.05 s can be produced by user corrections (`cut_list.subtract` has no minimum length).
- **Proposed fix:**
  1. For skipped segments set `real_durations[i] = 0.0`.
  2. On `CalledProcessError` raise `RuntimeError(f"Could not encode segment {i+1} ({start:.2f}-{end:.2f}s)")` after removing the already written `temp_clip_*` files, instead of continuing. A partial video is worse than a clear failure.
  3. In `cut_list.apply_correction` call `normalize(result, duration, min_len=0.05)` on the final list so corrections cannot create unencodable slivers (re-check the "would remove the whole video" guard after that).
- **Blast radius:** `edit_render.cut` and all four render callers. Behaviour change: a single failed segment now fails the job.
- **Verify before fixing:** Read `subtitle_service._seg_duration` (`subtitle_service.py:L30-L50`) and confirm a `0.0` entry is used as-is and not treated as "missing" (if it uses `or`, change it to an explicit `is None` check).
- **Verify after fixing:** Unit test with a fake `subprocess.run` that fails for clip 2 and expects `RuntimeError`. Unit test that a 0.02 s keep segment yields `real_durations[i] == 0.0`.
- **Depends on / blocks:** none

#### [L-09] Jobs stuck in `queued`/`processing` are never swept; a failed multi-clip upload creates them
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `app.py:L1503-L1520` (sweeper), `app.py:L1204-L1238` (multi-clip registration), `app.py:L186-L198` (`_upright` can raise)
- **Evidence:** The sweeper skips unfinished jobs: `if job.get('status') not in ('queued', 'processing') and workspace.is_expired(born, now)`, and protects every file whose name contains a live job prefix (`app.py:L1516-L1518`). In `submit_video` the master job is registered as `queued` before the uploads are saved. If `_save_upload` raises 413 for clip 3, or `_upright` raises 422 in the second loop, the function exits with the master (and any clip jobs already registered) still `queued` and nothing ever runs them. Only the `_reject_unusable` failure path removes the master.
- **Why it's a problem:** Those entries stay in memory forever, show up in `GET /jobs/` as permanently queued, and their raw uploads (up to 1 GB each) are protected from the file sweep forever. The same happens to any job whose worker thread dies without reaching its `except` block.
- **Proposed fix:**
  1. In `submit_video`, wrap everything from the first `_save_upload` of the multi-clip branch through the `job_store[master_id]["clip_jobs"] = ...` line in `try/except BaseException`: on failure `_cleanup` every saved path, pop the master id and any registered clip ids from `job_store`, then re-raise. Remove the now-redundant inner `try/except HTTPException` at `app.py:L1220-L1224`.
  2. In `_sweep_expired_jobs`, expire unfinished jobs too once they are older than a hard limit, for example `born + max(TTL_SECONDS, 6 * 3600)`; mark them `error` ("timed out") and run `_full_cleanup`.
  3. Stamp `created_at` at registration time instead of lazily in the sweeper so the limit is measured from submission.
- **Blast radius:** Upload endpoint error paths, sweeper. No change for successful jobs.
- **Verify before fixing:** none
- **Verify after fixing:** Temporarily set `MAX_UPLOAD_BYTES` small, upload two clips where the second exceeds it, then check `GET /jobs/` is empty and `../work` has no `raw_*` files from the request.
- **Depends on / blocks:** Related to L-10.

#### [L-10] Multi-clip monitor loop can spin forever and parallelism is unbounded
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `app.py:L762-L831`, `app.py:L693`, `app.py:L1079`, `app.py:L1213`
- **Evidence:** The master starts `ThreadPoolExecutor(max_workers=n)` where `n` is the number of uploaded files (no upper limit), stores the futures in `clip_futures`, never reads them, and loops `while not all_done: sleep(1.5)` on `job_store[clip]["status"]` only.
- **Why it's a problem:** (a) If a clip thread ends without setting `done` or `error` (exception inside `_run_pipeline`'s own `except` block, or its `job_store` entry removed), the master never finishes and permanently occupies one of the two workers of the shared executor (`app.py:L137`); two such events stop all processing. (b) Uploading 30 clips starts 30 concurrent ffmpeg/OpenCV pipelines and can exhaust the machine. (c) On the failure path, running sibling clips are not stopped (`cancel_futures` only cancels unstarted work) while their input files are deleted under them.
- **Proposed fix:**
  1. Reject more than a fixed number of clips in `submit_video` (suggest `MAX_CLIPS = 10`, 400 with a clear message) and cap the pool: `max_workers=min(n, 3)`.
  2. Replace the polling loop's exit condition with the futures: keep polling for progress, but treat `all(f.done() for f in clip_futures)` as the end, and for each future call `f.exception()`; if set, or if the clip's status is not `done`, mark that clip `error` and take the existing failure path.
  3. Delete the unused `as_completed` import.
- **Blast radius:** Multi-clip uploads only. Behaviour change: clip-count limit; with more than 3 clips they run 3 at a time (progress averaging still works).
- **Verify before fixing:** Check how many files the UIs allow (`rg -n "multiple" studio.html home.html editor.html`).
- **Verify after fixing:** Upload 2 short clips and confirm the combined result; inject `raise RuntimeError` at the top of `_run_pipeline` for one clip and confirm the master reports `error` within seconds.
- **Depends on / blocks:** L-09

#### [L-11] Failed reference pre-fetch triggers the parallel downloads it was built to avoid
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `app.py:L713-L757`, `app.py:L770-L777`, `app.py:L415`, `app.py:L442`
- **Evidence:** When the master's pre-fetch fails it logs "Clips will use style preset '...' instead" and leaves `prefetched_style_profile = None`, but still passes `reference_link` to every clip. In `_run_pipeline`, `if prefetched_style_profile:` is false so `elif reference_link:` runs: each clip downloads and transcribes the reference itself, in parallel.
- **Why it's a problem:** N simultaneous yt-dlp downloads of a link that just failed (rate limiting, long timeouts), N extra Whisper runs serialized behind the lock, and behaviour that contradicts the log. Clips can also end up with different pacing if some downloads succeed.
- **Proposed fix:** In `_run_multi_clip_pipeline`, pass `reference_link if prefetched_style_profile else ""` to the clip pipelines (`app.py:L773`), so a failed pre-fetch really falls back to `style_preset` for every clip.
- **Blast radius:** Multi-clip jobs with a bad reference link only.
- **Verify before fixing:** none
- **Verify after fixing:** Submit 2 clips with `reference_link=https://invalid.invalid/x`; the log must show one download attempt and both clips using the preset.
- **Depends on / blocks:** none

#### [L-12] `custom_templates.json`: one read error wipes every user's templates
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `app.py:L2004-L2019`, `app.py:L2075-L2077`
- **Evidence:**
  ```python
  def _load_custom_templates() -> dict:
      ...
      except Exception:
          return {}
  ...
  templates = _load_custom_templates()
  templates[key] = {...}
  _save_custom_templates(templates)     # open(path, "w") + json.dump
  ```
- **Why it's a problem:** (a) If the file is unreadable or half-written once, the next save overwrites it with a single entry: all users' saved caption templates are lost. (b) Two concurrent `extract-reference-style` requests both load, both write, and one template disappears. (c) The write is not atomic, so a crash mid-write produces exactly the corrupt file from (a). (d) The file lives in the source directory and is not in `.gitignore`, so user data can be committed. (e) There is no delete endpoint and no size cap.
- **Proposed fix:** Move this to Postgres next to the users table: `custom_templates(key TEXT PRIMARY KEY, owner_id TEXT NOT NULL, label TEXT NOT NULL, config JSONB NOT NULL, created_at TEXT NOT NULL)`, created in `auth.init_db` (or a new small `templates.py` using the L-05 helper). `list_custom_templates` becomes a `SELECT ... WHERE owner_id = %s`; `extract_reference_style` an `INSERT`. On first start, import an existing `custom_templates.json` if present, then rename it to `custom_templates.json.migrated`.
- **Blast radius:** `GET /custom-templates`, `POST /extract-reference-style`. Response shapes stay the same. Needs a DB migration (new table).
- **Verify before fixing:** Check whether a `custom_templates.json` exists on the deployment machine (none exists in this checkout). Confirm the response shape `studio.html` expects from `/custom-templates`.
- **Verify after fixing:** Create two templates as two users; each sees only their own; restart; still there.
- **Depends on / blocks:** L-05

#### [L-13] Email OTP can be brute-forced
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `auth.py:L231-L232`, `auth.py:L249-L269`, `auth.py:L450-L466`
- **Evidence:** `_generate_otp` uses `random.randint` (not a CSPRNG). `/auth/verify-email` has no attempt counter: a wrong code leaves the OTP valid for the full 10 minutes, and success sets a session cookie.
- **Why it's a problem:** 900,000 codes and unlimited guesses for 10 minutes is within reach of a script, and combined with L-01 it lets an attacker verify an account for an email they do not own.
- **Proposed fix:** Use `f"{secrets.randbelow(900000) + 100000}"`. Add an `attempts INTEGER NOT NULL DEFAULT 0` column to `email_verifications` (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `init_db`); in `_verify_otp` increment it on every wrong code and delete the row when it reaches 5, so a new code must be requested. Compare with `secrets.compare_digest`.
- **Blast radius:** Signup verification. Needs a column migration.
- **Verify before fixing:** none
- **Verify after fixing:** Six wrong codes then the right one must fail with 400; resend and verify succeeds.
- **Depends on / blocks:** L-05

#### [L-14] Login lockout ignores successful logins and lets anyone lock any account
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `auth.py:L279-L298`, `auth.py:L507-L527`
- **Evidence:** `_is_locked_out` counts rows with `success = FALSE` in the last 15 minutes. A successful login inserts a `success = TRUE` row but does not reset the count. Failures are recorded for any email string, including ones with no account, and rows are never deleted.
- **Why it's a problem:** Four typos, a successful login, then one more typo within 15 minutes locks the user out. Anyone can keep a known email locked out indefinitely with 5 requests every 15 minutes. `login_attempts` grows without bound.
- **Proposed fix:** Count only failures newer than the most recent success: add `AND attempted_at > COALESCE((SELECT MAX(attempted_at) FROM login_attempts WHERE email = %s AND success = TRUE), '')` to the query. Delete rows older than 24 hours in `_record_attempt`. The targeted-lockout problem needs a product decision (section 5, Q3); the minimal change is to key the counter on email plus client IP.
- **Blast radius:** Login only.
- **Verify before fixing:** none
- **Verify after fixing:** 4 wrong, 1 right, 1 wrong, then right again must succeed.
- **Depends on / blocks:** L-05

#### [L-15] Sessions cannot be revoked; cookie lacks `Secure`
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `auth.py:L200-L225`, `auth.py:L532-L535`, `auth.py:L620-L644`, `auth.py:L386-L396`
- **Evidence:** The session is a 7-day JWT containing only `sub`, `iat`, `exp`. Logout deletes the cookie client-side. `reset_password` and `update_me` change the hash but every previously issued token stays valid. Other unused reset tokens for the same user also stay valid. `set_cookie` does not set `secure`.
- **Why it's a problem:** A user who resets their password because the account was compromised does not evict the attacker for up to 7 days.
- **Proposed fix:** Add `token_version INTEGER NOT NULL DEFAULT 0` to `users`. Put it in the JWT (`"ver"`), compare it in `get_current_user` (the row is already loaded there), and increment it in `reset_password` and on password change in `update_me` (re-issue the caller's cookie in `update_me`). In `reset_password` also mark all of the user's other tokens used. Set `secure=True` when `APP_BASE_URL` (L-03) starts with `https://`.
- **Blast radius:** All authenticated requests (one extra comparison). Existing sessions without `ver` should be treated as version 0 so nobody is logged out by the deploy. Needs a column migration.
- **Verify before fixing:** none
- **Verify after fixing:** Log in on two browsers, reset the password, confirm the other browser gets 401 on `/auth/me`.
- **Depends on / blocks:** L-03, L-05

#### [L-16] Upload filename is used unsanitized in paths and download names
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High (crash and extension), Medium (quote breakage, legacy path only)
- **Location:** `app.py:L1165-L1166`, `app.py:L1215-L1216`, `app.py:L1549-L1554`, `clip_combiner.py:L397-L404`, `app.py:L2384-L2390`
- **Evidence:** `safe_name = os.path.basename(vf.filename)` is the only cleaning. `vf.filename` may be `None` (TypeError, 500). The name is embedded in `raw_*`, `edited_*`, `restyled_ref_*` paths; `/combine-clips/` writes those paths into an ffmpeg concat list as `file '{abs_path}'`, which breaks on an apostrophe (`it's.mp4`). `/download` names the file `edited_{original name}`, so a `.mov` upload downloads as `edited_x.mov` although the content is always MP4 (`/export-result` already normalizes to `.mp4`).
- **Why it's a problem:** Uploads with unusual names fail with opaque 500s or ffmpeg errors; downloaded files can carry the wrong extension and fail to open in strict players.
- **Proposed fix:** Add one helper in `app.py` and use it at both upload sites:
  ```python
  def _safe_filename(name: str | None) -> str:
      base = os.path.basename(name or "") or "video.mp4"
      stem, ext = os.path.splitext(base)
      stem = re.sub(r"[^A-Za-z0-9._ -]", "_", stem).strip(" .")[:80] or "video"
      return stem + (ext if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", ext) else ".mp4")
  ```
  In `download_video` use `os.path.splitext(safe_name)[0] + ".mp4"`. In `_write_concat_list` escape quotes per ffmpeg's concat syntax: `abs_path.replace("'", "'\\''")`.
- **Blast radius:** File names in `../work`, the download filename, `job['filename']` shown by `/jobs/`. Projects stored in the browser keep whatever name they already saved.
- **Verify before fixing:** `rg -n "safe_name|\['filename'\]|get\('filename'" app.py` for every consumer.
- **Verify after fixing:** Upload `it's "my" clip#1.MOV`; job completes; the download is named `edited_it_s _my_ clip_1.mp4`.
- **Depends on / blocks:** none

#### [L-17] Shared job state is mutated from several threads without a lock
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** Medium (the race window is small; the failure mode is certain when hit)
- **Location:** `app.py:L128` (`job_store`), `app.py:L1457` (`_EXPORTS`), `app.py:L2504-L2513` (`/jobs/` iterates the live dict), `app.py:L1924-L1941`
- **Evidence:** `list_jobs` runs a dict comprehension over `job_store.items()` while pipeline threads, the sweeper thread and request threads insert and pop entries. `_EXPORTS` gets one entry and one new daemon thread per `POST /export` and is never pruned.
- **Why it's a problem:** `RuntimeError: dictionary changed size during iteration` gives `home.html` an intermittent 500 on its job list. Every export leaks a dict entry for the life of the process. Double-clicking Export runs two full encodes in parallel with no cap.
- **Proposed fix:**
  1. Iterate over a snapshot in `list_jobs`: `for job_id, job in list(job_store.items())` (the sweeper and `_referenced_files` already do this).
  2. Run exports on the shared executor (`_get_executor().submit(_work)`) instead of a bare thread, so they are bounded.
  3. In `_sweep_expired_jobs`, drop `_EXPORTS` entries whose `path` no longer exists or that are older than the TTL (store `created_at` in the entry).
  4. In `export_video`, if the same user already has an export for this `job_id` in `processing`, return that `rid` instead of starting another.
- **Blast radius:** `/jobs/`, export endpoints. Exports now queue behind running edits (behaviour change; raise `max_workers` if that is not acceptable).
- **Verify before fixing:** none
- **Verify after fixing:** Export twice quickly and confirm one ffmpeg process; export, wait, and confirm the file downloads.
- **Depends on / blocks:** none

#### [L-18] Hard-cut combine feeds mixed-format clips to the concat demuxer
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** Low to Medium (not reproduced; depends on ffmpeg's handling of mid-stream parameter changes)
- **Location:** `clip_combiner.py:L166-L189`, `clip_combiner.py:L136-L141`, `app.py:L847-L856`
- **Evidence:** Only the crossfade path normalizes resolution and frame rate (`_get_common_resolution`, "Bug 6 fix"). `_combine_hard_cut`, which is also the last step of the fade path and the default for `transition_type=auto` without a reference, passes the clips straight to `-f concat`. Its docstring claims codec copy with a re-encode fallback; the code always re-encodes and has no fallback.
- **Why it's a problem:** In a multi-clip upload with no frame format selected, clips keep their own resolution/fps (phone portrait plus landscape screen recording, 30 and 60 fps). The concat demuxer expects identical stream parameters; results range from a correct file to stretched frames or A/V drift, depending on inputs.
- **Proposed fix:** Extract the normalization loop from `_combine_crossfade` (`clip_combiner.py:L247-L262`) into `_normalise(clip_paths, job_prefix) -> list[str]` and call it from `_combine_hard_cut` when `ffprobe` shows the inputs do not all share width, height and frame rate. Fix the docstring.
- **Blast radius:** Multi-clip combine with hard cut or fade. Adds one encode per clip only in the mixed case.
- **Verify before fixing:** Reproduce first: combine a 1920x1080/30 clip with a 1080x1920/60 clip via the multi-clip upload with no frame format and inspect the output. If it is correct, downgrade this to a docstring fix.
- **Verify after fixing:** Same test; `ffprobe` shows one resolution and the audio stays in sync at the end.
- **Depends on / blocks:** C-04 (shared probe helper)

#### [L-19] Single-clip reference failure ignores the chosen preset and the CV switch
- **Category:** Logic
- **Severity:** Low
- **Confidence:** High
- **Location:** `app.py:L442-L488`
- **Evidence:** On a failed reference download the single-clip path uses `_preset_profile("balanced")` (`L483`, `L486`), not the user's `style_preset`; the multi-clip path falls back to `style_preset`. Also the reference video is downloaded and `detect_scenes` is run on it even when `cv_analysis` is false (`L460`, `L466`), whereas the multi-clip path checks `if cv_analysis or copy_caption_style`. The comment at `L453-L459` describes a limitation ("yt-dlp downloads audio only") that no longer exists.
- **Why it's a problem:** A user who picked "aggressive" plus a bad link silently gets "balanced". Disabling CV analysis does not skip the slow reference download and scene detection.
- **Proposed fix:** Replace both `"balanced"` literals with `style_preset`. Guard the block at `L460-L478` with `if cv_analysis or copy_caption_style:` and the `detect_scenes` call with `if cv_analysis`. Delete the stale comment.
- **Blast radius:** Single-clip jobs with a reference link.
- **Verify before fixing:** none
- **Verify after fixing:** Submit with `style_preset=aggressive`, an invalid link, and confirm the log prints the aggressive thresholds.
- **Depends on / blocks:** Disappears if C-01 is done first (one shared code path).

#### [L-20] Correction re-evaluation uses the English filler list
- **Category:** Logic
- **Severity:** Low
- **Confidence:** High
- **Location:** `app.py:L1620-L1625`
- **Evidence:** The `/feedback/` re-render calls `evaluate(...)` without `active_filler_words`; the main pipeline (`app.py:L568`) and the restyle path (`app.py:L2263`) pass the language-specific set.
- **Why it's a problem:** After any timestamp correction on a non-English video, the report's filler statistics are computed against English words and change for no visible reason.
- **Proposed fix:** Pass `active_filler_words=get_filler_words(job.get('language', 'en'))` in `_render_correction`.
- **Blast radius:** Report numbers after a correction.
- **Verify before fixing:** none
- **Verify after fixing:** Compare `/report/{id}` filler counts before and after a correction on a Hindi clip.
- **Depends on / blocks:** L-07 changes the function it calls.

#### [L-21] Input validation gaps on job endpoints
- **Category:** Logic
- **Severity:** Low
- **Confidence:** High
- **Location:** `app.py:L1562-L1563` and `cut_list.py:L50-L56` (NaN/inf timestamps pass every comparison), `app.py:L1883-L1892` and `edit_render.py:L120` (`captions` JSON that is not a list of objects raises `AttributeError`/`KeyError`, a 500), `app.py:L1536-L1542` (`HTTPException(202)` for "still processing" and `500` for "job failed"), `app.py:L1081`, `L1108-L1109` (`style_preset`, `transition_type`, `transition_duration` unchecked)
- **Evidence:** `float('nan')` is accepted by `Form(...)` floats; `end <= start` and `start >= duration` are both false for NaN, so NaN reaches `normalize`. `_caption_source` only catches `ValueError` from `json.loads`.
- **Why it's a problem:** Malformed requests produce 500s and stack traces instead of 400s; a failed job reads as a server fault to monitoring; unknown presets silently become defaults.
- **Proposed fix:** In `apply_correction` add `if not all(math.isfinite(v) for v in (start, end)): raise ValueError(...)`. In `_caption_source` validate with the same comprehension `caption_command` uses (`app.py:L1989-L1990`), extracted to `_parse_captions(raw) -> list[dict]`, and use it in both places. In `download_video` return 409 for unfinished and failed jobs. Validate `style_preset` against the `_preset_profile` keys and `transition_duration` to `0 < d <= 3` in `submit_video`.
- **Blast radius:** Error responses only. Check the legacy editor's handling of the `/download` status codes before changing them.
- **Verify before fixing:** `rg -n "download/" *.html` and read how each caller treats non-200.
- **Verify after fixing:** `curl` each bad input and expect 400/409.
- **Depends on / blocks:** none

#### [L-22] `/version` is public and shells out on every request; `/debug/{job_id}` ships in the API
- **Category:** Logic
- **Severity:** Low
- **Confidence:** High
- **Location:** `app.py:L1342-L1349`, `app.py:L1352-L1371`
- **Evidence:** `"yt_dlp": __import__('subprocess').getoutput('yt-dlp --version').strip()` runs a shell command inside an unauthenticated `async def` handler. `/debug/{job_id}` dumps the raw job entry (owner-checked).
- **Why it's a problem:** Each `/version` hit blocks the event loop for the duration of a process spawn, without login: a trivial way to stall the server (see L-06). The hardcoded `"CutSmart-2025-build-v4"` string is stale. Neither endpoint is called by any page (see FE cross-check list).
- **Proposed fix:** Return the `_ytdlp_ver` already computed at startup (`app.py:L59`), or delete `/version`. Gate `/debug/{job_id}` behind `os.environ.get("DEV_MODE") == "1"` or delete it.
- **Blast radius:** None in the product; any manual debugging habit.
- **Verify before fixing:** Confirm with the owner that nothing external polls `/version`.
- **Verify after fixing:** `curl /version` returns instantly.
- **Depends on / blocks:** L-06

### 2.1b Logic defects - front-end

Front-end findings come from static reading only; nothing was run in a browser. Lines in `studio.html` are very long (200 to 900 characters), so a line reference usually means one function.

#### [FE-01] `?next=` allows script execution and open redirect on login and splash
- **Category:** Logic
- **Severity:** Critical
- **Confidence:** High for the redirect; Medium-High for `javascript:` execution (not browser-tested; the app sets no CSP header)
- **Location:** `login.html:L430-L435`, `login.html:L458-L460`, `login.html:L501`, `splash.html:L98-L102`, `splash.html:L126`
- **Evidence:**
  ```js
  // login.html: runs on page load when a session already exists
  if (r.ok) window.location.href = _nextUrl();
  function _nextUrl() { return new URLSearchParams(window.location.search).get('next') || '/home.html'; }
  // splash.html
  return n && n.startsWith('/') ? n : '/home.html';
  window.location.replace(target);
  ```
- **Why it's a problem:** A logged-in user who opens `/login.html?next=javascript:...` runs attacker script in the app origin. Combined with L-02 that script can change the password with one `PUT /auth/me`: account takeover from a single link. `/login.html?next=https://evil.example` redirects off-site. `/splash?next=//evil.example` passes the `startsWith('/')` check, so a user is sent off-site right after a genuine login.
- **Proposed fix:**
  1. Add `assets/nav.js` exporting `safeNext(raw, fallback = '/home.html')`: return `fallback` unless `raw` starts with exactly one `/` (reject `//` and `/\`); then `const u = new URL(raw, location.origin)`; return `fallback` unless `u.origin === location.origin`; return `u.pathname + u.search + u.hash`.
  2. Use it in `login.html` (`_nextUrl`) and `splash.html` (`nextUrl`).
- **Blast radius:** Post-login navigation on `login.html`, `signup.html` (goes through splash), Google callback (`auth.py:L706`).
- **Verify before fixing:** Confirm the three URLs above misbehave in a browser.
- **Verify after fixing:** Each lands on `/home.html`; `/login.html?next=/studio.html?p=abc` still lands on that project.
- **Depends on / blocks:** Pair with L-02.

#### [FE-02] Changing a caption style during a re-edit orphans the running job
- **Category:** Logic
- **Severity:** High
- **Confidence:** High (the backend 409 is at `app.py:L1876-L1877`)
- **Location:** `studio.html:L602` (`markDirty`), `L713-L716` (`loadCaptions`), `L644-L646` (`poll`); reached from `generate` (`L624-L636`), `correctCut` (`L843-L855`), `submitReferenceRestyle` (`L856-L877`), timeline drag (`L786-L787`)
- **Evidence:**
  ```js
  function markDirty(){saveProject();if(!RESULT_BLOB)return;setMainButton();if(!FRAME_DIRTY&&pipeSig()===PIPE_SIG)refreshCaptions()}
  // loadCaptions:
  if(r.status===404||r.status===409){JOB=null;setMainButton();toast('This edit has expired on the server...');return}
  ```
- **Why it's a problem:** During a Re-edit, Restore, Remove-section or reference restyle, `RESULT_BLOB` still holds the old edit while `JOB` is already the new, still-processing job id. Any font, colour or slider change posts `/captions/{new job}`, gets 409 (job not `done`), and sets `JOB = null`. `poll()` then requests `/status/null` five times, shows "Server was restarted", and marks the project as failed while the server keeps rendering a job nothing tracks.
- **Proposed fix:**
  1. In `markDirty` and `capsChanged`, do not call `refreshCaptions()` while `BUSY`; in `loadCaptions`, return early when `BUSY`.
  2. Poll a separate `PENDING_JOB` variable and assign `JOB` only inside `finish()`.
  3. In `loadCaptions`, never null `JOB` while a poll is active.
  `finish()` already calls `refreshCaptions(true)`, so style changes made while waiting are applied at the end.
- **Blast radius:** Studio job lifecycle (all four entry points listed).
- **Verify before fixing:** Make an edit, change Silence trimming, press Re-edit, move the Font Size slider while it processes; watch the network tab for the 409.
- **Verify after fixing:** Same steps: no `/captions` request while processing, the edit completes and shows the new size.
- **Depends on / blocks:** FE-03, FE-04 touch the same functions. FE-20 removes the root cause.

#### [FE-03] Polling loops never stop on 401/403/500 or a network failure
- **Category:** Logic
- **Severity:** High
- **Confidence:** High
- **Location:** `studio.html:L644-L652` (`poll`, driven by `setInterval`), `studio.html:L943-L946` (export loop)
- **Evidence:**
  ```js
  if(r.status===404){if(++miss>=5)fail(...);return}
  miss=0;const d=await r.json(); ... }catch(_){}
  for(;;){await new Promise(x=>setTimeout(x,700));
    const q=await (await fetch(API+'/export-status/'+rid,{credentials:'include'})).json();
    if(q.status==='error')throw ...;if(q.status==='done')break}
  ```
- **Why it's a problem:** A 401 (session expired), 403 or 500 returns `{detail}`, so the UI sits on "Processing 0%" forever; network errors are swallowed. After a server restart `/export-status` returns 404, `q.status` is `undefined`, and the export loop spins every 700 ms forever with Export disabled. `setInterval` with an async body can overlap, so two polls can both see `done` and run `finish()` twice.
- **Proposed fix:** Replace `setInterval` with a self-rescheduling `setTimeout` (one request in flight). Check `r.ok`: on 401 go to login; on any other failure or thrown error count consecutive failures and call `fail()` after 5. In the export loop throw on `!res.ok` and apply the same cap.
- **Blast radius:** Studio processing and export UI.
- **Verify before fixing:** none
- **Verify after fixing:** Stop the server mid-processing and mid-export; an error appears within about 15 seconds and the buttons re-enable.
- **Depends on / blocks:** FE-02

#### [FE-04] An older `/captions` response can overwrite a caption text edit
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** Medium (timing dependent, reasoned from code)
- **Location:** `studio.html:L711-L722`
- **Evidence:**
  ```js
  function refreshCaptions(now){clearTimeout(CAP_TIMER);
    return new Promise(done=>{CAP_TIMER=setTimeout(()=>loadCaptions().then(done),now?0:150)})}
  const seq=++CAP_SEQ, regroup=!CAP_EDITED||ST.wpc!==LAST_WPC;   // captured at send time
  const d=await r.json();if(seq!==CAP_SEQ)return;
  if(regroup){CAPS=parseSrt(d.srt);...}
  ```
- **Why it's a problem:** `CAP_SEQ` is bumped when the debounced request starts, 150 ms after the user's action. A style-change request already in flight with `regroup = true` that returns inside that window replaces `CAPS` with the server's list; the follow-up request then sends that list as "edited", and the user's text edit is gone. Also, `clearTimeout` leaves the superseded promise pending; if that happens during the `await refreshCaptions(true)` in `finish()` (`L698`), `BUSY` stays true.
- **Proposed fix:** Increment `CAP_SEQ` inside `refreshCaptions()` at schedule time; recompute `regroup` after the response from the current `CAP_EDITED`; resolve the superseded promise when clearing the timer.
- **Blast radius:** Live caption preview.
- **Verify before fixing:** Throttle `/captions` to about 1 s in devtools, change the font, immediately edit a caption.
- **Verify after fixing:** The edit survives.
- **Depends on / blocks:** FE-02

#### [FE-05] A transient failure when reopening a project discards its finished edit
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `studio.html:L910-L922`
- **Evidence:**
  ```js
  try{const r=await fetch(API+'/status/'+rec.jobId,...);const st=r.ok?(await r.json()).status:null; ... }catch(_){}
  PROJ.update(PID,{status:'draft',jobId:null});
  toast('Your previous edit has expired on the server...')
  ```
- **Why it's a problem:** A network blip, 401 or 500 on page open resets the project to draft and erases `jobId` although the edit still exists on the server; the user must re-run the whole pipeline. A job in `error` gets the same misleading "expired" message.
- **Proposed fix:** Clear `jobId` only on 404 or 403. On 401 redirect to login. On network error or 5xx keep the record and show "Could not reach the server" with a retry. Handle `st === 'error'` with its own message.
- **Blast radius:** Studio project open.
- **Verify before fixing:** none
- **Verify after fixing:** Open a finished project offline, go online, reload: the edit opens.
- **Depends on / blocks:** none

#### [FE-06] Unescaped strings in `innerHTML` (stored self-XSS)
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High on the sinks. Reach today is the user's own session: templates are per user (`app.py:L2022-L2026`) and presets live in per-user localStorage.
- **Location:** `studio.html:L583` (`tcard` name) fed by `L594` (preset name from `#pname`, `L585`) and `L982`/`L1001` (template `label`, derived from the uploaded file name at `app.py:L2073`); `studio.html:L538` (`${tn}`); `L567` (`value="${ST.tsearch}"`); `L395-L396`, `L430` (`ST.emColor`/`ST.emFont` inside a `style="..."` string); `L551`, `L557` (`value="${ST.color}"`); `L672` (`c.index` in an `onclick`; `esc()` followed by `.slice(0,30)` can split an entity); `editor.html:L4457` (toast `innerHTML` with `err.message`/`data.error`)
- **Evidence:**
  ```js
  function tcard(id,name,...){return `<div class="tcard ..."><div class="hd"><span>${name}</span>...`
  TPL.push([data.key,data.label,'Aa']);
  ```
- **Why it's a problem:** A preset named `<img src=x onerror=...>`, or a reference video uploaded with that file name, executes on every Templates render and persists. It becomes cross-user XSS the moment templates or presets are shared, and it is a second route to the L-02 takeover on a shared machine.
- **Proposed fix:** One `esc()` in `assets/ui.js` that escapes `& < > " '`; apply it to `name`, `tn`, `ST.tsearch` and every `ST.*` value placed in an attribute; coerce `c.index` with `Number()`; build the editor toast with `textContent`. Server side, strip `<>"'&` from `label` in `extract_reference_style` (`app.py:L2073`).
- **Blast radius:** Templates tab rendering.
- **Verify before fixing:** Save a preset named `<b>x</b>"'`.
- **Verify after fixing:** It renders literally and the page still works.
- **Depends on / blocks:** FE-07, FE-19

#### [FE-07] Hex colour and size text inputs are not validated
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `studio.html:L551`, `L557` (inputs), `L390` (`hex2ass`), `L549` (`#szv`); server side `edit_render.py:L46`, `L62`
- **Evidence:**
  ```js
  <input class="in w" id="hx" value="${ST.color}" onchange="ST.color=this.value;touch()">
  function hex2ass(h){h=h.replace('#','');return '&H00'+h.slice(4,6)+h.slice(2,4)+h.slice(0,2)}
  ```
- **Why it's a problem:** Typing `#fff` or `red` sends a malformed `subtitle_colour` (`&H00fff`) to `/captions`, `/export`, `/edit-video/` and `/caption-preview`, is saved into the project and reloaded. `build_style` passes `subtitle_colour` and `emph_colour` into the ASS file unchecked. The size box accepts any number although the slider is 16 to 64.
- **Proposed fix:** Client: accept only `/^#?[0-9a-f]{6}$/i` (expand 3-digit forms), otherwise restore the previous value; clamp size to 16-64. Server: in `build_style` fall back to the default when a colour does not match `^&H[0-9A-Fa-f]{8}$`, and clamp `subtitle_size`.
- **Blast radius:** Caption styling inputs; projects saved with bad colours now render with the default.
- **Verify before fixing:** none
- **Verify after fixing:** Enter `#fff`: the preview is white and the request carries `&H00FFFFFF`.
- **Depends on / blocks:** none

#### [FE-08] Login shows "[object Object]" and points unverified users at a control that does not exist
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `login.html:L488-L499` (form is `novalidate`, `L405`); same pattern at `reset_password.html:L412`, `profile.html:L651`, `L716`
- **Evidence:**
  ```js
  if (r.status === 403) { showError(data.detail + ' - Check your inbox or resend the code below.'); }
  else { showError(data.detail || 'Could not log in. Please try again.'); }
  ```
- **Why it's a problem:** An email like `foo` gets a 422 whose `detail` is an array, rendered as `[object Object]`. An unverified user is told to "resend the code below", but the login page has no OTP or resend UI, so they are stuck unless they guess to sign up again.
- **Proposed fix:** Share a `detailText(data)` helper (the array handling already exists at `signup.html:L572-L574`) and use it everywhere `data.detail` is displayed. On 403, redirect to `/signup.html?verify=<email>` and make `signup.html` open its OTP step for that address and call `/auth/resend-otp`.
- **Blast radius:** Login error display; signup page gains a query parameter.
- **Verify before fixing:** Check how `signup.html` enters its OTP step today.
- **Verify after fixing:** Log in with `foo` (readable message) and with an unverified account (lands on the code entry step).
- **Depends on / blocks:** FE-19

#### [FE-09] Auth pages report success without checking the response
- **Category:** Logic
- **Severity:** Low
- **Confidence:** High
- **Location:** `signup.html:L671-L685` (resend), `profile.html:L723-L734`, `forgot_password.html:L374-L385`
- **Evidence:** The fetches are awaited and the success UI is shown without reading `r.ok`.
- **Why it's a problem:** Resend restarts the 60-second countdown and the profile page says "Reset link sent" even on 429 or 500; forgot-password shows success for a 422 typo.
- **Proposed fix:** Check `r.ok`. On 422 show "Enter a valid email"; on 429 or 5xx show an error; only then start the countdown or show success. (Not revealing whether an account exists only requires treating 200 the same for known and unknown emails, which the backend already does.)
- **Blast radius:** Three small handlers.
- **Verify before fixing:** none
- **Verify after fixing:** Stop the server and click each button: an error is shown.
- **Depends on / blocks:** none

#### [FE-10] Auth gate keeps executing after redirect and drops the project id
- **Category:** Logic
- **Severity:** Low
- **Confidence:** High
- **Location:** `studio.html:L354-L357`, `home.html:L173-L179`, `profile.html:L617-L636`, `editor.html:L3025-L3041`
- **Evidence:**
  ```js
  ...}catch(e){location.href='/login.html?next=/studio.html'}
  PROJ=CutSmartProjects.forUser(USER.id);   // USER is null here: unhandled TypeError
  ```
- **Why it's a problem:** Every logged-out visit to Studio throws; `next` loses `?p=...`, so after login Studio opens the most recent project instead of the requested one; pages paint their full shell before `/auth/me` resolves; on Home, dropping a file before the gate resolves raises "Cannot read properties of null" (`home.html:L224`).
- **Proposed fix:** A shared `requireUser()` in `assets/session.js` that returns the user or redirects with `next=encodeURIComponent(location.pathname + location.search)` and then never resolves. Hide `body` until it resolves.
- **Blast radius:** All gated pages.
- **Verify before fixing:** none
- **Verify after fixing:** Logged out, open `/studio.html?p=<id>`, log in, land on that project with no console error.
- **Depends on / blocks:** FE-01 (`safeNext` must accept the encoded path), FE-19

#### [FE-11] IndexedDB wrapper can hang and holds whole videos in memory
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** Medium (quota abort not exercised; storing `File` directly not verified on Safari)
- **Location:** `assets/projects.js:L22-L30` (`idb`), `L32-L40`, `L70-L86` (`readAll`, `create`), `L46`, `L89-L98`
- **Evidence:**
  ```js
  tx.oncomplete = () => resolve(...);
  tx.onerror = () => reject(tx.error);          // no tx.onabort
  const buf = await readAll(f, ...); items.push({ name: f.name, type: f.type, buf });
  ```
- **Why it's a problem:** A quota abort at commit fires only `abort`, so `create()` never settles and Home stays on "Preparing your video..." (`home.html:L218-L226`). Each upload is read into an ArrayBuffer, cloned into IDB, read back in Studio and uploaded, with no size check against the server's 1 GB limit. `write()` swallows localStorage quota errors, which can leave a stored video with no list entry.
- **Proposed fix:** Add `tx.onabort = () => reject(tx.error || new Error('Storage full'))`; cache one DB connection; store `File` objects directly and delete `readAll`; reject oversize files on Home before storing; make `write()` report failure and have `create()` remove the IDB record when it fails.
- **Blast radius:** Home upload and Studio project load.
- **Verify before fixing:** Test `File` storage in Safari before relying on it.
- **Verify after fixing:** Upload a file larger than the remaining quota: an error is shown, no hang.
- **Depends on / blocks:** none

#### [FE-12] Blob URLs and blobs leak on every re-edit and export
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `studio.html:L694`, `L936`, `L948`, `L366`, `L895-L897`
- **Evidence:** `vid.src=URL.createObjectURL(RESULT_BLOB)` with no `revokeObjectURL` anywhere in the file.
- **Why it's a problem:** Every Restore, Remove-section or Re-edit downloads the full intermediate again and pins the previous one in memory; exports and `.srt` downloads are never released. A long editing session on a large video exhausts tab memory.
- **Proposed fix:** Keep `RESULT_URL` and revoke it before reassigning. Revoke download URLs after `a.click()` in a `setTimeout`. Download the export with a plain `<a href="/export-result/{rid}" download>` (same-origin cookie auth) instead of buffering it through `fetch`.
- **Blast radius:** Studio preview and export download.
- **Verify before fixing:** none
- **Verify after fixing:** Re-edit five times and compare tab memory in the browser task manager.
- **Depends on / blocks:** none

#### [FE-13] Live-caption fonts are dropped when two `show()` calls overlap
- **Category:** Logic
- **Severity:** Low
- **Confidence:** Medium
- **Location:** `assets/live_captions.js:L22-L35`, `L8`
- **Evidence:**
  ```js
  if (!loaded.has(fam)) { loaded.add(fam); urls.push(...(cat[fam] || [])); }   // marked before use
  const urls = await newFontUrls(ass);
  if (ticket !== shown) return;                                                // urls discarded
  ```
- **Why it's a problem:** If a second `show()` arrives while the first awaits `/font-catalog`, the first marks the families as loaded and then bails; the second gets no URLs, so the preview uses a fallback font while the export uses the real one (preview and export no longer match). A failed catalog fetch is cached as `{}` for the page lifetime.
- **Proposed fix:** Return `{fams, urls}` from `newFontUrls` and add to `loaded` only after the ticket check passes. Check `r.ok` and do not cache a failed catalog.
- **Blast radius:** Live caption preview.
- **Verify before fixing:** none
- **Verify after fixing:** Reopen a project with edited captions in a non-default font with the network throttled: the right font appears.
- **Depends on / blocks:** none

#### [FE-14] Home shows "Processing" forever for jobs the server no longer has
- **Category:** Logic
- **Severity:** Low
- **Confidence:** High
- **Location:** `home.html:L186-L191`
- **Evidence:** `l.forEach(p=>{const s=j[p.jobId];if(!s)return; ...`
- **Why it's a problem:** After a server restart (the job store is in memory), a project saved as `processing` keeps that badge until someone opens it.
- **Proposed fix:** When `/jobs/` answered OK and a `processing` project's `jobId` is missing from it, set the project to `draft`.
- **Blast radius:** Home project list.
- **Verify before fixing:** none
- **Verify after fixing:** Start a job, restart the server, reload Home.
- **Depends on / blocks:** L-17 (the `/jobs/` 500)

#### [FE-15] Replace video leaves a stale thumbnail and duration
- **Category:** Logic
- **Severity:** Low
- **Confidence:** High
- **Location:** `studio.html:L937-L939`, `home.html:L210-L214`
- **Evidence:** The update sets `name`, `status`, `jobId`, `caps` but not `dur`, `thumb`, `editedDur` or `pipe`.
- **Why it's a problem:** Home and the library show the old clip's picture and length until the next edit finishes.
- **Proposed fix:** Move `meta()` from `home.html` into `assets/projects.js` and call it from both places.
- **Blast radius:** Project records.
- **Verify before fixing:** none
- **Verify after fixing:** Replace a video and check Home.
- **Depends on / blocks:** none

#### [FE-16] Legacy editor sends a field the backend ignores; its Home hand-off code cannot work
- **Category:** Logic
- **Severity:** Medium
- **Confidence:** High
- **Location:** `editor.html:L3752`, `editor.html:L3044-L3062`, `assets/projects.js:L6`, `L14`, `L47`
- **Evidence:** `fd.append('subtitle_adopt_from_reference', S.adoptRefStyle ? '1' : '0');` The backend parameter is `copy_caption_style` (`app.py:L1115`). `csMark()` and the `?new=1` loader read `cs_active`, `cs_projects`, `cs_lang` and open IndexedDB `cutsmart` at version 1 with a `pending` store; `projects.js` deletes those keys and upgrades the database to version 2 without `pending`, so that open throws `VersionError`.
- **Why it's a problem:** The editor's "adopt reference style" toggle silently does nothing, and the hand-off code is dead.
- **Proposed fix:** Decided by FE-17. If the editor is kept: rename the field to `copy_caption_style` and delete `L3044-L3062`.
- **Blast radius:** Legacy editor only.
- **Verify before fixing:** Section 5, Q2.
- **Verify after fixing:** If kept, submit with the toggle on and see "Caption style copied" in the server log.
- **Depends on / blocks:** FE-17

### 2.2 Dead code - backend

#### [D-01] `render_engine.render_final_video` is an unused 195-line copy of the live render path
- **Category:** Dead code
- **Severity:** Medium
- **Confidence:** High
- **Location:** `render_engine.py:L413-L606` (function, including the unused local `burn_cmd` at `L548-L557`), comments that still point at it: `render_engine.py:L165-L166`, `L304`, `L522-L525`, `subtitle_service.py:L131`
- **Evidence:** `rg -w render_final_video` finds only its definition and comments. The live path is `extract_and_concat_segments` (`L301-L410`) plus `burn_subtitles` (`L629-L665`), which duplicate its two passes line for line.
- **Why it's a problem:** Every fix to the render path (for example L-08) has an identical-looking second place that does nothing, and readers cannot tell which one runs.
- **Proposed fix:** Delete `L413-L606`. Reword the four comments to name `extract_and_concat_segments` / `burn_subtitles`.
- **Blast radius:** None at runtime.
- **Verify before fixing:** `rg -n "render_final_video" .` across the repo and the parent directory.
- **Verify after fixing:** `python -m pyflakes render_engine.py`; run `python tests/check_keyword_overflow.py` and `python locked_templates/check_locked_templates.py`; one end-to-end edit.
- **Depends on / blocks:** Do before L-08 and C-05.

#### [D-02] Unused functions
- **Category:** Dead code
- **Severity:** Low
- **Confidence:** High
- **Location:** `subtitle_service.py:L322-L357` (`create_animated_ass`), `evaluator.py:L23-L36` (`_ffprobe_duration`), `frame_extractor.py:L134-L144` (`get_segment_thumbnail`), `reframe.py:L100-L105` (`window_fractions`), `auth.py:L399-L403` (`get_current_user_optional`)
- **Evidence:** For each name, `rg -w <name>` over `*.py`, `tests/`, `locked_templates/`, `*.html` and `assets/*.js` returns only the definition (plus one comment for `create_animated_ass` at `subtitle_service.py:L370`). None is a route, a dependency, or looked up by string.
- **Why it's a problem:** Dead surface area that still has to be read and kept compiling.
- **Proposed fix:** Delete the five functions. Keep `_words_to_chunks`, `_build_ass_events`, `_x_margins` and `_ASS_ALIGNMENT`: `create_remapped_ass` still uses them.
- **Blast radius:** None.
- **Verify before fixing:** Re-run the `rg -w` checks, including the parent directory, in case of scripts outside the repo.
- **Verify after fixing:** `python -c "import app"` (needs DB env) or `python -m pyflakes *.py`.
- **Depends on / blocks:** none

#### [D-03] Dead work and vestigial values inside live functions
- **Category:** Dead code
- **Severity:** Low
- **Confidence:** High
- **Location and evidence:**
  - `app.py:L313`, `L390`, `app.py:L35`: `create_srt(transcript_data, srt_path)` writes an original-timeline SRT that nothing reads before `_cleanup(temp_files)` deletes it. After removal `subtitle_service.create_srt` (`subtitle_service.py:L1-L27`) has no callers.
  - `app.py:L762`, `L778`: `clip_futures` is filled and never read (becomes live if L-10 is fixed as proposed).
  - `caption_preview.py:L50-L54`: `write_captions` always returns `ass_path`, so `use_ass` is always truthy and the `subtitles=c.srt...force_style` branch cannot run. `_sample(st, preset, f)` ignores `preset` and `f` (`L19`).
  - `subtitle_service.py:L610-L615`: `rnd`, `prev_pos` and `next_pos()` (always returns `(0.0, 0.0)`) are leftovers of a removed random layout.
  - `edit_render.py:L279`: the `captions_burned` key is never read. `app.py:L1555`: the `X-Soft-Captions` response header has no consumer in any page.
  - `evaluator.py:L250`, `L261` (`filler_time_s`), `L296` (`avg_confidence`); `scene_detector.py:L194` (`peak_idx`), `L289-L290` (`rg_ratio`, `gb_ratio`); `color_grader.py:L194` (`result`): assigned, never used.
  - `auth.py:L410`, `L451`: `response` parameter unused in `signup` (used in `verify_email`). `app.py:L210`: `_require_owner`'s `job_id` parameter is unused.
  - `auth.py:L139-L148`: the `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` loop adds two columns that the `CREATE TABLE` just above already defines. Keep it only if databases created before those columns existed are still in use (section 5, Q5).
- **Why it's a problem:** Wasted I/O on every job (the SRT), and misleading code.
- **Proposed fix:** Remove each item. For `next_pos()`, inline `0.0, 0.0` at its call sites inside `create_remapped_pop_ass` and re-run both caption regression scripts, since that function has golden-style checks.
- **Blast radius:** None expected. The pop-caption edit must be byte-identical in output.
- **Verify before fixing:** `rg -n "next_pos\(|X-Soft-Captions|captions_burned|create_srt" .`
- **Verify after fixing:** `python tests/check_keyword_overflow.py`, `python locked_templates/check_locked_templates.py`, one end-to-end edit.
- **Depends on / blocks:** L-10 (for `clip_futures`)

#### [D-04] Unused imports
- **Category:** Dead code
- **Severity:** Low
- **Confidence:** High (pyflakes and vulture agree)
- **Location:** `app.py:L18` (`status`, `BackgroundTasks`), `app.py:L693` (`as_completed`), `clip_combiner.py:L31` (`tempfile`), `L33` (`Literal`), `color_grader.py:L2-L3` (`os`, `json`), `dl_fonts.py:L3` (`os`), `evaluator.py:L3` (`math`), `feature_extractor.py:L133` (`numpy` re-import inside function), `filler_remover.py:L1` (`pandas`), `frame_extractor.py:L4` (`json`), `render_engine.py:L6` (`field`), `scene_detector.py:L3-L4` (`AdaptiveDetector`, `HistogramDetector`), `style_extractor.py:L3` (`numpy`), `style_extractor.py:L279` (re-import of `transcribe_audio`)
- **Evidence:** `python -m pyflakes *.py`.
- **Why it's a problem:** Noise; `filler_remover` importing pandas adds import time for nothing. `app.py` also imports the same modules under several aliases (`subprocess` and `_sp`, `json` as `_json` three times, `time` and `_time`), see C-02.
- **Proposed fix:** Delete the listed imports. pyflakes also reports 21 f-strings without placeholders (`app.py:L426`, `L443`, `L503`, `L725`, `L751`, `L2193`, and in `color_grader.py`, `evaluator.py`, `style_extractor.py`): drop the `f` prefix.
- **Blast radius:** None.
- **Verify before fixing:** none
- **Verify after fixing:** `python -m pyflakes *.py` reports no "imported but unused".
- **Depends on / blocks:** none

#### [D-05] Stale comments and docstrings that describe removed behaviour
- **Category:** Dead code
- **Severity:** Low
- **Confidence:** High
- **Location:** `app.py:L322-L323`, `L577-L578`, `L612-L622` and `L1979-L1981` (refer to `/restyle/` and `/recaption/` endpoints that do not exist; the live ones are `/captions/{id}`, `/export/{id}`, `/restyle-from-reference/{id}`); `app.py:L2106-L2109` (says a restyled job cannot be restyled again, but `transcript_data` is now carried forward at `L2305`); `app.py:L2441-L2449` and `L2388` ("deleted on download": files are never deleted on download, see `workspace.py:L8-L10`); `app.py:L1826-L1827`; `app.py:L118-L127` (job schema comment lists 8 of roughly 30 keys); `render_engine.py:L94-L98` (the comments on `SOFT_CAPTION_JOBS` and `FONTS_DIR_ARG` are swapped); `clip_combiner.py:L169-L172`; scattered "Bug 3/4/5/6 fix" markers with no tracker behind them.
- **Evidence:** `rg -n "recaption|/restyle/" app.py` versus the route list.
- **Why it's a problem:** The next engineer will look for endpoints that do not exist and trust guarantees (delete on download) that were reversed.
- **Proposed fix:** Correct or delete each comment. Replace the job schema comment with a `TypedDict` (see C-01).
- **Blast radius:** None.
- **Verify before fixing:** none
- **Verify after fixing:** `rg -n "recaption|/restyle/|on download" *.py` returns nothing misleading.
- **Depends on / blocks:** none

#### [D-06] Repository hygiene: tracked-but-deleted file, stray artifacts, duplicate and incomplete manifests
- **Category:** Dead code
- **Severity:** Low
- **Confidence:** High, except the `httpx2` item (Low, see section 5 Q6)
- **Location and evidence:**
  - `feedback_service.py`: deleted in the working tree, still tracked (`git status` shows ` D`). No import of it remains.
  - `_intermediate_79148b78_extract.mp4`, `subtitles_remapped_79148b78.ass`, `subtitles_remapped_79148b78.srt` in the source directory: render leftovers from before `workspace.py` moved job files to `../work`. Ignored by `.gitignore`, never swept (the sweeper only scans `WORK_DIR`).
  - Two manifests: `requirements.txt` (pinned: `python-dotenv==1.2.3`, `httpx2==2.16.2`, `bcrypt==3.2.0`, `PyJWT==2.3.0`, `opencv-python==5.0.0.93`, `numpy`, `pandas`) and `../requirements.txt` (same list, unpinned). Neither lists `fonttools` (imported by `make_static_fonts.py`) or `Pillow` (required by `tests/check_keyword_overflow.py` per its docstring). `fontTools` is not installed in `../.venv`.
  - `GOOGLE_REDIRECT_URI` is defined in `../.env` and read nowhere (`auth.py:L654` derives the redirect URI from the request).
  - `if __name__ == "__main__":` demo blocks that reference a `test_video.mp4` that is not in the repo: `decision_engine.py:L165-L196`, `filler_remover.py:L79-L88`, `transcription_service.py:L157-L171`, `feature_extractor.py:L97-L111` (placed in the middle of the module, before `detect_edge_silences`), `style_extractor.py:L278-L298`, `color_grader.py:L262-L289`. These are test-only usage, not production code.
  - `dl_fonts.py`, `make_static_fonts.py`: one-off font build scripts with no caller. Not dead, but undocumented.
- **Why it's a problem:** A fresh install from either manifest may not match the working environment; stale files confuse "what is source".
- **Proposed fix:** `git rm feedback_service.py`. Delete the three stray media files. Keep one `requirements.txt` (the pinned one in the repo), delete the parent copy, add `fonttools` and `Pillow` under a `# dev / scripts` comment. Remove `GOOGLE_REDIRECT_URI` from `.env` or use it (it would also remove a Host-header dependence similar to L-03). Move the `__main__` blocks into real tests (section 4) or delete them; at minimum move the one in `feature_extractor.py` to the end of the file. Add a short "Scripts" section to a README naming the two font scripts.
- **Blast radius:** None at runtime.
- **Verify before fixing:** `rg -n "feedback_service" .`; confirm with the owner that the `__main__` demos are not used in a report or demo.
- **Verify after fixing:** Fresh venv, `pip install -r requirements.txt`, server starts.
- **Depends on / blocks:** Batch 0

### 2.2b Dead code - front-end

#### [FE-17] `index.html` is unreachable; `editor.html` is reachable only by typing its URL
- **Category:** Dead code
- **Severity:** Medium
- **Confidence:** High
- **Location:** `index.html` (2,346 lines, 123 KB), `editor.html` (4,683 lines, 164 KB), routes `app.py:L1320-L1323`, stale comment `splash.html:L97`
- **Evidence:** `index.html` has no route in `app.py` and no static mount serves the source directory. It has no auth guard. 1,270 of its 1,760 unique whitespace-stripped lines (72%) appear verbatim in `editor.html`, and 48 of its 54 function names are shared: it is an older copy. No page, `assets/*.js` file or Python file links to `/editor` or `editor.html`; the only references are the editor's own login redirect (`editor.html:L3039`) and the comment in `splash.html`.
- **Why it's a problem:** 7,000 lines (57% of all front-end code) that must be kept working against every API change, already drifting (FE-16), for pages users cannot navigate to.
- **Proposed fix:**
  1. `git rm index.html`.
  2. Decide on `editor.html` (section 5, Q2). If retired: `git rm editor.html`, delete the `/editor` routes, then the endpoints only it used become removable: `GET /download/{id}`, `GET /srt/{id}`, `POST /combine-clips/` (`app.py:L1524-L1556`, `L1805-L1834`, `L2333-L2498`). `clip_combiner.combine_clips` stays: the multi-clip upload uses it (`app.py:L854`).
  3. Fix the comment at `splash.html:L97`.
- **Blast radius:** None for `index.html`. Retiring the editor removes the only UI for combining already-edited jobs and for the plain `/download`.
- **Verify before fixing:** `rg -n "index\.html|/editor|editor\.html" .` in the repo and parent directory; ask whether anyone bookmarks `/editor`; check whether the FYP report or demo script references it.
- **Verify after fixing:** Every link on every remaining page resolves; `GET /openapi.json` no longer lists removed routes.
- **Depends on / blocks:** Blocks FE-16. Shrinks C-01 and L-16, L-18, L-21 if the endpoints go.

#### [FE-18] Dead code in `studio.html`, `home.html`, auth pages and `assets/`
- **Category:** Dead code
- **Severity:** Low
- **Confidence:** High for the JS items (each name was searched across all HTML and JS including inline handlers); Medium for the CSS classes (word-match based)
- **Location and evidence:**
  - `studio.html:L348` `PRESET_LAYOUT={}` never read. `L745`, `L747`, `L697` `CAP_DIRTY` is write-only. `L386` `$('lCap').parentElement.onclick=null` is a no-op. `L377` `fmt=(s,f)` ignores `f`. `L339`, `L982`, `L1001`: the third element of every `TPL` entry (`'Aa'`) is never read. `L342` `LANGS=CUTSMART_LANGUAGES` is a pure alias. `L885` `['tAdd'].forEach(...)` loops over one element. `L597` `else if(p==='custom'){ST.em=false}else{ST.em=false}` has identical branches.
  - `studio.html:L400-L407` `placeOvl(o,masked)`: the only caller (`L430`) passes `!!RESULT_BLOB` right after an early return when it is truthy (`L429`), so `masked` is always false and the `bg` branch cannot run.
  - `studio.html:L611-L612`: `al` can only be 6, 10 or 2, so the `al===8` test never matches and top-positioned captions always send `subtitle_margin_v=0`. This may be a latent bug rather than dead code: see section 5, Q8.
  - `assets/live_captions.js:L62` `destroy` is exported and never called.
  - Unused CSS: `studio.html` `.tools` (`L49`), `.cq` (`L57`), `.dirty` (`L112`), `.pill.r1` (`L126`), `.tp` (`L174-L177`), `.aitag` (`L190`), `.audio` (`L191-L193`); `home.html` `.plan`, `.tag`, `.meter` (`L28-L32`), `.langpill` (`L65`); auth pages `.badge`, `.badge-pill`, `.cta`, `.eyebrow`, `.feature`, `.panel`, `.tb-btn`, `.tb-pill`, `.wordmark` (for example `login.html:L334-L360`), repeated in signup, forgot, reset, profile and splash.
- **Why it's a problem:** Noise in files whose lines are already hard to read.
- **Proposed fix:** Delete each item. For CSS, confirm with `rg -w` per class across all pages first, because classes may be added from JS strings.
- **Blast radius:** None expected.
- **Verify before fixing:** `rg -n "\b(tools|cq|dirty|tp|aitag|audio|plan|tag|meter|langpill)\b" *.html assets/*.js` and inspect hits inside template strings.
- **Verify after fixing:** Visual check of Studio (all tabs), Home, and each auth page.
- **Depends on / blocks:** Easier after FE-19 (shared stylesheet).

**Endpoints with no front-end caller** (searched all HTML and `assets/*.js`, including `${API}` template literals and string concatenation): `GET /debug/{id}` and `GET /version` have no caller at all (L-22). `GET /download/{id}`, `GET /srt/{id}` and `POST /combine-clips/` are called only by `editor.html` and `index.html` (FE-17). `GET /system-fonts/{f}` is reached indirectly through URLs returned by `/font-catalog`. `/auth/google` is a plain link; `/auth/google/callback` is the OAuth redirect. `/restyle-from-reference` accepts `letter_spacing` and `line_spacing` (`app.py:L2087-L2088`) but Studio does not send them (`studio.html:L865`), so a restyle resets both to defaults (section 5, Q9). Every Studio form field name matches the backend parameters; no page calls a missing endpoint or the wrong method.

### 2.3 Unnecessary complexity - backend

#### [C-01] Three near-copies of the edit pipeline live inside `app.py`
- **Category:** Complexity
- **Severity:** Medium
- **Confidence:** High
- **Location:** `app.py:L294-L636` (`_run_pipeline`, 343 lines), `app.py:L2155-L2318` (`_run_restyle_from_reference`, a closure of 164 lines), `app.py:L1609-L1639` (`_render_correction`), `app.py:L661-L1073` (`_run_multi_clip_pipeline`, 413 lines)
- **Evidence:** The restyle closure repeats the main pipeline step for step: pause features, edge silences, fillers, reference download and profile, `classify_pauses`, the CUT/COMPRESS/KEEP count print, `_get_media_duration`, `cut_list.edge_cuts`, `get_keep_segments`, `edit_render.render`, `evaluate`, the `decisions_records` projection (`L586-L590` equals `L2271-L2275`), and a 25-key `job_store.update` whose key set must stay identical to `L592-L624` for `/timeline`, `/captions`, `/export` and `/feedback` to work. The differences already caused bugs: L-19 (different fallbacks), L-20 (missing argument), and the correction path builds its job by `{**job}` while the other two enumerate keys.
- **Why it's a problem:** Any new job field or pipeline step must be added in three places and nothing checks that it was. `app.py` mixes HTTP routing, job state, orchestration and file cleanup in 2,526 lines.
- **Proposed fix:** Create `pipeline.py` with:
  1. `@dataclass class Analysis` holding `transcript`, `pause_features`, `leading_cut_end`, `trailing_cut_start`, `filler_cuts`, `lang_fillers`, `video_duration`.
  2. `analyse(input_path, audio_path, language, transcript=None) -> Analysis` (transcribes only when `transcript` is None, so restyle can reuse the cached one).
  3. `reference_profile(link, prefix, want_video) -> (style_profile, ref_video_path | None)` shared by the single-clip, master pre-fetch and restyle paths.
  4. `decide_and_render(analysis, style_profile, style, preset, prefix, output, frame, color_grade, grade_params, on_progress) -> dict` returning exactly the job fields to store (one function builds the 25 keys).
  5. A `Job` `TypedDict` documenting every key.
  `_run_pipeline`, the restyle route and `_render_correction` then become short callers. Move `_run_multi_clip_pipeline` and its aggregation (`L893-L1050`) into `pipeline.py` as `combine_jobs(...)`.
- **Blast radius:** Every job-producing endpoint. Must be behaviour-preserving.
- **Verify before fixing:** Land the section 4 tests first, especially an end-to-end test per entry point that snapshots the set of keys in the finished job.
- **Verify after fixing:** The same tests; diff the job key sets before and after; run all three flows in `studio.html` with the 8-second clips.
- **Depends on / blocks:** After L-07, L-08, L-11, L-19, L-20 (they simplify what is moved). Blocks C-02, C-03.

#### [C-02] Repeated request plumbing in `app.py`
- **Category:** Complexity
- **Severity:** Low
- **Confidence:** High
- **Location and evidence:**
  - Job lookup plus owner check written out 9 times: `app.py:L1355-L1357`, `L1385-L1389`, `L1530-L1534`, `L1577-L1580`, `L1667-L1671`, `L1728-L1732`, `L1819-L1821`, `L1984-L1987`, `L2113-L2117` (and again per id in `L2372-L2378`), with three different 404 messages.
  - The 22 caption `Form(...)` parameters are declared twice with identical defaults: `app.py:L1082-L1104` and `L1838-L1861`; `submit_video` then recovers them with `dict(locals())` (`L1128`), which also captures `video_file`, `user` and everything else in scope.
  - Upload streaming with the size limit is implemented twice: `app.py:L1138-L1159` and `L2046-L2056`.
  - 18 page routes of three lines each: `app.py:L1263-L1338`.
  - The same modules are imported under several names: `subprocess` (`L44`), `_sp` (`L57`, re-imported `L358`), `__import__('subprocess')` (`L1348`); `json` as `_json` (`L23`, `L358`, `L1983`) and plain `json` (`L163`); `time` (`L25`) and `_time` (`L692`, `L2426`); `ThreadPoolExecutor` (`L136`, `L693`).
- **Why it's a problem:** Small inconsistencies creep in (L-21's status codes, the three 404 texts) and each new endpoint copies the boilerplate again.
- **Proposed fix:**
  1. `def _owned_job(job_id: str, user: dict = Depends(auth.get_current_user)) -> dict` raising 404/403; routes take `job: dict = Depends(_owned_job)`.
  2. Split `_caption_fields` into `_style_fields` (the 22 look fields) and the three caption-list fields; `submit_video` takes `style_fields: dict = Depends(_style_fields)` and passes it to `build_style`.
  3. One `async def _save_upload(upload, dest) -> int` at module level used by both endpoints.
  4. Generate page routes from a table: `for path, file in PAGES.items(): app.add_api_route(path, partial(_page, file), include_in_schema=False)`.
  5. One import per module at the top of the file.
- **Blast radius:** Route signatures only; the HTTP contract must not change (same field names and defaults).
- **Verify before fixing:** Snapshot `GET /openapi.json` before the change.
- **Verify after fixing:** Diff `openapi.json` (form field names, types, defaults identical for `/edit-video/`, `/captions/{id}`, `/export/{id}`); click through every page URL in `PAGES`.
- **Depends on / blocks:** After C-01.

#### [C-03] Temp-file cleanup is a hand-maintained list of 13 glob patterns, twice, and already out of date
- **Category:** Complexity
- **Severity:** Medium
- **Confidence:** High
- **Location:** `app.py:L1485-L1497`, `app.py:L1064-L1071`, `app.py:L804-L807`, `app.py:L883-L885`
- **Evidence:** Patterns are typed out per call site. `graded_{req_id}_*` (`L1067`, `L1490`) matches nothing: the file is named `_graded_{prefix}.mp4` (`edit_render.py:L238`). `subtitles_{req_id}*.srt` no longer matches the real caption files (`subtitles_remapped_{req_id}.srt`). `frames_{req_id}/` directories (`frame_extractor.py:L57`) and `ref_video_{req_id}*.mp4` are in no pattern, and `workspace.sweep_files` only deletes plain files (`workspace.py:L46`), so a crash during thumbnail extraction leaves a directory forever.
- **Why it's a problem:** Every new temp file needs edits in up to four lists, and the lists have already drifted, leaking files.
- **Proposed fix:** Every job file already contains the 8-character job prefix. Replace the pattern lists with one function in `workspace.py`:
  ```python
  def remove_job_files(prefix: str, keep: set[str] = frozenset()) -> None:
      for entry in os.scandir(WORK_DIR):
          if prefix in entry.name and entry.name not in keep:
              shutil.rmtree(entry.path, ignore_errors=True) if entry.is_dir() else os.remove(entry.path)
  ```
  Call it from `_full_cleanup` (with `keep=shared`), the multi-clip failure path and the multi-clip success cleanup. Make `sweep_files` handle directories the same way.
- **Blast radius:** Cleanup only. Risk: deleting a file another job still references, so always pass `_referenced_files(exclude_id=...)` as `keep` (corrections and restyles share the raw upload).
- **Verify before fixing:** List every filename template: `rg -n "f\"[_a-z]*_\{(req_id|prefix|job_prefix|mid|tag|sub_req)" *.py` and confirm each contains the prefix.
- **Verify after fixing:** Run a job, a correction on it, then expire the first job (set `CUTSMART_JOB_TTL_HOURS=0.001`): the raw upload must survive while the correction job is live, and `../work` must be empty after both expire.
- **Depends on / blocks:** none

#### [C-04] Media probing is implemented eight times
- **Category:** Complexity
- **Severity:** Medium
- **Confidence:** High
- **Location:** `media_probe.py:L6-L21` (`probe`), `app.py:L160-L173` (`_get_media_duration`), `app.py:L357-L367` (inline ffprobe in `_run_pipeline`), `render_engine.py:L288-L298` (`_probe_duration`), `evaluator.py:L23-L36` (dead, D-02) and `L39-L80` (`_ffprobe_metadata`), `clip_combiner.py:L65-L86` (`get_video_duration`) and `L192-L220` (`_get_common_resolution`), `subtitle_service.py:L447-L456` (`_probe_dims`), `reference_caption_style.py:L35-L45` (`_probe_dims`), `reframe.py:L61-L82` (`probe_video`)
- **Evidence:** Each builds its own `ffprobe` argv, parses JSON or plain output, and has its own failure value (`None`, `0.0`, `60.0`, a passed-in fallback, or an exception). Only `media_probe.probe` sets a timeout.
- **Why it's a problem:** A hung `ffprobe` on a corrupt file blocks a worker forever in seven of the eight. Failure handling differs per call site (the pipeline silently assumes a 60-second video at `app.py:L367`).
- **Proposed fix:** Extend `media_probe.py` to one cached call that returns everything: `info(path) -> MediaInfo | None` with `duration`, `width`, `height`, `fps`, `rotation`, `has_video`, `has_audio`, `codec`, `size_bytes`, using one `ffprobe -show_streams -show_format -of json` with `timeout=30`. Replace each listed function with a thin call (`_get_media_duration(path, fallback)` becomes `(media_probe.info(path) or fallback_info).duration`). Keep `reframe.probe_video` only if it needs rotation handling that the others do not; otherwise fold it in too.
- **Blast radius:** All render, evaluation and combine paths. Pure refactor; keep each call site's existing fallback value.
- **Verify before fixing:** Read each function's return contract and list its callers (`rg -n "_probe_dims|_probe_duration|get_video_duration|_get_media_duration|_ffprobe_metadata|probe_video" *.py`).
- **Verify after fixing:** Unit test `media_probe.info` on the sample clips (landscape, portrait with rotation metadata, audio-only, non-media file). End-to-end edit and multi-clip combine.
- **Depends on / blocks:** Helps L-18.

#### [C-05] Every edit is lossy-encoded three times
- **Category:** Complexity
- **Severity:** Low
- **Confidence:** Medium (design is deliberate in part; gain not measured)
- **Location:** `render_engine.py:L342-L356` (per-segment libx264 encode), `render_engine.py:L380-L391` (concat re-encodes again with libx264 crf 23), `render_engine.py:L648-L649` (caption burn, third encode), plus `edit_render.py:L236-L240` (optional grade, a fourth)
- **Evidence:** The segments are all produced by the same command with the same codec settings, then the concat step uses `-c:v libx264 ... -c:a aac` instead of `-c copy`.
- **Why it's a problem:** Roughly doubles pass-1 time and stacks generation loss at crf 23 before captions are burned.
- **Proposed fix:** Change the concat step to `-c copy` (identical parameters make this valid for the concat demuxer) and keep the measured `real_durations` logic unchanged. Only if captions stay in sync in testing; otherwise leave as is and record why in a comment.
- **Blast radius:** Every render. Caption timing depends on real clip durations, so this is the riskiest low-severity item.
- **Verify before fixing:** Measure on a clip with 20+ cuts: total duration of the concat output with `-c copy` versus the sum of `real_durations`, and audio continuity at the joins.
- **Verify after fixing:** Captions on the last cut of a 3-minute clip are in sync; `tests/check_keyword_overflow.py` still passes.
- **Depends on / blocks:** After D-01 and L-08. Optional.

#### [C-06] `auth.py` boilerplate
- **Category:** Complexity
- **Severity:** Low
- **Confidence:** High
- **Location:** `auth.py:L99-L118` and 20 open/close pairs (for example `forgot_password` opens two connections for two statements, `L596-L613`; `resend_otp` likewise, `L472-L488`), password rules in three places (`L336-L343`, `L368-L375`, `L552-L554`), avatar colour expression twice (`L435`, `L696`), timestamps stored as ISO strings in `TEXT` columns and compared as strings (`L290-L295`)
- **Evidence:** See locations.
- **Why it's a problem:** Each handler is about half connection management; the three password rules already disagree (L-02).
- **Proposed fix:** Done mostly by L-05 (context manager) and L-02 (single password rule). Additionally add `_avatar_color(user_id)`. Leave the `TEXT` timestamps alone: all values are written by `datetime.now(timezone.utc).isoformat()`, so string comparison is consistent, and changing column types needs a data migration for no functional gain.
- **Blast radius:** `auth.py` only.
- **Verify before fixing:** none
- **Verify after fixing:** Auth tests.
- **Depends on / blocks:** L-05, L-02

#### [C-07] Multi-clip evaluation aggregation re-implements evaluator logic inline
- **Category:** Complexity
- **Severity:** Low
- **Confidence:** High
- **Location:** `app.py:L944-L1018` (aggregation, with a local `_score_to_grade` at `L984-L990` duplicating `evaluator.py:L430-L436`), `app.py:L927` and `L939` (loop variable `frame` shadows the `frame` parameter of the function)
- **Evidence:** 75 lines in the orchestrator rebuild a reduced evaluation dict by hand from per-clip reports.
- **Why it's a problem:** The combined report silently diverges from the single-clip schema whenever `evaluate()` changes; the shadowed parameter is a trap for anyone who later uses `frame` after the loop.
- **Proposed fix:** Move the block to `evaluator.aggregate(reports: list[dict], original_duration: float) -> dict` reusing `evaluator._score_to_grade`; rename the loop variable to `thumb`.
- **Blast radius:** Master job `evaluation` (must be identical).
- **Verify before fixing:** Capture a master job's `evaluation` via `/report/{id}`.
- **Verify after fixing:** Same input gives the same dict; unit test `aggregate` with two fixture reports.
- **Depends on / blocks:** Natural part of C-01.

### 2.3b Unnecessary complexity - front-end

#### [FE-19] Session, fetch, toast, escaping and password logic are copied into every page
- **Category:** Complexity
- **Severity:** Medium
- **Confidence:** High
- **Location:**
  - API base constant `const API = location.origin` in 9 files; Studio also mixes it with bare paths without `credentials` (`studio.html:L975`, `L996`).
  - Auth gate: `home.html:L173-L179`, `studio.html:L354-L357`, `profile.html:L617-L636`, `editor.html:L3025-L3041`; inverse gate `login.html:L430-L435`, `signup.html:L526-L531`.
  - Logout: `home.html:L180`, `studio.html:L352`, `profile.html:L741-L744`, `editor.html:L3064-L3067`.
  - Toast: `studio.html:L351`, `profile.html:L611-L615`, `editor.html:L4453-L4460`.
  - HTML escape: `home.html:L184` and `studio.html:L738` are two different implementations (neither escapes `'`), plus an ad hoc `.replace(/</g,'&lt;')` at `studio.html:L642`.
  - Password rule: `signup.html:L538`, `L555`; `reset_password.html:L380`, `L392`; `profile.html:L703`; server `auth.py:L336-L343`, `L368-L375`.
  - Thumbnail capture: `home.html:L210-L214` and `studio.html:L727-L731`.
  - Auth page stylesheet: of 214 unique CSS lines in `login.html`, 207 recur in `signup.html`, 166 in `forgot_password.html`, 158 in `reset_password.html`, 131 in `profile.html`.
- **Evidence:** See locations.
- **Why it's a problem:** FE-01, FE-03, FE-06, FE-08, FE-09 and FE-10 are each "one page got it right, another did not". A fix has to be found and repeated per page.
- **Proposed fix:** Add to `assets/` (already served at `/assets`): `api.js` (fetch wrapper that always sends credentials, checks `r.ok`, exposes `detailText`, redirects on 401), `session.js` (`requireUser`, `logout`, `safeNext`), `ui.js` (`toast`, `esc`), `password.js` (one rule matching `auth.py`), `auth.css`. Migrate one page per commit, starting with `login.html` and `splash.html` because of FE-01.
- **Blast radius:** Every page. No behaviour change intended beyond the listed bug fixes.
- **Verify before fixing:** Pages are served with `Cache-Control: no-cache` but `/assets` is a plain `StaticFiles` mount: decide on cache busting (query string version) before moving logic there.
- **Verify after fixing:** Manual pass through signup, verify, login, forgot, reset, profile change, logout, Home, Studio.
- **Depends on / blocks:** Provides the helpers for FE-01, FE-06, FE-08, FE-10.

#### [FE-20] `studio.html` is one 684-line global script with about 25 mutable flags
- **Category:** Complexity
- **Severity:** Medium
- **Confidence:** High
- **Location:** `studio.html:L330-L1013` (about 75 KB of inline script, most lines 200 to 900 characters); `renderR()` `L526-L571`; `markDirty` `L602-L604`
- **Evidence:** Globals include `JOB`, `BUSY`, `RESULT_BLOB`, `CAP_EDITED`, `CAP_SEQ`, `FRAME_DIRTY`, `PIPE_SIG`, `LAST_WPC`. `renderR()` rebuilds the whole right panel as one template string with inline handlers. `markDirty` fires twice per input (inline `touch()` plus the delegated listeners at `L603-L604`), and each call runs `saveProject()`, which rewrites the whole project list including base64 thumbnails to localStorage on every slider tick.
- **Why it's a problem:** FE-02, FE-04 and FE-05 are direct consequences of implicit shared state; the file cannot be reviewed line by line or unit tested.
- **Proposed fix:** Split into ES modules under `assets/studio/` (player, framing, style panel, job lifecycle, caption editor, timeline, cuts, library). Replace the flag set with one explicit job state (`idle | uploading | processing | ready | error`) owned by the job-lifecycle module. Debounce `saveProject` (about 300 ms) and store thumbnails outside the list that is rewritten.
- **Blast radius:** The whole Studio page.
- **Verify before fixing:** Fix FE-02 to FE-05 first as small patches, then write a manual regression checklist (or Playwright script) covering generate, re-edit, restore, remove section, restyle, caption edit, export.
- **Verify after fixing:** The same checklist.
- **Depends on / blocks:** After FE-02 to FE-07, FE-12, FE-19.

---

## 3. Remediation plan

Each batch is shippable and testable on its own. Inside a batch, apply findings in the order listed.

### Batch 0 - Make the work recoverable (no code changes)
1. Review `git status`, then commit the current working tree as a baseline: the modified files, everything `git status` lists as untracked (`edit_render.py`, `cut_list.py`, `workspace.py`, `media_probe.py`, `font_catalog.py`, `caption_preview.py`, `reframe.py`, `requirements.txt`, `assets/`, `images/`, `models/`), and the deletion of `feedback_service.py`. Do not commit `AUDIT_REPORT.md` into the same commit if you want a clean baseline.
2. Save `curl localhost:8000/openapi.json > /tmp/openapi.before.json` for later diffing (C-02, FE-17).
3. Confirm the two existing checks pass: `python tests/check_keyword_overflow.py` and `python locked_templates/check_locked_templates.py` (run from `src_v4`).

### Batch 1 - Account security (Critical and High)
Order: **L-05** (connection helper; every later auth fix touches the same lines) → **L-04** → **L-03** → **L-01** → **L-02** together with the `profile.html` change (its `PUT /auth/me` calls are at `profile.html:L645` and `L710`) → **FE-01** → **L-13**.
Write the auth tests from section 4 alongside each fix; they need only Postgres and `DEV_MODE=1`.
- Needs configuration: new env var `APP_BASE_URL` (L-03); `JWT_SECRET` becomes mandatory (L-04).
- Needs migration: `email_verifications.attempts` column (L-13), via `ADD COLUMN IF NOT EXISTS` in `init_db`.
- Needs coordination: L-02 changes the `PUT /auth/me` contract; ship backend and `profile.html` together.

### Batch 2 - Remaining auth hardening and template storage
**L-14** → **L-15** → **L-12** → **FE-06** → **FE-07** (client and server validation) → **L-22**.
- Needs migration: `users.token_version` column (L-15); new `custom_templates` table plus one-time import of any existing `custom_templates.json` (L-12).
- L-14 needs the decision in Q3 for the targeted-lockout part; the "reset on success" part does not.

### Batch 3 - Pipeline tests, then pipeline correctness
First add the pure-function and end-to-end tests listed in section 4. Then:
**L-06** → **L-16** → **L-09** → **L-10** → **L-11** → **D-01** (delete the dead render copy before editing the live one) → **L-08** → **L-17** → **L-19** → **L-20** → **L-21**.
- **L-07** goes here once Q1 is answered. It changes edit output on purpose, so ship it on its own and say so in the release note.
- **L-18** only after reproducing it (its "verify before fixing").

### Batch 4 - Studio and page behaviour
**FE-03** → **FE-02** → **FE-04** → **FE-05** → **FE-12** → **FE-11** → **FE-13** → **FE-08** → **FE-09** → **FE-10** → **FE-14** → **FE-15**.
FE-02, FE-03 and FE-04 all edit the same three functions (`poll`, `markDirty`, `loadCaptions`); do them in one sitting and test together.

### Batch 5 - Dead code removal
**FE-17** step 1 (`index.html`) immediately; `editor.html` and its endpoints only after Q2 (if kept, do **FE-16** instead) → **D-02** → **D-03** → **D-04** → **D-05** → **D-06** → **FE-18**.
Run pyflakes and both caption regression scripts after each of D-02 and D-03.

### Batch 6 - Refactors
**C-03** → **C-04** → **C-06** → **C-07** → **C-01** → **C-02** → **FE-19** → **FE-20** → **C-05** (optional, measure first).
C-01 and FE-20 are the two large ones; do not start either without the tests from section 4 in place.

### Dependency summary
- L-05 blocks L-01, L-02, L-12, L-13, L-14, L-15, C-06.
- L-03 provides `APP_BASE_URL`, which L-15 uses for the `Secure` flag.
- L-02 and FE-01 together close the takeover chain; neither alone does.
- D-01 before L-08 and C-05. L-07 before L-20.
- FE-17 (Q2) decides FE-16 and whether `/download`, `/srt`, `/combine-clips/` are in scope for L-16, L-18, L-21.
- FE-19 supplies helpers used by FE-01, FE-06, FE-08, FE-10; in Batch 1 create only `assets/nav.js` (or `session.js`) with `safeNext` and grow it later.
- C-01 after L-07, L-08, L-11, L-19, L-20; C-02 after C-01.

### Leave alone
- **`subtitle_service.py` caption generators** (`create_remapped_pop_ass` is 380 lines, `L459-L838`; box and soft generators `L855-L1100`). Oversized, but two of them are frozen by byte-exact golden files in `locked_templates/` and the third by `tests/check_keyword_overflow.py`. Apart from the D-03 leftovers, refactoring here risks visible caption regressions for little gain.
- **`_SSA_TO_NUMPAD` and the 6/10/2 alignment numbers** (`subtitle_service.py:L1168`, `render_engine.py:L113-L116`). Looks wrong, is the legacy SSA numbering that ffmpeg's `force_style` expects.
- **In-memory `job_store`**. Losing jobs on restart is a documented trade-off (`app.py:L115-L117`); replacing it with a database is a project, not a fix. Do the L-17 hardening only.
- **`workspace.enter()` changing the process working directory**. Unusual, but ffmpeg filter strings rely on bare relative file names (`workspace.py:L3-L6`); undoing it means escaping paths in every filter.
- **`_WHISPER_LOCK` and model load at import** (`transcription_service.py:L21-L24`). Correct for a shared model.
- **ISO-string timestamps in `TEXT` columns** (`auth.py`). Consistent as written; see C-06.
- **Vendored JASSUB files in `assets/`, `fonts/`, `fonts_variable_src/`, `models/`**. Third-party or binary assets.
- **CORS allow-list** (`app.py:L86-L97`). Same-origin app; the explicit localhost list is fine.
- **`dl_fonts.py`, `make_static_fonts.py`**. One-off build scripts, not dead; document them (D-06).

## 4. Test gaps

Current state: there is no test runner, no `pytest` dependency and no CI. The only automated checks are two standalone scripts for caption rendering (`tests/check_keyword_overflow.py`, `locked_templates/check_locked_templates.py`). Auth, the edit pipeline, the job lifecycle and every endpoint have no coverage. Add `pytest` and `httpx` (for FastAPI's `TestClient`) to a dev requirements section.

Note for whoever sets this up: importing `app` loads Whisper, connects to Postgres and changes the working directory. Tests of pure modules must import those modules directly, not `app`.

### Add before Batch 1 (auth)
Run against a throwaway Postgres database with `DEV_MODE=1`, capturing the OTP and reset link from stdout or by monkeypatching `email_service`.
1. Signup → verify → login → `/auth/me` → logout round trip.
2. Re-signup before verification replaces the password (L-01) and the Google-link case nulls it.
3. `PUT /auth/me` with `password` and no or wrong `current_password` is rejected (L-02).
4. Forgot-password link host ignores the `Host` header (L-03); token is single use and expires.
5. Wrong OTP five times invalidates the code (L-13).
6. Lockout: 5 failures lock, a success resets (L-14).
7. A job endpoint returns 403 for another user's job id and 401 without a cookie (`_require_owner`).
8. Thirty concurrent signups for one email leave the pool usable (L-05).

### Add before Batch 3 (pipeline), pure functions, no ffmpeg needed
9. `cut_list`: `normalize` (overlap, touching, clamping), `subtract`, `apply_correction` (KEEP, CUT, out of range, NaN, empties the video), `removed`, `label_timeline` (tie-breaking).
10. `render_engine.get_keep_segments`: no cuts; CUT; COMPRESS keeps `compress_target` seconds; filler overlapping a pause; slivers under `MIN_KEEP_S`; edge silences.
11. `decision_engine.classify_pauses` and `compress_target` for the three presets (the `__main__` block at `decision_engine.py:L165-L196` is a ready-made fixture).
12. `filler_remover.detect_fillers`: merging within 0.15 s, punctuation stripping, and the L-07 cases.
13. `edit_render.build_style` (preset vs custom, clamping, bad numbers), `transcript_from_captions`, `edited_transcript` (word inside a cut, word spanning a boundary, `real_durations` offsets).
14. `style_extractor.calculate_style_profile` with a synthetic pause table (threshold clamps, `compress < max` invariant).
15. `comment_parser.parse_command`: one case per supported command form plus ambiguous and out-of-range inputs.
16. `workspace.sweep_files` and `app._sweep_expired_jobs` with a temp directory and a fake clock: shared raw upload survives while a correction job is live (C-03).

### Add before Batch 3, end to end with ffmpeg (use the 8-second sample clips)
17. `POST /edit-video/` single clip → poll → `/timeline`, `/report`, `/intermediate`, `/captions`, `/export` → file plays; snapshot the set of keys in the finished job (guards C-01).
18. `POST /feedback/` KEEP and CUT on that job; captions still aligned at the last cut.
19. Two-clip upload → combined job; one clip forced to fail → master reports `error` (L-10).
20. Upload rejected paths: non-video, no audio, oversize, odd file name (L-16), rotate 90.
21. `extract_and_concat_segments` with a stubbed `subprocess.run` that fails one clip (L-08).
Whisper and yt-dlp should be stubbed in these tests (`transcribe_audio` returning a fixture transcript, `download_youtube_audio` returning a local wav) so they run offline in seconds.

### Add before Batch 4 and FE-20 (front-end)
22. A browser script (Playwright or a written manual checklist) for Studio: generate, change style during processing (FE-02), re-edit, restore a cut, remove a section, edit a caption while a style request is in flight (FE-04), export, reopen the project after a reload (FE-05).
23. `safeNext` unit cases: `/home.html`, `/studio.html?p=1`, `//evil`, `/\evil`, `https://evil`, `javascript:alert(1)`, empty (FE-01).

## 5. Open questions

- **Q1 (L-07): Which words should filler removal cut?** Only hesitation sounds, or also discourse words such as "like", "so", "right", "actually" and the Urdu/Hindi conjunctions in the current lists? Should the user be able to turn filler removal off or review it? The current behaviour removes all of them everywhere.
- **Q2 (FE-17, FE-16): Is `editor.html` retired?** No page links to it. If yes, `/download/{id}`, `/srt/{id}` and `/combine-clips/` lose their only caller and can be removed too. If no, it needs a link from Studio or Home and the FE-16 fix.
- **Q3 (L-14): Lockout policy.** Per-email lockout lets anyone lock a known account. Acceptable for this project, or should it be per email plus IP, or replaced by a delay/CAPTCHA?
- **Q4: Will this run anywhere other than `http://localhost:8000`?** The severity of L-03, L-04 and the cookie `Secure` flag assume it might. `font_catalog.py:L9-L10` also notes that serving OS fonts (Arial, Impact, Georgia) needs web-embedding rights on a public deployment.
- **Q5 (D-03): Do databases created before the `email_verified` and `google_id` columns still exist?** If not, the `ALTER TABLE` loop at `auth.py:L139-L148` can go.
- **Q6 (D-06): Is `httpx2==2.16.2` the intended dependency?** Both manifests list `httpx2`, it is what is installed, and plain `httpx` is not importable in `../.venv`. I could not confirm which package authlib's HTTP client is meant to use here or that `httpx2` is the publisher you expect. Check its PyPI page and owner before the next clean install.
- **Q7 (restyle): Colour grade after matching a new reference.** `/restyle-from-reference` reuses the job's old `grade_params` (`app.py:L2244`, `L2302`). With `color_grade=match_reference`, should the grade follow the new reference instead?
- **Q8 (FE-18): Top-positioned captions.** `studio.html:L611-L612` tests `al===8`, but `al` is only ever 6, 10 or 2, so top captions always send `subtitle_margin_v=0`. With the `custom` preset the backend positions by `pos_y` and the margin may be irrelevant. Confirm against a legacy preset with position "top" whether this is dead code or a wrong margin.
- **Q9 (restyle): Letter and line spacing.** The endpoint accepts `letter_spacing` and `line_spacing` but Studio does not send them, so a restyle resets both to defaults (`app.py:L2215-L2217` overwrite the job's stored values with the form defaults). Should a restyle keep the current spacing? If yes, default the two form parameters to `None` and only override when sent.
- **Q10 (restyle on combined videos).** `/feedback/` rejects multi-clip masters with a clear 400 (`app.py:L1583-L1585`); `/restyle-from-reference` instead returns 409 "The original upload for this video has expired" (`app.py:L2131-L2135`) because a master has no `input_video_path`. Should it get the same explicit message, or should restyle be supported for combined videos?
- **Q11 (L-17): Should exports queue behind running edits?** Moving exports onto the shared 2-worker executor bounds resource use but means an export waits while two edits run. The alternative is a separate small export pool.
- **Q12 (L-10): Maximum clips per multi-clip upload and how many may run at once?** The report suggests 10 and 3; pick values that fit the demo machine.
