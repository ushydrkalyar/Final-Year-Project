"""
auth.py — user accounts, email verification, password reset, Google OAuth.

Routes:
    POST /auth/signup              { name, email, password }  → sends OTP email
    POST /auth/verify-email        { email, otp }             → activates account, sets session
    POST /auth/resend-otp          { email }                  → resend OTP (1/min rate limit)
    POST /auth/login               { email, password }        → login (verified accounts only)
    POST /auth/logout                                         → clear session cookie
    GET  /auth/me                                             → current user (protected)
    PUT  /auth/me                  { name?, password? }       → update profile (protected)
    POST /auth/forgot-password     { email }                  → send reset link
    POST /auth/reset-password      { token, new_password }    → use token to set new password
    GET  /auth/google                                         → start Google OAuth
    GET  /auth/google/callback                                → OAuth callback

── Security features ────────────────────────────────────────────────────────
  • Unverified accounts cannot log in
  • 5 failed logins from one IP → 15-minute lockout of that email for that IP
  • OTP codes expire after 10 minutes
  • Password-reset tokens expire after 1 hour and are single-use
  • Forgot-password always returns 200 (prevents email enumeration)
──────────────────────────────────────────────────────────────────────────────
"""
import hashlib
import os
import re
import secrets
import uuid
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import psycopg2
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.config import Config

import db
import email_service

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JWT_SECRET = os.environ.get("JWT_SECRET", "").strip()
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not set. Put a long random value in ../.env "
                       "(python -c 'import secrets; print(secrets.token_urlsafe(48))').")
JWT_ALGORITHM     = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24 * 7   # 7 days
COOKIE_NAME       = "session_token"
PENDING_COOKIE    = "pending_signup"

# Links we email, and where Google returns to, come from configuration, never from the
# request's Host header (the client controls that).
APP_BASE_URL = (os.environ.get("APP_BASE_URL", "").strip() or "http://localhost:8000").rstrip("/")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip() or f"{APP_BASE_URL}/auth/google/callback"

SECURE_COOKIES = APP_BASE_URL.startswith("https://")

# Google OAuth
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# True only once real credentials are in .env — lets us fail gracefully
# (redirect back with a friendly message) instead of sending the user
# to Google with an empty client_id, which Google shows as a raw,
# unbranded "Error 401: invalid_client" page.
GOOGLE_CONFIGURED = bool(GOOGLE_CLIENT_ID.strip() and GOOGLE_CLIENT_SECRET.strip())

_starlette_config = Config(environ={
    "GOOGLE_CLIENT_ID":     GOOGLE_CLIENT_ID,
    "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
})
oauth = OAuth(_starlette_config)
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_db():
    """Create all tables at server startup."""
    with db.transaction() as cur:

        # Core users table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT,
                avatar_color  TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                email_verified BOOLEAN NOT NULL DEFAULT FALSE,
                google_id     TEXT UNIQUE
            )
        """)

        # Migrate: add columns if this is an existing DB that lacked them
        for col, defn in [
            ("email_verified", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("google_id",      "TEXT UNIQUE"),
        ]:
            cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {defn}")

        # Bumped on password change/reset: revokes every session issued before
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0")

        # Email OTP verification codes
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_verifications (
                id         TEXT PRIMARY KEY,
                email      TEXT NOT NULL,
                otp        TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                verified   BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)

        cur.execute("ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0")

        # Password reset tokens
        cur.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                token      TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used       BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)

        # Login attempt tracking (for lockout)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id           TEXT PRIMARY KEY,
                email        TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                success      BOOLEAN NOT NULL
            )
        """)
        cur.execute("ALTER TABLE login_attempts ADD COLUMN IF NOT EXISTS ip TEXT")

        for table in ("users", "email_verifications", "password_reset_tokens", "login_attempts"):
            db.enable_rls(cur, table)


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------
def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# ---------------------------------------------------------------------------
# JWT session helpers
# ---------------------------------------------------------------------------
def _create_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user["id"], "ver": user["token_version"], "iat": now, "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
                             options={"require": ["sub", "exp"]})
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")


def _set_session_cookie(response: Response, user: dict):
    token = _create_token(user)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=SECURE_COOKIES,
        samesite="lax",
        max_age=JWT_EXPIRE_MINUTES * 60,
        path="/",
    )


