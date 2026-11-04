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
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

EXEMPT_EXACT = {"/health", "/ping", "/auth/login", "/auth/status", "/", "/index.html"}
EXEMPT_PREFIXES = ("/app/", "/app")
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
    if path in EXEMPT_EXACT:
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


# ── Endpoints ────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@router.get("/auth/status")
def auth_status(request: Request):
    token = _token_from_scope(request.scope)
    return {"auth_required": auth_required(),
            "authenticated": (not auth_required()) or verify_token(token)}


@router.post("/auth/login")
def login(payload: LoginRequest):
    if not auth_required():
        return {"auth_required": False, "token": None, "expires_at": None}
    if not hmac.compare_digest(payload.password.encode("utf-8"), (password() or "").encode("utf-8")):
        return JSONResponse({"detail": "Wrong password"}, status_code=401)
    token, exp = issue_token()
    return {"auth_required": True, "token": token, "expires_at": exp}
