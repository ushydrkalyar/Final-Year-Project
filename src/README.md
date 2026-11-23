# CutSmart

## Run

From this directory:

```sh
python -m pip install -r requirements.txt
python -m uvicorn app:app --port 8000
```

Needs ffmpeg with libass (Homebrew: `ffmpeg-full`) and Tesseract for caption-style OCR.
Job files go to `../work` and expire after 24 hours (`CUTSMART_JOB_TTL_HOURS`).

Settings, read from `../.env`:

| Name | |
|---|---|
| `DATABASE_URL` | Postgres. Required. Tables and columns are created at startup. |
| `JWT_SECRET` | Long random value. Required: the server will not start without it. |
| `APP_BASE_URL` | Public origin for emailed links. Default `http://localhost:8000`; use `https://` in production so cookies are `Secure`. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google sign-in. |
| `GOOGLE_REDIRECT_URI` | Must match the one registered with Google. Default `APP_BASE_URL` + `/auth/google/callback`. |
| `DEV_MODE` | `1` prints email codes to the console and enables `/debug/{job}`. Never set it in production. |
| `RESEND_API_KEY` | Email delivery. |

## How an edit works

- A project is an ordered list of clips (up to 10). `/edit-video/` takes the list; a clip is a new
  file or a finished clip to build on, and only changed clips are made again (`project_plan.py`).
- A cut is frame-exact pieces of at most 6 seconds (`segment_render.py`). A timestamp correction
  re-encodes only the pieces it changes; transitions re-encode only the footage under them.
- Edits are caption-free. Studio draws captions live from `/captions/{job}`; `/export/{job}` burns them.
- A finished job is never modified, so Undo in Studio just shows the job from before.
- Studio (`studio.html`, `assets/studio/`) is the only editor. Models have no DOM; `main.js` does the wiring.

## Test

Tests need a local Postgres role that can create databases and roles. They refuse any
non-loopback host and never use `../.env`.

```sh
TEST_DATABASE_URL=postgresql://user:password@127.0.0.1:5432/postgres python -m pytest tests
python tests/check_keyword_overflow.py
python locked_templates/check_locked_templates.py
```

## Scripts

- `python dl_fonts.py fonts`: download the bundled Google fonts.
- `python make_static_fonts.py fonts`: turn variable fonts into static files and rebuild `fonts/fonts.json`.

## Assets

`/assets` is always revalidated. The `?v=` on each tag exists for copies browsers cached before
that: bump it if a change must reach a browser that could hold such a copy.