# ---------------------------------------------------------------------------
# Pending-signup proof
# ---------------------------------------------------------------------------
# An emailed code proves who owns the inbox, not who chose the password waiting on the
# account: anyone can sign up with someone else's email before or after they do. So a
# code only verifies in a browser that proved it knows the password now on the account
# (by signing up with it, or logging in with it).
def _password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()


def _set_pending_cookie(response: Response, email: str, password_hash: str):
    token = jwt.encode({"pending": email, "pw": _password_fingerprint(password_hash),
                        "exp": datetime.now(timezone.utc) + timedelta(days=1)},
                       JWT_SECRET, algorithm=JWT_ALGORITHM)
    response.set_cookie(PENDING_COOKIE, token, httponly=True, secure=SECURE_COOKIES, samesite="lax", max_age=86400, path="/auth")


def _proved_password(request: Request, email: str, user: dict | None) -> bool:
    if not user or user["email_verified"] or not user["password_hash"]:
        return False
    try:
        claim = jwt.decode(request.cookies.get(PENDING_COOKIE, ""), JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return False
    return claim.get("pending") == email and claim.get("pw") == _password_fingerprint(user["password_hash"])


# ---------------------------------------------------------------------------
# OTP helpers
# ---------------------------------------------------------------------------
def _generate_otp() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


OTP_MAX_ATTEMPTS = 5


def _issue_otp(cur, email: str) -> str:
    """Save and return a fresh code, invalidating earlier ones for the same email."""
    otp = _generate_otp()
    cur.execute("DELETE FROM email_verifications WHERE email = %s", (email,))
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    cur.execute(
        "INSERT INTO email_verifications (id, email, otp, expires_at) VALUES (%s, %s, %s, %s)",
        (str(uuid.uuid4()), email, otp, expires),
    )
    return otp


def _consume_otp(cur, email: str, otp: str) -> bool:
    """True, and the code is used up, if it matches and has not expired."""
    cur.execute(
        "SELECT * FROM email_verifications WHERE email = %s AND verified = FALSE "
        "ORDER BY expires_at DESC LIMIT 1 FOR UPDATE",
        (email,),
    )
    row = cur.fetchone()
    if (not row or row["attempts"] >= OTP_MAX_ATTEMPTS
            or datetime.now(timezone.utc) > datetime.fromisoformat(row["expires_at"])):
        return False
    if not secrets.compare_digest(row["otp"].encode(), otp.encode()):
        # A burnt code's row stays: it is also what the resend cooldown is measured from
        cur.execute("UPDATE email_verifications SET attempts = attempts + 1 WHERE id = %s", (row["id"],))
        return False
    cur.execute("UPDATE email_verifications SET verified = TRUE WHERE id = %s", (row["id"],))
    return True


# ---------------------------------------------------------------------------
# Lockout helpers
# ---------------------------------------------------------------------------
LOCKOUT_MAX_ATTEMPTS = 5
LOCKOUT_WINDOW_MINUTES = 15


def _begin_attempt(email: str, ip: str) -> str | None:
    """Reserve a login attempt, or None when this email + IP is locked out.

    The attempt counts as a failure before the password is checked, so parallel guesses
    cannot slip past the limit. Keyed on email + IP, so a stranger cannot lock the owner
    out; only failures since the caller's last successful login count.
    """
    now = datetime.now(timezone.utc)
    window = (now - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).isoformat()
    with db.transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"login:{email}:{ip}",))
        cur.execute("DELETE FROM login_attempts WHERE attempted_at < %s", ((now - timedelta(days=1)).isoformat(),))
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM login_attempts "
            "WHERE email = %s AND ip = %s AND success = FALSE AND attempted_at > %s "
            "AND attempted_at > COALESCE((SELECT MAX(attempted_at) FROM login_attempts "
            "                             WHERE email = %s AND ip = %s AND success = TRUE), '')",
            (email, ip, window, email, ip),
        )
        if cur.fetchone()["cnt"] >= LOCKOUT_MAX_ATTEMPTS:
            return None
        attempt_id = str(uuid.uuid4())
        cur.execute("INSERT INTO login_attempts (id, email, ip, attempted_at, success) VALUES (%s, %s, %s, %s, FALSE)",
                    (attempt_id, email, ip, now.isoformat()))
    return attempt_id


# ---------------------------------------------------------------------------
# User formatting
# ---------------------------------------------------------------------------
AVATAR_COLORS = ["#6366F1", "#22C55E", "#F59E0B", "#EF4444", "#8B5CF6", "#06B6D4"]


