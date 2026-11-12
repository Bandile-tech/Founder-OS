"""
Integrations — the external systems the day runs on (Phase 6C).

Primitives: get_or_create, redact, serialize, configured, listing
Endpoints:  GET /integrations

One row per provider (google_calendar, email, telegram, youtube). Tokens that a provider
hands back at runtime live in ``secret_json``; static tokens stay in environment variables.
**``secret_json`` is never serialised by any endpoint**, and every log line or stored error
that could carry a credential goes through ``redact()`` first.
"""

import json
import os
import re
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from database import get_db

router = APIRouter()

PROVIDERS = ("google_calendar", "email", "telegram", "youtube")
LABELS = {"google_calendar": "Google Calendar", "email": "Email", "telegram": "Telegram", "youtube": "YouTube"}
REDACTED = "***"

# Env vars whose values must never appear in a log line or a stored error.
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "EMAIL_APP_PASSWORD", "GOOGLE_CLIENT_SECRET", "JOBS_TOKEN",
              "YOUTUBE_API_KEY", "TELEGRAM_WEBHOOK_SECRET", "OPENAI_API_KEY", "FOUNDER_OS_SECRET",
              "FOUNDER_OS_PASSWORD")
_BOT_PATH = re.compile(r"bot[0-9]+:[A-Za-z0-9_-]+")
_MIN_SECRET_LEN = 6            # shorter values are too likely to be ordinary words


# ── Rows ─────────────────────────────────────────────────────

def _find(db: Session, provider: str):
    return db.query(models.Integration).filter(models.Integration.provider == provider).first()


def get_or_create(db: Session, provider: str) -> models.Integration:
    """The provider's row, created on first use. Safe when two requests arrive together:
    the loser of the race on the unique ``provider`` reads the winner's row."""
    row = _find(db, provider)
    if row is None:
        row = models.Integration(provider=provider, status="disconnected",
                                 created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = _find(db, provider)
            if row is None:
                raise
        else:
            db.refresh(row)
    return row


def config(row: models.Integration) -> dict:
    try:
        data = json.loads(row.config_json) if row.config_json else {}
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def secrets(row: models.Integration) -> dict:
    try:
        data = json.loads(row.secret_json) if row.secret_json else {}
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def set_config(row: models.Integration, **values) -> None:
    data = config(row)
    data.update(values)
    row.config_json = json.dumps({k: v for k, v in data.items() if v is not None}, default=str)
    row.updated_at = datetime.utcnow()


def set_error(db: Session, row: models.Integration, message) -> None:
    """Mark a provider as failing. The message is redacted before it is stored."""
    row.status, row.last_error, row.updated_at = "error", redact(message, db)[:500], datetime.utcnow()


# ── Redaction ────────────────────────────────────────────────

def _secret_values(db: Session | None) -> set[str]:
    values = {os.getenv(name) for name in SECRET_ENV}
    if db is not None:
        try:
            for row in db.query(models.Integration).all():
                stack = list(secrets(row).values())
                while stack:
                    item = stack.pop()
                    if isinstance(item, dict):
                        stack.extend(item.values())
                    elif isinstance(item, (list, tuple)):
                        stack.extend(item)
                    elif isinstance(item, str):
                        values.add(item)
        except Exception:                        # noqa: BLE001 — redaction must never fail a caller
            pass
    return {v for v in values if v and len(v) >= _MIN_SECRET_LEN}


def redact(text, db: Session | None = None) -> str:
    """``text`` with every configured secret replaced by ``***``: the env tokens, every
    value in any row's ``secret_json`` (when ``db`` is given), and anything shaped like a
    Telegram bot token in a URL path. Used for every log line and stored error."""
    out = str(text)
    for value in sorted(_secret_values(db), key=len, reverse=True):
        out = out.replace(value, REDACTED)
    return _BOT_PATH.sub("bot" + REDACTED, out)


def log(message, db: Session | None = None) -> None:
    print(redact(message, db))


# ── What each provider needs, and what can be done with it ──

def configured(provider: str) -> bool:
    """Are the environment variables the provider needs present?"""
    env = os.getenv
    if provider == "google_calendar":
        return bool(env("GOOGLE_CLIENT_ID") and env("GOOGLE_CLIENT_SECRET") and env("FOUNDER_OS_API_URL"))
    if provider == "email":
        return bool(env("EMAIL_ADDRESS") and env("EMAIL_APP_PASSWORD"))
    if provider == "telegram":
        return bool(env("TELEGRAM_BOT_TOKEN"))
    if provider == "youtube":
        return bool(env("YOUTUBE_API_KEY"))
    return False


def _youtube_account(db: Session):
    """The Brand tab's YouTube account, which is where that connection really lives."""
    if not hasattr(models, "SocialAccount"):
        return None
    return db.query(models.SocialAccount).filter(
        models.SocialAccount.platform == "youtube", models.SocialAccount.status != "disconnected").first()


def _effective(db: Session, row: models.Integration) -> tuple[str, str | None, list[str]]:
    """(status, account label, actions) as the Settings card should show them."""
    provider, ready = row.provider, configured(row.provider)
    if provider == "google_calendar":
        if row.status == "connected":
            return "connected", row.account_label, ["sync", "disconnect"]
        if row.status == "error":
            return "error", row.account_label, (["connect"] if ready else []) + ["disconnect"]
        return "disconnected", None, ["connect"] if ready else []
    if provider == "email":
        if not ready:
            return "disconnected", None, []
        status = row.status if row.status in ("connected", "error") else "pending"
        return status, os.getenv("EMAIL_ADDRESS"), ["sync"]
    if provider == "telegram":
        chat = config(row).get("chat_id")
        if chat:
            return "connected", config(row).get("username") or row.account_label, ["set_webhook", "disconnect"]
        return ("pending" if ready else "disconnected"), None, (["pair", "set_webhook"] if ready else [])
    if provider == "youtube":
        account = _youtube_account(db)
        if account is not None:
            return ("error" if account.status == "error" else "connected"), account.handle, []
        return ("pending" if ready else "disconnected"), None, []
    return row.status, row.account_label, []


def serialize(db: Session, row: models.Integration) -> dict:
    """Never includes ``secret_json``."""
    status, label, actions = _effective(db, row)
    last_error, last_sync = row.last_error, row.last_sync_at
    if row.provider == "youtube":
        account = _youtube_account(db)
        if account is not None:
            last_error, last_sync = (redact(account.last_error, db) if account.last_error else None), account.last_synced_at
    return {"provider": row.provider, "label": LABELS.get(row.provider, row.provider), "status": status,
            "account_label": label, "connected_at": row.connected_at, "last_sync_at": last_sync,
            "last_error": last_error if status == "error" else None,
            "configured": configured(row.provider), "actions": actions}


def listing(db: Session) -> list[dict]:
    return [serialize(db, get_or_create(db, provider)) for provider in PROVIDERS]


def statuses(db: Session) -> dict:
    """provider → status, for the briefing's connection warnings."""
    return {item["provider"]: item["status"] for item in listing(db)}


@router.get("/integrations")
def list_integrations(db: Session = Depends(get_db)):
    return listing(db)
