"""Test harness for CutSmart.

    TEST_DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/postgres python -m pytest tests

The URL must be a LOOPBACK Postgres whose role can create databases. Every run gets
its own database and work directory, and ../.env can never leak in: each setting the
app reads is set below, before any project module can be imported (some read theirs at
import, and python-dotenv does not override existing variables).
Whisper, email and the network are stubbed, so tests run offline.
"""
import os
import sys
import tempfile
import types
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

# Must stay above every project import: workspace fixes its folder when first imported.
ISOLATED = {
    "CUTSMART_WORK_DIR": tempfile.mkdtemp(prefix="cutsmart_test_work_"),
    "CUTSMART_JOB_TTL_HOURS": "24", "CUTSMART_SWEEP_INTERVAL_SECONDS": "600",
    "DATABASE_URL": "postgresql://tests-only@127.0.0.1:9/none",     # replaced by the run's own database
    "JWT_SECRET": "test-secret-" + "x" * 32, "DEV_MODE": "1",
    "APP_BASE_URL": "http://localhost:8000",
    "GOOGLE_REDIRECT_URI": "http://localhost:8000/auth/google/callback",
    "GOOGLE_CLIENT_ID": "test-client", "GOOGLE_CLIENT_SECRET": "test-secret",
    "RESEND_API_KEY": "", "RESEND_FROM": "CutSmart <tests@example.com>",
    "SMTP_USER": "", "SMTP_PASSWORD": "", "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "9", "SMTP_FROM": "tests@example.com",
}
os.environ.update(ISOLATED)

PASSWORD = "Passw0rd1"
WORDS = [("Hello", .5, .9), ("there", .95, 1.3), ("everyone.", 1.35, 1.9),
         ("This", 3.9, 4.2), ("is", 4.25, 4.4), ("CutSmart.", 4.45, 5.2)]


def fake_transcript(words=WORDS):
    return {"language": "en", "segments": [{
        "start": words[0][1], "end": words[-1][2], "text": " ".join(w[0] for w in words),
        "words": [{"word": " " + w, "start": s, "end": e} for w, s, e in words]}]}


class FakeWhisper:
    def transcribe(self, *args, **kwargs):
        return fake_transcript()


sys.modules["whisper"] = types.SimpleNamespace(load_model=lambda *a, **k: FakeWhisper())


@pytest.fixture(scope="session")
def database_url():
    admin = os.environ.get("TEST_DATABASE_URL")
    if not admin:
        pytest.skip("TEST_DATABASE_URL is not set")
    parts = urlsplit(admin)
    if parts.hostname not in ("127.0.0.1", "localhost", "::1") or "host=" in parts.query:
        pytest.exit("TEST_DATABASE_URL must point at a loopback Postgres", returncode=2)
    import psycopg2
    name = "cutsmart_test_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f'CREATE DATABASE "{name}"')
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'anon'")
    if not cur.fetchone():
        cur.execute("CREATE ROLE anon NOLOGIN")
    url = urlunsplit(parts._replace(path="/" + name))
    # Behave like Supabase: every new table is granted to the public `anon` role
    setup = psycopg2.connect(url)
    with setup, setup.cursor() as c:
        c.execute("GRANT USAGE ON SCHEMA public TO anon")
        c.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon")
    setup.close()
    yield url
    cur.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    conn.close()


@pytest.fixture(scope="session")
def app_module(database_url):
    os.environ["DATABASE_URL"] = database_url
    try:
        import templates
    except ImportError:
        pass
    else:   # the import step renames the file it reads: never let a test see the real one
        templates.LEGACY_PATH = os.path.join(os.environ["CUTSMART_WORK_DIR"], "custom_templates.json")
    import app
    if hasattr(app, "CUSTOM_TEMPLATES_PATH"):
        app.CUSTOM_TEMPLATES_PATH = os.path.join(os.environ["CUTSMART_WORK_DIR"], "custom_templates.json")
    return app


@pytest.fixture
def sql(database_url):
    import psycopg2
    conn = psycopg2.connect(database_url)
    conn.autocommit = True

    def run(query, params=None):
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else []
    yield run
    conn.close()


@pytest.fixture
def mail(app_module, monkeypatch):
    sent = {"otp": {}, "reset": {}, "count": 0}

    def otp(to, code):
        sent["otp"][to] = code
        sent["count"] += 1
        return True

    def reset(to, token, base):
        sent["reset"][to] = (token, base)
        return True

    monkeypatch.setattr(app_module.auth.email_service, "send_otp_email", otp)
    monkeypatch.setattr(app_module.auth.email_service, "send_reset_email", reset)
    return sent


@pytest.fixture
def new_client(app_module):
    from fastapi.testclient import TestClient

    def make(ip="10.0.0.1"):
        return TestClient(app_module.app, base_url="http://localhost:8000",
                          client=(ip, 50000), raise_server_exceptions=False)
    return make


@pytest.fixture
def email():
    return f"u{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def make_user(new_client, mail):
    def make(password=PASSWORD):
        address = f"u{uuid.uuid4().hex[:12]}@example.com"
        c = new_client()
        r = c.post("/auth/signup", json={"name": "Test User", "email": address, "password": password})
        assert r.status_code == 200, r.text
        r = c.post("/auth/verify-email", json={"email": address, "otp": mail["otp"][address]})
        assert r.status_code == 200, r.text
        return c, r.json()["user"]
    return make