def _avatar_color(user_id: str) -> str:
    return AVATAR_COLORS[int(user_id.replace("-", ""), 16) % len(AVATAR_COLORS)]


def _public_user(row: dict) -> dict:
    initials = "".join(w[0] for w in row["name"].split()[:2]).upper() or "?"
    return {
        "id":             row["id"],
        "name":           row["name"],
        "email":          row["email"],
        "avatar_color":   row["avatar_color"],
        "initials":       initials,
        "created_at":     row["created_at"],
        "email_verified": row.get("email_verified", False),
    }


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
def _no_nul(v):
    if v and "\x00" in v:              # Postgres rejects NUL in text
        raise ValueError("Contains an invalid character")
    return v


def _check_password_strength(v):
    """The one password rule (signup, reset, change)."""
    if v is not None:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not re.search(r"[A-Za-z]", v) or not re.search(r"[0-9]", v):
            raise ValueError("Password must contain both letters and numbers")
    return v


class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Name must be at least 2 characters")
        return _no_nul(v)

    _strong = field_validator("password")(_check_password_strength)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str


class ResendOtpRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    _strong = field_validator("new_password")(_check_password_strength)


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    password: str | None = None
    current_password: str | None = None

    _name_clean = field_validator("name")(_no_nul)
    _strong = field_validator("password")(_check_password_strength)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
def get_current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    claims = _decode_token(token)
    with db.transaction() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (claims["sub"],))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    if claims.get("ver", 0) != row["token_version"]:    # tokens from before versions existed count as 0
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    return _public_user(row)


# ---------------------------------------------------------------------------
# Routes — Signup & Email Verification
# ---------------------------------------------------------------------------
@router.post("/signup")
def signup(body: SignupRequest, response: Response):
    email = body.email.lower()
    password_hash = _hash_password(body.password)   # slow on purpose: not inside the transaction
    try:
        with db.transaction() as cur:
            cur.execute("SELECT id, email_verified FROM users WHERE email = %s FOR UPDATE", (email,))
            existing = cur.fetchone()
            if existing and existing["email_verified"]:
                raise HTTPException(status_code=409, detail="An account with this email already exists")
            if existing:
                # Not verified, so nobody owns it yet: the latest signup replaces what is pending
                cur.execute("UPDATE users SET name = %s, password_hash = %s WHERE id = %s",
                            (body.name, password_hash, existing["id"]))
                message = "Verification code resent. Check your email."
            else:
                user_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO users (id, name, email, password_hash, avatar_color, created_at, email_verified) "
                    "VALUES (%s, %s, %s, %s, %s, %s, FALSE)",
                    (user_id, body.name, email, password_hash, _avatar_color(user_id),
                     datetime.now(timezone.utc).isoformat()),
                )
                message = "Account created. Check your email for the verification code."
            # Signing up again must not be a way round the resend cooldown (and the guess limit)
            otp = None if existing and _code_sent_recently(cur, email) else _issue_otp(cur, email)
    except psycopg2.errors.UniqueViolation:     # lost a race with a signup for the same email
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    if otp:
        email_service.send_otp_email(email, otp)
    else:
        message = "We sent you a code moments ago. Check your email."
    _set_pending_cookie(response, email, password_hash)
    return {"message": message, "email": email}


@router.post("/verify-email")
def verify_email(body: VerifyEmailRequest, request: Request, response: Response):
    email = body.email.lower()
    row = None
    with db.transaction() as cur:
        cur.execute("SELECT * FROM users WHERE email = %s FOR UPDATE", (email,))
        user = cur.fetchone()
        proved = _proved_password(request, email, user)
        if proved and _consume_otp(cur, email, body.otp):
            cur.execute("UPDATE users SET email_verified = TRUE WHERE id = %s RETURNING *", (user["id"],))
            row = cur.fetchone()
    if not proved:
        raise HTTPException(status_code=400, detail="To finish verifying, log in with your password, "
                                                    "or sign up again, in this browser.")
    if not row:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    response.delete_cookie(PENDING_COOKIE, path="/auth")
    _set_session_cookie(response, row)
    return {"user": _public_user(row)}


