"""
Login (Rebuild Stage 6, item 1).

One password from ``FOUNDER_OS_PASSWORD``. A successful ``POST /auth/login`` returns a
signed, expiring session token; every API route then requires
``Authorization: Bearer <token>`` except /health, /ping, /auth/*, CORS preflights and
the static frontend. When ``FOUNDER_OS_PASSWORD`` is unset, auth is off (local dev) and
the startup log says so loudly.

Tokens are HMAC-SHA256 signed (secret = ``FOUNDER_OS_SECRET`` or, failing that, a hash of
the password), carry an expiry (``FOUNDER_OS_SESSION_DAYS``, default 30) and need no
server-side storage, so they survive restarts and work across devices.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

# "/" and "/index.html" only 307 to "/app/" (main.root_redirect); they serve no files.
# /jobs/tick is called by the scheduler, not a signed-in user: it checks its own token.
EXEMPT_EXACT = {"/health", "/ping", "/auth/login", "/auth/status", "/", "/index.html", "/jobs/tick",
                "/integrations/telegram/webhook",   # verified by Telegram's secret header instead
                "/integrations/google/callback"}    # the browser returns from Google; the signed state is the check
EXEMPT_PREFIXES = ("/app/",)   # the static frontend mount; "/app" itself redirects to "/app/"
DEFAULT_ORIGINS = ["http://127.0.0.1:8000", "http://localhost:8000"]


def password() -> str | None:
    return os.getenv("FOUNDER_OS_PASSWORD") or None


def auth_required() -> bool:
    return password() is not None


def _secret() -> bytes:
    s = os.getenv("FOUNDER_OS_SECRET")
    if s:
        return s.encode("utf-8")
    return hashlib.sha256(("founder-os:" + (password() or "")).encode("utf-8")).digest()


def session_days() -> int:
    try:
        return max(1, int(os.getenv("FOUNDER_OS_SESSION_DAYS", "30")))
    except ValueError:
        return 30


def allowed_origins() -> list[str]:
    raw = os.getenv("FOUNDER_OS_ORIGINS") or os.getenv("FOUNDER_OS_ORIGIN")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return list(DEFAULT_ORIGINS)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_token(now: float | None = None) -> tuple[str, int]:
    now = now or time.time()
    exp = int(now + session_days() * 86400)
    payload = json.dumps({"iat": int(now), "exp": exp, "n": secrets.token_hex(8)}, separators=(",", ":")).encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}", exp


def verify_token(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    try:
        p64, s64 = token.split(".", 1)
        payload = _unb64(p64)
        sig = _unb64(s64)
    except Exception:  # noqa: BLE001
        return False
    expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    return int(data.get("exp", 0)) > (now or time.time())


def _token_from_scope(scope) -> str | None:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if headers.get("x-founder-token"):
        return headers["x-founder-token"].strip()
    return None


def is_exempt(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return True
    if path in EXEMPT_EXACT or path == "/app":
        return True
    return any(path.startswith(p) for p in EXEMPT_PREFIXES)


class AuthMiddleware:
    """Pure ASGI middleware (keeps SSE streaming untouched)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not auth_required():
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if is_exempt(path, scope.get("method", "GET")) or verify_token(_token_from_scope(scope)):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"detail": "Not authenticated"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)


# ── Login rate limiting ──────────────────────────────────────
# Simple in-memory limiter per client address: after LOGIN_MAX_FAILURES wrong passwords
# inside LOGIN_WINDOW_SECONDS the address is locked for LOGIN_LOCK_SECONDS. Enough to stop
# online guessing on a single-user app; it resets on process restart.
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "300"))
LOGIN_LOCK_SECONDS = int(os.getenv("LOGIN_LOCK_SECONDS", "900"))
_attempts: dict[str, dict] = {}
_attempts_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")   # Render sits behind a proxy
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_locked(ip: str, now: float | None = None) -> int:
    """Seconds remaining on the lock for this address, 0 when not locked."""
    now = now or time.time()
    with _attempts_lock:
        rec = _attempts.get(ip)
        if not rec:
            return 0
        if rec.get("locked_until", 0) > now:
            return int(rec["locked_until"] - now) + 1
        return 0


def record_login_failure(ip: str, now: float | None = None) -> int:
    """Register a failed attempt; returns seconds locked (0 if still allowed)."""
    now = now or time.time()
    with _attempts_lock:
        rec = _attempts.setdefault(ip, {"failures": [], "locked_until": 0})
        rec["failures"] = [t for t in rec["failures"] if now - t < LOGIN_WINDOW_SECONDS] + [now]
        if len(rec["failures"]) >= LOGIN_MAX_FAILURES:
            rec["locked_until"] = now + LOGIN_LOCK_SECONDS
            rec["failures"] = []
            return LOGIN_LOCK_SECONDS
        return 0


def clear_login_failures(ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(ip, None)


def reset_rate_limits() -> None:
    with _attempts_lock:
        _attempts.clear()


# ── Endpoints ────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@router.get("/auth/status")
def auth_status(request: Request):
    token = _token_from_scope(request.scope)
    return {"auth_required": auth_required(),
            "authenticated": (not auth_required()) or verify_token(token)}


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request):
    if not auth_required():
        return {"auth_required": False, "token": None, "expires_at": None}
    ip = _client_ip(request)
    wait = login_locked(ip)
    if wait:
        return JSONResponse({"detail": f"Too many attempts. Try again in {wait} seconds."},
                            status_code=429, headers={"Retry-After": str(wait)})
    if not hmac.compare_digest(payload.password.encode("utf-8"), (password() or "").encode("utf-8")):
        locked = record_login_failure(ip)
        if locked:
            return JSONResponse({"detail": f"Too many attempts. Try again in {locked} seconds."},
                                status_code=429, headers={"Retry-After": str(locked)})
        return JSONResponse({"detail": "Wrong password"}, status_code=401)
    clear_login_failures(ip)
    token, exp = issue_token()
    return {"auth_required": True, "token": token, "expires_at": exp}
