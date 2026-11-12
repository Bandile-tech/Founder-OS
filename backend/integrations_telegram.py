"""
Telegram pairing and webhook registration (Phase 6C; the channel itself is in notify.py).

Primitives: pair_code, is_paired, chat_id, pair, unpair
Endpoints:  POST /integrations/telegram/set-webhook, POST /integrations/telegram/unpair

Pairing: Bandile opens the bot and sends it the six-character code shown in Settings. The
code is derived from the server's secret, so it is stable and needs no storage. The paired
chat id lives in the ``telegram`` integrations row's ``config_json`` (it is not a secret;
the bot token stays in the environment).
"""

import base64
import hashlib
import hmac
import os
import secrets as _secrets
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import integrations
from database import get_db

router = APIRouter()

API = "https://api.telegram.org"
TIMEOUT = 10


def bot_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN") or None


def bot_link() -> str | None:
    username = (os.getenv("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    return f"https://t.me/{username}" if username else None


_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
MIN_SECRET_LENGTH = 16          # shorter than this, a shared secret is treated as not set


def webhook_secret() -> str | None:
    """TELEGRAM_WEBHOOK_SECRET, or None when it is unset or too short to be one. Whoever
    holds it can speak as the paired chat, so a guessable value is no secret at all."""
    value = os.getenv("TELEGRAM_WEBHOOK_SECRET") or ""
    return value if len(value) >= MIN_SECRET_LENGTH else None


def pair_code(db: Session) -> str:
    """The code to send the bot: six random base32 characters kept on the telegram row.

    Random, not derived from the server secret or the password, so seeing one tells nothing
    about either. Spent when it is used: pairing and disconnecting both discard it, and the
    next call makes a new one. The caller need not commit."""
    row = _row(db)
    code = integrations.config(row).get("pair_code")
    if not code:
        code = "".join(_secrets.choice(_CODE_ALPHABET) for _ in range(6))
        integrations.set_config(row, pair_code=code)
        db.commit()
    return code


def _row(db: Session):
    return integrations.get_or_create(db, "telegram")


def chat_id(db: Session):
    return integrations.config(_row(db)).get("chat_id")


def is_paired(db: Session) -> bool:
    return chat_id(db) is not None


def pair(db: Session, chat, username: str | None = None) -> None:
    """Store the chat that sent the right code. The caller commits."""
    row = _row(db)
    integrations.set_config(row, chat_id=chat, username=username, pair_code=None)    # the code is spent
    row.status, row.account_label, row.last_error = "connected", username, None
    row.connected_at = datetime.utcnow()


def unpair(db: Session) -> None:
    row = _row(db)
    row.config_json, row.status, row.account_label = None, "disconnected", None
    row.updated_at = datetime.utcnow()


@router.post("/integrations/telegram/set-webhook")
def set_webhook(db: Session = Depends(get_db)):
    """Tell Telegram where to deliver messages, with the secret it must send back in the
    ``X-Telegram-Bot-Api-Secret-Token`` header. 503 until the three env vars are set."""
    token, base, secret = bot_token(), os.getenv("FOUNDER_OS_API_URL"), webhook_secret()
    missing = [name for name, value in (("TELEGRAM_BOT_TOKEN", token), ("FOUNDER_OS_API_URL", base),
                                        ("TELEGRAM_WEBHOOK_SECRET", os.getenv("TELEGRAM_WEBHOOK_SECRET"))) if not value]
    if missing:
        raise HTTPException(503, f"Not set: {', '.join(missing)}")
    if secret is None:
        raise HTTPException(503, f"TELEGRAM_WEBHOOK_SECRET must be at least {MIN_SECRET_LENGTH} characters")
    url = base.rstrip("/") + "/integrations/telegram/webhook"
    try:
        # VERIFY: setWebhook's parameter names (url, secret_token, allowed_updates).
        resp = httpx.post(f"{API}/bot{token}/setWebhook",
                          json={"url": url, "secret_token": secret, "allowed_updates": ["message"]},
                          timeout=TIMEOUT)
        data = resp.json()
    except Exception as exc:                     # noqa: BLE001 — report it, redacted
        integrations.log(f"[telegram] setWebhook failed: {type(exc).__name__}: {exc}", db)
        return {"ok": False, "description": integrations.redact(f"Telegram could not be reached ({type(exc).__name__})", db)}
    ok = bool(data.get("ok"))
    description = integrations.redact(data.get("description") or ("Webhook set" if ok else "Telegram refused"), db)
    row = _row(db)
    integrations.set_config(row, webhook_url=url if ok else None)
    if not ok:
        row.last_error = description[:500]
    db.commit()
    return {"ok": ok, "description": description, "url": url}


@router.post("/integrations/telegram/unpair")
def unpair_endpoint(db: Session = Depends(get_db)):
    unpair(db)
    db.commit()
    return integrations.serialize(db, _row(db))
