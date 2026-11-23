"""Job workspace: where uploads, intermediates and renders live, and for how long.

The pipeline names its files with bare relative paths (``raw_<id>_x.mp4`` ...), and
ffmpeg filter strings depend on that. Rather than threading a directory through
every call site, the server process works *inside* ``WORK_DIR``; only the page
routes need to reach back into the source tree, via ``page()``.

Files are never deleted when they are downloaded (a user may download again,
restore a cut, or re-match a reference later). Instead everything older than
``TTL_SECONDS`` is swept on a timer.
"""
import os
import re
import shutil
import sys
import threading
import time

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.abspath(os.environ.get(
    "CUTSMART_WORK_DIR", os.path.join(SOURCE_DIR, os.pardir, "work")))
TTL_SECONDS = float(os.environ.get("CUTSMART_JOB_TTL_HOURS", "24")) * 3600
SWEEP_INTERVAL_SECONDS = float(os.environ.get("CUTSMART_SWEEP_INTERVAL_SECONDS", "600"))


def enter() -> None:
    """Create WORK_DIR and make it the process working directory."""
    os.makedirs(WORK_DIR, exist_ok=True)
    if SOURCE_DIR not in sys.path:      # lazy `import x` of local modules must still resolve
        sys.path.insert(0, SOURCE_DIR)
    os.chdir(WORK_DIR)


def page(name: str) -> str:
    """Absolute path of a file shipped with the source (HTML pages)."""
    return os.path.join(SOURCE_DIR, name)


def is_expired(timestamp: float, now: float | None = None) -> bool:
    return ((now or time.time()) - timestamp) > TTL_SECONDS


# Every file a job makes is named <letters and underscores><8-hex job prefix><. or _ or end>
_JOB_NAME = re.compile(r"[A-Za-z_]*?([0-9a-f]{8})(?:[._]|$)")


def job_prefix_of(name: str) -> str | None:
    """The job prefix a work-dir name belongs to; None for anything that isn't a job's."""
    match = _JOB_NAME.match(name)
    return match.group(1) if match else None


def _delete(entry: os.DirEntry) -> None:
    if entry.is_dir(follow_symlinks=False):
        shutil.rmtree(entry.path)
    else:
        os.remove(entry.path)


def remove_job_files(prefix: str, keep=frozenset()) -> None:
    """Delete everything in WORK_DIR made by the job with this prefix, except the names in ``keep``."""
    if not re.fullmatch("[0-9a-f]{8}", prefix):
        raise ValueError(f"not a job prefix: {prefix!r}")
    for entry in list(os.scandir(WORK_DIR)):
        if entry.name not in keep and job_prefix_of(entry.name) == prefix:
            try:
                _delete(entry)
            except OSError as exc:
                print(f"   ⚠️  Could not delete {entry.name}: {exc}")


def sweep_files(now: float | None = None, keep=lambda name: False) -> list[str]:
    """Delete expired files and job folders in WORK_DIR, except those ``keep(name)`` protects.

    Folders that aren't a job's are never touched: WORK_DIR may be a directory someone chose."""
    removed = []
    for entry in list(os.scandir(WORK_DIR)):
        try:
            ours = entry.is_file() or (entry.is_dir(follow_symlinks=False) and job_prefix_of(entry.name))
            if ours and not keep(entry.name) and is_expired(entry.stat().st_mtime, now):
                _delete(entry)
                removed.append(entry.name)
        except OSError:
            pass
    return removed


def run_periodically(fn, interval: float = SWEEP_INTERVAL_SECONDS) -> threading.Thread:
    """Call ``fn()`` now and then every ``interval`` seconds on a daemon thread."""
    def loop():
        while True:
            try:
                fn()
            except Exception as exc:  # a failed sweep must never kill the server
                print(f"   ⚠️  Workspace sweep failed: {exc}")
            time.sleep(interval)
    t = threading.Thread(target=loop, name="workspace-sweeper", daemon=True)
    t.start()
    return t