def _code_sent_recently(cur, email: str) -> bool:
    cur.execute(
        "SELECT expires_at FROM email_verifications WHERE email = %s ORDER BY expires_at DESC LIMIT 1",
        (email,),
    )
    recent = cur.fetchone()
    if not recent:
        return False
    sent_at = datetime.fromisoformat(recent["expires_at"]) - timedelta(minutes=10)
    return (datetime.now(timezone.utc) - sent_at).total_seconds() < 60


@router.post("/resend-otp")
def resend_otp(body: ResendOtpRequest):
    email = body.email.lower()
    with db.transaction() as cur:
        cur.execute("SELECT id, email_verified FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        if not row:
            # Don't reveal whether the email exists
            return {"message": "If that email is registered, a new code has been sent."}
        if row["email_verified"]:
            raise HTTPException(status_code=400, detail="Email is already verified")
        if _code_sent_recently(cur, email):
            raise HTTPException(status_code=429, detail="Please wait 60 seconds before requesting a new code")
        otp = _issue_otp(cur, email)
    email_service.send_otp_email(email, otp)
    return {"message": "A new verification code has been sent to your email."}


# ---------------------------------------------------------------------------
# Routes — Login / Logout
# ---------------------------------------------------------------------------
@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    email = body.email.lower()

    attempt = _begin_attempt(email, request.client.host if request.client else "")
    if not attempt:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {LOCKOUT_WINDOW_MINUTES} minutes."
        )

    with db.transaction() as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        row = cur.fetchone()

    if not row or not row.get("password_hash") or not _verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    with db.transaction() as cur:
        if row["email_verified"]:
            cur.execute("UPDATE login_attempts SET success = TRUE, attempted_at = %s WHERE id = %s",
                        (datetime.now(timezone.utc).isoformat(), attempt))
        else:       # right password: neither a failure nor a login
            cur.execute("DELETE FROM login_attempts WHERE id = %s", (attempt,))

    if not row["email_verified"]:
        refusal = JSONResponse(status_code=403, content={
            "detail": "Please verify your email before logging in. Check your inbox for the verification code."})
        # The password was right, so this browser may finish the verification
        _set_pending_cookie(refusal, email, row["password_hash"])
        return refusal

    _set_session_cookie(response, row)
    return {"user": _public_user(row)}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — Profile
# ---------------------------------------------------------------------------
@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}


@router.put("/me")
def update_me(body: ProfileUpdateRequest, response: Response, user: dict = Depends(get_current_user)):
    new_hash = old_hash = None
    if body.password:
        with db.transaction() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", (user["id"],))
            old_hash = cur.fetchone()["password_hash"]
        # Google-only accounts (no hash) may set a first password without one
        if old_hash and not (body.current_password and _verify_password(body.current_password, old_hash)):
            raise HTTPException(status_code=401, detail="wrong_password")
        new_hash = _hash_password(body.password)
    with db.transaction() as cur:
        if body.name and body.name.strip():
            cur.execute("UPDATE users SET name = %s WHERE id = %s", (body.name.strip(), user["id"]))
        if new_hash:
            # ...only if the password is still the one that was just checked
            cur.execute("UPDATE users SET password_hash = %s, token_version = token_version + 1 WHERE id = %s "
                        "AND password_hash IS NOT DISTINCT FROM %s RETURNING id", (new_hash, user["id"], old_hash))
            if not cur.fetchone():
                raise HTTPException(status_code=409, detail="Your password was changed meanwhile. Try again.")
        cur.execute("SELECT * FROM users WHERE id = %s", (user["id"],))
        row = cur.fetchone()
    if new_hash:
        _set_session_cookie(response, row)      # this browser stays in; every other session is out
    return {"user": _public_user(row)}


# ---------------------------------------------------------------------------
# Routes — Verify current password (used by profile Change Password flow)
# ---------------------------------------------------------------------------
class VerifyPasswordRequest(BaseModel):
    current_password: str


@router.post("/verify-password")
def verify_current_password(body: VerifyPasswordRequest, user: dict = Depends(get_current_user)):
    """Check the user's current password without changing anything."""
    with db.transaction() as cur:
        cur.execute("SELECT password_hash FROM users WHERE id = %s", (user["id"],))
        row = cur.fetchone()

    if not row or not row.get("password_hash"):
        # Google-only accounts have no password
        raise HTTPException(status_code=400, detail="no_password")

    if not _verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="wrong_password")

    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — Forgot / Reset Password
