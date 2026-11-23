"""Caption templates that users made from reference videos: one row each, private to the owner."""
import json
import os
import time
from datetime import datetime, timezone

import db

LEGACY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_templates.json")
MAX_PER_USER = 100


class LimitReached(Exception):
    pass


def init_db() -> None:
    with db.transaction() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS custom_templates (
                key        TEXT PRIMARY KEY,
                owner_id   TEXT NOT NULL,
                label      TEXT NOT NULL,
                config     JSONB NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS custom_templates_owner ON custom_templates (owner_id)")
        db.enable_rls(cur, "custom_templates")      # in the same transaction: never readable by `anon`
    import_legacy(LEGACY_PATH)


def import_legacy(path: str) -> None:
    """Copy the old JSON file into the table, then set it aside. A file we can't read is left as it is."""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            rows = [(key, t["owner_id"], str(t.get("label") or "Reference Style"),
                     json.dumps({k: v for k, v in t.items() if k not in ("owner_id", "label")}))
                    for key, t in json.load(f).items() if t.get("owner_id")]
    except (OSError, ValueError, AttributeError) as exc:
        print(f"⚠️  Could not read {path} ({exc}); left untouched, not imported")
        return
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as cur:
        for row in rows:
            cur.execute("INSERT INTO custom_templates (key, owner_id, label, config, created_at) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING", (*row, now))
    done = path + ".migrated"
    os.replace(path, f"{done}.{time.time_ns()}" if os.path.exists(done) else done)


def list_for(owner_id: str) -> dict:
    with db.transaction() as cur:
        cur.execute("SELECT key, label, config FROM custom_templates WHERE owner_id = %s ORDER BY created_at",
                    (owner_id,))
        return {r["key"]: {"label": r["label"], **r["config"]} for r in cur.fetchall()}


def create(key: str, owner_id: str, label: str, config: dict) -> None:
    with db.transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"templates:{owner_id}",))
        cur.execute("SELECT COUNT(*) AS n FROM custom_templates WHERE owner_id = %s", (owner_id,))
        if cur.fetchone()["n"] >= MAX_PER_USER:
            raise LimitReached()
        cur.execute("INSERT INTO custom_templates (key, owner_id, label, config, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (key, owner_id, label, json.dumps(config), datetime.now(timezone.utc).isoformat()))


def delete(key: str, owner_id: str) -> bool:
    with db.transaction() as cur:
        cur.execute("DELETE FROM custom_templates WHERE key = %s AND owner_id = %s RETURNING key", (key, owner_id))
        return cur.fetchone() is not None
