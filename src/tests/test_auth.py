"""Account security. Each test is named for the audit finding it guards."""
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from conftest import PASSWORD, SRC

ATTACKER, VICTIM = "Attacker123", "Victim12345"


def signup(client, email, password=PASSWORD, name="Test User"):
    return client.post("/auth/signup", json={"name": name, "email": email, "password": password})


def verify(client, email, otp):
    return client.post("/auth/verify-email", json={"email": email, "otp": otp})


def login(client, email, password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def age_code(sql, email, seconds=61):
    for id_, expires in sql("SELECT id, expires_at FROM email_verifications WHERE email = %s", (email,)):
        sql("UPDATE email_verifications SET expires_at = %s WHERE id = %s",
            ((datetime.fromisoformat(expires) - timedelta(seconds=seconds)).isoformat(), id_))


def wrong(otp):
    return "100000" if otp != "100000" else "100001"


def test_round_trip(make_user, new_client):
    client, user = make_user()
    assert client.get("/auth/me").json()["user"]["email"] == user["email"]
    assert login(new_client(), user["email"]).status_code == 200
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401


# ---- L-01: pre-hijack ----
def test_L01_victim_signing_up_after_attacker_owns_the_password(new_client, mail, sql, email):
    signup(new_client(), email, ATTACKER)
    age_code(sql, email)
    victim = new_client()
    signup(victim, email, VICTIM)
    assert verify(victim, email, mail["otp"][email]).status_code == 200
    assert login(new_client(), email, ATTACKER).status_code == 401
    assert login(new_client(), email, VICTIM).status_code == 200


def test_L01_attacker_signing_up_after_victim_cannot_plant_a_password(new_client, mail, sql, email):
    victim = new_client()
    signup(victim, email, VICTIM)
    age_code(sql, email)
    signup(new_client(), email, ATTACKER)          # the fresh code lands in the victim's inbox
    verify(victim, email, mail["otp"][email])      # and the victim types it in
    assert login(new_client(), email, ATTACKER).status_code != 200


def test_L01_code_is_useless_without_proof_of_the_password(new_client, mail, email):
    signup(new_client(), email, ATTACKER)
    assert verify(new_client(), email, mail["otp"][email]).status_code == 400


def test_L01_login_lets_the_owner_verify_on_another_device(new_client, mail, email):
    signup(new_client(), email)
    other = new_client()
    assert login(other, email).status_code == 403
    assert verify(other, email, mail["otp"][email]).status_code == 200


@pytest.fixture
def google(app_module, monkeypatch, new_client):
    def callback(**userinfo):
        async def token(request):
            return {"userinfo": userinfo}
        monkeypatch.setattr(app_module.auth.oauth.google, "authorize_access_token", token)
        return new_client().get("/auth/google/callback", follow_redirects=False)
    return callback


def test_L01_google_link_drops_an_unverified_password(new_client, mail, sql, email, google):
    signup(new_client(), email, ATTACKER)
    r = google(sub=uuid.uuid4().hex, email=email, email_verified=True, name="Owner")
    assert "splash" in r.headers["location"]
    assert sql("SELECT password_hash, name FROM users WHERE email = %s", (email,)) == [(None, "Owner")]
    assert login(new_client(), email, ATTACKER).status_code == 401


def test_L01_google_link_keeps_a_verified_password(make_user, new_client, google):
    _, user = make_user()
    google(sub=uuid.uuid4().hex, email=user["email"], email_verified=True, name="Owner")
    assert login(new_client(), user["email"]).status_code == 200


@pytest.mark.parametrize("extra", [{"email_verified": False}, {}, {"email": None, "email_verified": True}])
def test_L01_google_requires_a_verified_email(email, google, extra):
    r = google(**{"sub": uuid.uuid4().hex, "email": email, "name": "X", **extra})
    assert "google_error=failed" in r.headers.get("location", "")


# ---- L-02: password change ----
@pytest.mark.parametrize("extra", [{}, {"current_password": "Wrong1234"}])
def test_L02_change_needs_current_password(make_user, new_client, extra):
    client, user = make_user()
    r = client.put("/auth/me", json={"password": "Newpass123", **extra})
    assert r.status_code == 401
    assert r.json()["detail"] == "wrong_password"
    assert login(new_client(), user["email"]).status_code == 200


def test_L02_change_with_current_password(make_user, new_client):
    client, user = make_user()
    r = client.put("/auth/me", json={"password": "Newpass123", "current_password": PASSWORD})
    assert r.status_code == 200
    assert login(new_client(), user["email"], "Newpass123").status_code == 200


def test_L02_same_strength_rule_as_signup(make_user):
    client, _ = make_user()
    r = client.put("/auth/me", json={"password": "lettersonly", "current_password": PASSWORD})
    assert r.status_code in (400, 422)


def test_L02_google_account_may_set_a_first_password(make_user, sql, new_client):
    client, user = make_user()
    sql("UPDATE users SET password_hash = NULL WHERE id = %s", (user["id"],))
    assert client.put("/auth/me", json={"password": "Newpass123"}).status_code == 200
    assert login(new_client(), user["email"], "Newpass123").status_code == 200


# ---- L-03: reset links ----
def test_L03_reset_link_ignores_host_header(make_user, new_client, mail):
    _, user = make_user()
    new_client().post("/auth/forgot-password", json={"email": user["email"]}, headers={"host": "evil.example"})
    assert mail["reset"][user["email"]][1] == "http://localhost:8000"


def test_L03_google_redirect_ignores_host_header(new_client):
    r = new_client().get("/auth/google", headers={"host": "evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "http://localhost:8000/auth/google?canonical=1"


def test_reset_token_is_single_use_and_expires(make_user, new_client, mail, sql):
    _, user = make_user()
    c = new_client()
    c.post("/auth/forgot-password", json={"email": user["email"]})
    token = mail["reset"][user["email"]][0]
    assert c.post("/auth/reset-password", json={"token": token, "new_password": "Reset1234"}).status_code == 200
    assert c.post("/auth/reset-password", json={"token": token, "new_password": "Reset5678"}).status_code == 400
    c.post("/auth/forgot-password", json={"email": user["email"]})
    token = mail["reset"][user["email"]][0]
    sql("UPDATE password_reset_tokens SET expires_at = %s WHERE token = %s",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), token))
    assert c.post("/auth/reset-password", json={"token": token, "new_password": "Reset5678"}).status_code == 400


# ---- L-04: secrets ----
def test_L04_startup_fails_without_jwt_secret(app_module):
    env = {**os.environ, "JWT_SECRET": ""}
    r = subprocess.run([sys.executable, "-c", "import auth"], cwd=SRC, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "JWT_SECRET is not set" in r.stderr


def test_L04_connection_failure_never_prints_the_password(app_module):
    env = {**os.environ, "DATABASE_URL": "postgresql://someone:HUNTER2SECRET@127.0.0.1:1/none?connect_timeout=2"}
    r = subprocess.run([sys.executable, "-c", "import auth"], cwd=SRC, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "HUNTER2SECRET" not in r.stdout + r.stderr


# ---- L-13: code guessing ----
def test_L13_five_wrong_guesses_burn_the_code(new_client, mail, email):
    c = new_client()
    signup(c, email)
    code = mail["otp"][email]
    for _ in range(5):
        assert verify(c, email, wrong(code)).status_code == 400
    assert verify(c, email, code).status_code == 400


def test_L13_burnt_code_does_not_reset_the_cooldown(new_client, mail, email):
    c = new_client()
    signup(c, email)
    for _ in range(5):
        verify(c, email, wrong(mail["otp"][email]))
    assert c.post("/auth/resend-otp", json={"email": email}).status_code == 429
    before = mail["count"]
    signup(c, email)                               # re-signup must not hand out fresh guesses either
    assert mail["count"] == before


def test_L13_parallel_guesses_cannot_exceed_the_limit(new_client, mail, email):
    c = new_client()
    signup(c, email)
    code = mail["otp"][email]
    with ThreadPoolExecutor(12) as ex:
        list(ex.map(lambda _: verify(c, email, wrong(code)).status_code, range(12)))
    assert verify(c, email, code).status_code == 400


def test_L13_resend_then_verify_works(new_client, mail, sql, email):
    c = new_client()
    signup(c, email)
    for _ in range(5):
        verify(c, email, wrong(mail["otp"][email]))
    age_code(sql, email)
    assert c.post("/auth/resend-otp", json={"email": email}).status_code == 200
    assert verify(c, email, mail["otp"][email]).status_code == 200


# ---- L-14: lockout ----
def test_L14_success_resets_the_count(make_user, new_client):
    _, user = make_user()
    c = new_client()
    for _ in range(4):
        login(c, user["email"], "Wrong1234")
    assert login(c, user["email"]).status_code == 200
    login(c, user["email"], "Wrong1234")
    assert login(c, user["email"]).status_code == 200


def test_L14_five_failures_lock_that_caller_only(make_user, new_client):
    _, user = make_user()
    attacker = new_client("10.9.9.9")
    for _ in range(5):
        login(attacker, user["email"], "Wrong1234")
    assert login(attacker, user["email"]).status_code == 429
    assert login(new_client("10.1.1.1"), user["email"]).status_code == 200


def test_L14_parallel_guesses_cannot_exceed_the_limit(make_user, new_client):
    _, user = make_user()
    c = new_client("10.7.7.7")
    with ThreadPoolExecutor(15) as ex:
        codes = list(ex.map(lambda _: login(c, user["email"], "Wrong1234").status_code, range(15)))
    assert codes.count(401) == 5 and codes.count(429) == 10


# ---- L-15: revocation ----
def test_L15_reset_revokes_sessions_and_other_reset_tokens(make_user, new_client, mail):
    old, user = make_user()
    c = new_client()
    tokens = []
    for _ in range(2):
        c.post("/auth/forgot-password", json={"email": user["email"]})
        tokens.append(mail["reset"][user["email"]][0])
    assert c.post("/auth/reset-password", json={"token": tokens[1], "new_password": "Reset1234"}).status_code == 200
    assert old.get("/auth/me").status_code == 401
    assert c.post("/auth/reset-password", json={"token": tokens[0], "new_password": "Reset5678"}).status_code == 400


def test_L15_change_revokes_other_sessions_but_not_this_one(make_user, new_client):
    here, user = make_user()
    there = new_client()
    login(there, user["email"])
    here.put("/auth/me", json={"password": "Newpass123", "current_password": PASSWORD})
    assert there.get("/auth/me").status_code == 401
    assert here.get("/auth/me").status_code == 200


def test_L15_sessions_issued_before_the_deploy_still_work(app_module, make_user, new_client):
    import jwt
    _, user = make_user()
    legacy = jwt.encode({"sub": user["id"], "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                        os.environ["JWT_SECRET"], algorithm="HS256")
    c = new_client()
    c.cookies.set("session_token", legacy)
    assert c.get("/auth/me").status_code == 200


def test_L15_secure_cookie_on_https(app_module, make_user, new_client, monkeypatch):
    _, user = make_user()
    monkeypatch.setattr(app_module.auth, "SECURE_COOKIES", True, raising=False)
    r = login(new_client(), user["email"])
    assert "secure" in r.headers["set-cookie"].lower()


# ---- tables must not be readable through Supabase's public REST role ----
def test_every_table_is_hidden_from_the_anon_role(make_user, sql):
    make_user()
    open_tables = sql("SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                      "AND relkind = 'r' AND NOT relrowsecurity")
    assert open_tables == []
    sql("SET ROLE anon")
    try:
        assert sql("SELECT count(*) FROM users") == [(0,)]
    finally:
        sql("RESET ROLE")


# ---- L-05: pool (last, because on broken code it takes the whole app down) ----
def test_L05_parallel_requests_wait_for_a_connection(make_user):
    client, _ = make_user()
    with ThreadPoolExecutor(40) as ex:
        codes = list(ex.map(lambda _: client.get("/auth/me").status_code, range(120)))
    assert set(codes) == {200}


def test_L05_parallel_signups_for_one_email(new_client, mail, sql, email, make_user):
    with ThreadPoolExecutor(30) as ex:
        codes = list(ex.map(lambda _: signup(new_client(), email).status_code, range(30)))
    assert 500 not in codes
    assert sql("SELECT count(*) FROM users WHERE email = %s", (email,)) == [(1,)]
    assert make_user()[0].get("/auth/me").status_code == 200


def test_L05_sql_errors_do_not_leak_connections(new_client, mail, make_user):
    c = new_client()
    for _ in range(12):
        # Postgres rejects NUL in text, so this errors inside the query
        c.post("/auth/signup", json={"name": "bad\u0000name", "email": f"{uuid.uuid4().hex}@example.com",
                                     "password": PASSWORD})
    assert make_user()[0].get("/auth/me").status_code == 200
