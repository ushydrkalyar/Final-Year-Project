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
  • 5 failed login attempts → 15-minute account lockout
  • OTP codes expire after 10 minutes
  • Password-reset tokens expire after 1 hour and are single-use
  • Forgot-password always returns 200 (prevents email enumeration)
──────────────────────────────────────────────────────────────────────────────
"""
import os
import re
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import psycopg2
import psycopg2.extras
import psycopg2.pool
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, field_validator
from starlette.config import Config

import email_service

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it to your Postgres connection string."
    )

_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=DATABASE_URL)

JWT_SECRET        = os.environ.get("JWT_SECRET", "dev-only-secret-change-before-deploying")
JWT_ALGORITHM     = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24 * 7   # 7 days
COOKIE_NAME       = "session_token"

# Google OAuth
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

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
# DB helpers
# ---------------------------------------------------------------------------
class _PooledConn:
    def __init__(self):
        self._conn = _pool.getconn()
        self._conn.cursor_factory = psycopg2.extras.RealDictCursor
        self._cur = self._conn.cursor()

    def execute(self, sql: str, params: tuple = ()):
        self._cur.execute(sql.replace("?", "%s"), params)
        return self._cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._cur.close()
        _pool.putconn(self._conn)


def _get_db() -> _PooledConn:
    return _PooledConn()


def init_db():
    """Create all tables at server startup."""
    conn = _get_db()

    # Core users table
    conn.execute("""
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
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {defn}")
            conn.commit()
        except Exception:
            conn._conn.rollback()

    # Email OTP verification codes
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_verifications (
            id         TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            otp        TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            verified   BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)

    # Password reset tokens
    conn.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            token      TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used       BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)

    # Login attempt tracking (for lockout)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id           TEXT PRIMARY KEY,
            email        TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            success      BOOLEAN NOT NULL
        )
    """)

    conn.commit()
    conn.close()


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
def _create_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")


def _set_session_cookie(response: Response, user_id: str):
    token = _create_token(user_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=JWT_EXPIRE_MINUTES * 60,
        path="/",
    )


# ---------------------------------------------------------------------------
# OTP helpers
# ---------------------------------------------------------------------------
def _generate_otp() -> str:
    return str(random.randint(100000, 999999))


def _store_otp(email: str, otp: str):
    """Save a fresh OTP, invalidating any previous ones for the same email."""
    conn = _get_db()
    # Invalidate old codes
    conn.execute("DELETE FROM email_verifications WHERE email = ?", (email,))
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    conn.execute(
        "INSERT INTO email_verifications (id, email, otp, expires_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), email, otp, expires),
    )
    conn.commit()
    conn.close()


def _verify_otp(email: str, otp: str) -> bool:
    """Returns True and marks as verified if OTP matches and is not expired."""
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM email_verifications WHERE email = ? AND verified = FALSE ORDER BY expires_at DESC LIMIT 1",
        (email,),
    ).fetchone()
    if not row:
        conn.close()
        return False
    expires = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) > expires:
        conn.close()
        return False
    if row["otp"] != otp:
        conn.close()
        return False
    conn.execute("UPDATE email_verifications SET verified = TRUE WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True


# ---------------------------------------------------------------------------
# Lockout helpers
# ---------------------------------------------------------------------------
LOCKOUT_MAX_ATTEMPTS = 5
LOCKOUT_WINDOW_MINUTES = 15


def _record_attempt(email: str, success: bool):
    conn = _get_db()
    conn.execute(
        "INSERT INTO login_attempts (id, email, attempted_at, success) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), email, datetime.now(timezone.utc).isoformat(), success),
    )
    conn.commit()
    conn.close()


def _is_locked_out(email: str) -> bool:
    window_start = (datetime.now(timezone.utc) - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).isoformat()
    conn = _get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM login_attempts "
        "WHERE email = ? AND success = FALSE AND attempted_at > ?",
        (email, window_start),
    ).fetchone()
    conn.close()
    return (row["cnt"] or 0) >= LOCKOUT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# User formatting
# ---------------------------------------------------------------------------
AVATAR_COLORS = ["#6366F1", "#22C55E", "#F59E0B", "#EF4444", "#8B5CF6", "#06B6D4"]


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
        return v

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not re.search(r"[A-Za-z]", v) or not re.search(r"[0-9]", v):
            raise ValueError("Password must contain both letters and numbers")
        return v


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

    @field_validator("new_password")
    @classmethod
    def _pw_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not re.search(r"[A-Za-z]", v) or not re.search(r"[0-9]", v):
            raise ValueError("Password must contain letters and numbers")
        return v


class ProfileUpdateRequest(BaseModel):
    name: str | None = None
    password: str | None = None


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
def get_current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    user_id = _decode_token(token)
    conn = _get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    return _public_user(row)


def get_current_user_optional(request: Request) -> dict | None:
    try:
        return get_current_user(request)
    except HTTPException:
        return None


# ---------------------------------------------------------------------------
# Routes — Signup & Email Verification
# ---------------------------------------------------------------------------
@router.post("/signup")
def signup(body: SignupRequest, response: Response):
    email = body.email.lower()
    conn = _get_db()
    existing = conn.execute("SELECT id, email_verified FROM users WHERE email = ?", (email,)).fetchone()

    if existing:
        if existing["email_verified"]:
            conn.close()
            raise HTTPException(status_code=409, detail="An account with this email already exists")
        # Account exists but unverified — resend OTP
        conn.close()
        otp = _generate_otp()
        _store_otp(email, otp)
        email_service.send_otp_email(email, otp)
        return {"message": "Verification code resent. Check your email.", "email": email}

    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, name, email, password_hash, avatar_color, created_at, email_verified) "
        "VALUES (?, ?, ?, ?, ?, ?, FALSE)",
        (
            user_id,
            body.name.strip(),
            email,
            _hash_password(body.password),
            AVATAR_COLORS[abs(hash(user_id)) % len(AVATAR_COLORS)],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    # Send OTP
    otp = _generate_otp()
    _store_otp(email, otp)
    email_service.send_otp_email(email, otp)

    return {"message": "Account created. Check your email for the verification code.", "email": email}


@router.post("/verify-email")
def verify_email(body: VerifyEmailRequest, response: Response):
    email = body.email.lower()
    if not _verify_otp(email, body.otp):
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    conn = _get_db()
    conn.execute("UPDATE users SET email_verified = TRUE WHERE email = ?", (email,))
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    _set_session_cookie(response, row["id"])
    return {"user": _public_user(row)}


@router.post("/resend-otp")
def resend_otp(body: ResendOtpRequest):
    email = body.email.lower()
    conn = _get_db()
    row = conn.execute("SELECT id, email_verified FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not row:
        # Don't reveal whether the email exists
        return {"message": "If that email is registered, a new code has been sent."}
    if row["email_verified"]:
        raise HTTPException(status_code=400, detail="Email is already verified")

    # Rate limit: check if OTP was sent in the last 60 seconds
    conn2 = _get_db()
    recent = conn2.execute(
        "SELECT expires_at FROM email_verifications WHERE email = ? ORDER BY expires_at DESC LIMIT 1",
        (email,),
    ).fetchone()
    conn2.close()
    if recent:
        sent_at = datetime.fromisoformat(recent["expires_at"]) - timedelta(minutes=10)
        if (datetime.now(timezone.utc) - sent_at).total_seconds() < 60:
            raise HTTPException(status_code=429, detail="Please wait 60 seconds before requesting a new code")

    otp = _generate_otp()
    _store_otp(email, otp)
    email_service.send_otp_email(email, otp)
    return {"message": "A new verification code has been sent to your email."}


# ---------------------------------------------------------------------------
# Routes — Login / Logout
# ---------------------------------------------------------------------------
@router.post("/login")
def login(body: LoginRequest, response: Response):
    email = body.email.lower()

    if _is_locked_out(email):
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Account locked for {LOCKOUT_WINDOW_MINUTES} minutes."
        )

    conn = _get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not row or not row.get("password_hash") or not _verify_password(body.password, row["password_hash"]):
        _record_attempt(email, False)
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    if not row["email_verified"]:
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before logging in. Check your inbox for the verification code."
        )

    _record_attempt(email, True)
    _set_session_cookie(response, row["id"])
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
def update_me(body: ProfileUpdateRequest, user: dict = Depends(get_current_user)):
    conn = _get_db()
    if body.name and body.name.strip():
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (body.name.strip(), user["id"]))
    if body.password:
        if len(body.password) < 8:
            conn.close()
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_hash_password(body.password), user["id"]),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()
    return {"user": _public_user(row)}



# ---------------------------------------------------------------------------
# Routes — Verify current password (used by profile Change Password flow)
# ---------------------------------------------------------------------------
class VerifyPasswordRequest(BaseModel):
    current_password: str


@router.post("/verify-password")
def verify_current_password(body: VerifyPasswordRequest, user: dict = Depends(get_current_user)):
    """Check the user's current password without changing anything."""
    conn = _get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()

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
def forgot_password(body: ForgotPasswordRequest, request: Request):
    email = body.email.lower()
    conn = _get_db()
    row = conn.execute("SELECT id FROM users WHERE email = ? AND email_verified = TRUE", (email,)).fetchone()
    conn.close()

    # Always return 200 — don't reveal whether email exists
    if not row:
        return {"message": "If that email is registered, you'll receive a reset link shortly."}

    token = secrets.token_urlsafe(48)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    conn2 = _get_db()
    conn2.execute(
        "INSERT INTO password_reset_tokens (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), row["id"], token, expires),
    )
    conn2.commit()
    conn2.close()

    base_url = str(request.base_url).rstrip("/")
    email_service.send_reset_email(email, token, base_url)
    return {"message": "If that email is registered, you'll receive a reset link shortly."}


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest):
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM password_reset_tokens WHERE token = ? AND used = FALSE",
        (body.token,),
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid or already used reset link")

    expires = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) > expires:
        conn.close()
        raise HTTPException(status_code=400, detail="This reset link has expired. Please request a new one.")

    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (_hash_password(body.new_password), row["user_id"]),
    )
    conn.execute("UPDATE password_reset_tokens SET used = TRUE WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return {"message": "Password updated successfully. You can now log in."}


# ---------------------------------------------------------------------------
# Routes — Google OAuth
# ---------------------------------------------------------------------------
@router.get("/google")
async def google_login(request: Request):
    redirect_uri = str(request.url_for("google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/google/callback", name="google_callback")
async def google_callback(request: Request, response: Response):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        raise HTTPException(status_code=400, detail="Google authentication failed")

    user_info = token.get("userinfo") or await oauth.google.userinfo(token=token)
    google_id = user_info["sub"]
    email     = user_info["email"].lower()
    name      = user_info.get("name", email.split("@")[0])

    conn = _get_db()
    row = conn.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()

    if not row:
        # Check if email already exists (regular account → link Google)
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET google_id = ?, email_verified = TRUE WHERE id = ?",
                (google_id, row["id"]),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        else:
            # Brand new Google user
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, name, email, password_hash, avatar_color, created_at, email_verified, google_id) "
                "VALUES (?, ?, ?, NULL, ?, ?, TRUE, ?)",
                (
                    user_id, name, email,
                    AVATAR_COLORS[abs(hash(user_id)) % len(AVATAR_COLORS)],
                    datetime.now(timezone.utc).isoformat(),
                    google_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    conn.close()

    redirect = RedirectResponse(url="/splash.html?next=/editor.html")
    _set_session_cookie(redirect, row["id"])
    return redirect
