"""The Postgres connection pool, and the one way to use it: ``with db.transaction() as cur``.

The block commits when it ends and rolls back if it raises; the connection always
returns to the pool. Never nest two transactions (each holds a pool slot), and raise
HTTP errors after the block when the writes made inside it must survive.
"""
import os
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Set it to your Postgres connection string.")

MAX_CONNECTIONS = 10
WAIT_SECONDS = 30

try:
    _pool = psycopg2.pool.ThreadedConnectionPool(1, MAX_CONNECTIONS, dsn=DATABASE_URL)
except psycopg2.Error as exc:
    # The host only: the URL carries the password.
    print(f"\n❌ Could not connect to Postgres at {urlsplit(DATABASE_URL).hostname}: {exc}")
    print("   Check DATABASE_URL in ../.env and that the database is reachable.\n")
    raise

# ThreadedConnectionPool raises as soon as it is exhausted; this makes callers queue instead.
_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)


@contextmanager
def transaction():
    """A cursor (rows are dicts) inside one transaction."""
    if not _slots.acquire(timeout=WAIT_SECONDS):
        raise RuntimeError("Timed out waiting for a database connection")
    conn = None
    try:
        conn = _pool.getconn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except BaseException:
        if conn is not None and not conn.closed:
            try:
                conn.rollback()
            except psycopg2.Error:
                conn.close()
        raise
    finally:
        try:
            if conn is not None:
                _pool.putconn(conn, close=bool(conn.closed))   # never re-pool a dead connection
        finally:
            _slots.release()


def enable_rls(cur, table: str) -> None:
    """Hide ``table`` from non-owner roles.

    Supabase grants every new table in ``public`` to its public `anon` API role;
    row-level security with no policies is what keeps that role out. The owner
    (this app) is unaffected. Skipped when already on: ALTER takes an exclusive lock.
    """
    cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(%s)", (table,))
    row = cur.fetchone()
    if row and not row["relrowsecurity"]:
        cur.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