# ---------------------------------------------------------------------------
@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest):
    email = body.email.lower()
    token = secrets.token_urlsafe(48)
    with db.transaction() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s AND email_verified = TRUE", (email,))
        row = cur.fetchone()
        if row:
            expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            cur.execute(
                "INSERT INTO password_reset_tokens (id, user_id, token, expires_at) VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), row["id"], token, expires),
            )
    if row:
        email_service.send_reset_email(email, token, APP_BASE_URL)
    # Always return 200 — don't reveal whether email exists
    return {"message": "If that email is registered, you'll receive a reset link shortly."}


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest):
    new_hash = _hash_password(body.new_password)
    with db.transaction() as cur:
        # Claim the token atomically: two simultaneous uses cannot both succeed
        cur.execute("UPDATE password_reset_tokens SET used = TRUE "
                    "WHERE token = %s AND used = FALSE RETURNING user_id, expires_at", (body.token,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Invalid or already used reset link")
        if datetime.now(timezone.utc) > datetime.fromisoformat(row["expires_at"]):
            raise HTTPException(status_code=400, detail="This reset link has expired. Please request a new one.")
        cur.execute("UPDATE users SET password_hash = %s, token_version = token_version + 1 WHERE id = %s",
                    (new_hash, row["user_id"]))
        cur.execute("UPDATE password_reset_tokens SET used = TRUE WHERE user_id = %s", (row["user_id"],))
    return {"message": "Password updated successfully. You can now log in."}


# ---------------------------------------------------------------------------
# Routes — Google OAuth
# ---------------------------------------------------------------------------
def _google_account(google_id: str, email: str, name: str) -> dict:
    """The user row for a Google sign-in, linking or creating it as needed (blocking)."""
    with db.transaction() as cur:
        cur.execute("SELECT * FROM users WHERE google_id = %s", (google_id,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        if row and row["email_verified"]:     # regular account -> link Google
            cur.execute("UPDATE users SET google_id = %s WHERE id = %s RETURNING *", (google_id, row["id"]))
            return cur.fetchone()
        if row:     # never verified: whoever typed that name and password has not shown they own the inbox
            cur.execute("UPDATE users SET google_id = %s, email_verified = TRUE, password_hash = NULL, name = %s "
                        "WHERE id = %s RETURNING *", (google_id, name, row["id"]))
            return cur.fetchone()
        user_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO users (id, name, email, password_hash, avatar_color, created_at, email_verified, google_id) "
            "VALUES (%s, %s, %s, NULL, %s, %s, TRUE, %s) RETURNING *",
            (user_id, name, email, _avatar_color(user_id), datetime.now(timezone.utc).isoformat(), google_id),
        )
        return cur.fetchone()


@router.get("/google")
async def google_login(request: Request):
    if not GOOGLE_CONFIGURED:
        return RedirectResponse(url="/login.html?google_error=not_configured")
    target = urlsplit(GOOGLE_REDIRECT_URI)
    if request.url.netloc != target.netloc and "canonical" not in request.query_params:
        # The OAuth state cookie must be set on the host Google returns to
        # (opening 127.0.0.1 when localhost is registered used to fail here).
        return RedirectResponse(url=f"{target.scheme}://{target.netloc}/auth/google?canonical=1")
    return await oauth.google.authorize_redirect(request, GOOGLE_REDIRECT_URI)


@router.get("/google/callback", name="google_callback")
async def google_callback(request: Request):
    if not GOOGLE_CONFIGURED:
        return RedirectResponse(url="/login.html?google_error=not_configured")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        # Print the real reason to the server terminal — a silent redirect
        # is friendly for the user but useless for debugging a failure.
        print(f"\n❌ Google OAuth callback failed: {type(e).__name__}: {e}\n")
        return RedirectResponse(url="/login.html?google_error=failed")

    user_info = token.get("userinfo") or await oauth.google.userinfo(token=token)
    if (not user_info.get("sub") or not user_info.get("email")
            or user_info.get("email_verified") not in (True, "true")):
        return RedirectResponse(url="/login.html?google_error=failed")
    google_id = user_info["sub"]
    email     = user_info["email"].lower()
    name      = user_info.get("name") or email.split("@")[0]

    row = await run_in_threadpool(_google_account, google_id, email, name)

    redirect = RedirectResponse(url="/splash.html?next=/home.html")
    _set_session_cookie(redirect, row)
    return redirect
