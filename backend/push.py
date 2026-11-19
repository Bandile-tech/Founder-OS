"""
Web Push: the first channel Founder OS uses to reach Bandile when the app is closed
(Phase 7, Stage 4).

  GET  /push/config        whether the server has VAPID keys, the public key, how many devices
  POST /push/subscribe     this device wants notifications (the browser's PushSubscription)
  POST /push/unsubscribe   this device no longer does

A device subscribes from Settings; the browser hands back an endpoint at its push service
and two keys. A notification is encrypted for that device and posted to that endpoint,
signed with the server's VAPID key. The service worker (``frontend/sw.js``) shows it, and
a tap opens the app on the matching view.

Nothing sends from here on its own: ``notify.dispatch`` is the only caller of ``send``.

Needs ``VAPID_PUBLIC_KEY`` and ``VAPID_PRIVATE_KEY`` (``python tools/vapid_keys.py``
makes a pair) and optionally ``VAPID_SUBJECT`` (a mailto: or https: address the push
service may contact). Without the keys nothing is sent and Settings says so.
"""

import json
import os
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import integrations
import models
from database import get_db

router = APIRouter()

TTL_SECONDS = 12 * 3600           # a push service keeps a message this long for a phone that is off
TIMEOUT = 10
MAX_BODY = 600                    # the encrypted payload must stay under about 4 KB
MAX_FAILURES = 5                  # a device that fails this many times in a row is dropped
# Only real push services are ever posted to: an endpoint is somewhere this server sends
# requests, so it is not taken on trust.
_PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com", "push.apple.com")


def public_key() -> str | None:
    return (os.getenv("VAPID_PUBLIC_KEY") or "").strip() or None


def _private_key() -> str | None:
    return (os.getenv("VAPID_PRIVATE_KEY") or "").strip() or None


def subject() -> str:
    given = (os.getenv("VAPID_SUBJECT") or "").strip()
    if given:
        return given
    base = (os.getenv("FOUNDER_OS_API_URL") or "").strip()
    return base if base.startswith("https://") else "mailto:founder-os@users.noreply.github.com"


def configured() -> bool:
    return bool(public_key() and _private_key())


def count(db: Session) -> int:
    return db.query(models.PushSubscription).count()


def ready(db: Session) -> bool:
    """Can a notification reach a device right now?"""
    return configured() and count(db) > 0


def _allowed(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in _PUSH_HOSTS)


def subscribe(db: Session, endpoint: str, p256dh: str, auth: str, user_agent: str | None = None) -> models.PushSubscription:
    if not _allowed(endpoint):
        raise ValueError("that is not a push service this app sends to")
    if not p256dh or not auth:
        raise ValueError("the subscription has no keys")
    row = db.query(models.PushSubscription).filter(models.PushSubscription.endpoint == endpoint).first()
    if row is None:
        row = models.PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth, failures=0,
                                      created_at=datetime.utcnow())
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:                   # two taps at once: the other one made the row
            row = db.query(models.PushSubscription).filter(models.PushSubscription.endpoint == endpoint).first()
            if row is None:
                raise
    row.p256dh, row.auth, row.failures, row.last_error = p256dh, auth, 0, None
    row.user_agent = (user_agent or "")[:200] or None
    db.commit()
    return row


def unsubscribe(db: Session, endpoint: str) -> bool:
    removed = db.query(models.PushSubscription).filter(models.PushSubscription.endpoint == endpoint).delete()
    db.commit()
    return bool(removed)


def _deliver(subscription: dict, payload: str) -> None:
    """Encrypt and post one message. Raises ``pywebpush.WebPushException`` when the push
    service refuses it. Tests patch this function."""
    from pywebpush import webpush
    webpush(subscription_info=subscription, data=payload, vapid_private_key=_private_key(),
            vapid_claims={"sub": subject()}, ttl=TTL_SECONDS, timeout=TIMEOUT)


def send(db: Session, *, title: str, body: str, url: str, tag: str | None = None) -> dict:
    """Send one notification to every subscribed device. ``ok`` when at least one device
    took it. A device whose subscription has ended (404 or 410) is forgotten. Never raises."""
    if not configured():
        return {"ok": False, "sent": 0, "error": "VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are not set"}
    rows = db.query(models.PushSubscription).order_by(models.PushSubscription.id).all()
    if not rows:
        return {"ok": False, "sent": 0, "error": "no device has notifications turned on"}
    payload = json.dumps({"title": str(title or "Founder OS")[:120], "body": str(body or "")[:MAX_BODY],
                          "url": url, "tag": tag or None})
    sent, errors = 0, []
    for row in rows:
        try:
            _deliver({"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}}, payload)
        except Exception as exc:  # noqa: BLE001 — one device must not stop the others
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                db.delete(row)                   # the browser dropped this subscription
                errors.append("a device is no longer subscribed")
                continue
            row.failures = (row.failures or 0) + 1
            row.last_error = integrations.redact(f"{type(exc).__name__}"
                                                 + (f" (HTTP {status})" if status else ""), db)[:200]
            errors.append(row.last_error)
            if row.failures >= MAX_FAILURES:
                db.delete(row)
            continue
        row.failures, row.last_error, row.last_success_at = 0, None, datetime.utcnow()
        sent += 1
    db.commit()
    return {"ok": sent > 0, "sent": sent, "error": None if sent else "; ".join(dict.fromkeys(errors))[:300]}


# ── Endpoints ────────────────────────────────────────────────

class Keys(BaseModel):
    p256dh: str
    auth: str


class Subscription(BaseModel):
    endpoint: str
    keys: Keys


class Endpoint(BaseModel):
    endpoint: str


def _state(db: Session) -> dict:
    return {"configured": configured(), "public_key": public_key(), "subscriptions": count(db)}


@router.get("/push/config")
def push_config(db: Session = Depends(get_db)):
    return _state(db)


@router.post("/push/subscribe")
def push_subscribe(payload: Subscription, request: Request, db: Session = Depends(get_db)):
    if not configured():
        raise HTTPException(503, "VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are not set on the server.")
    try:
        subscribe(db, payload.endpoint, payload.keys.p256dh, payload.keys.auth, request.headers.get("user-agent"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"subscribed": True, **_state(db)}


@router.post("/push/unsubscribe")
def push_unsubscribe(payload: Endpoint, db: Session = Depends(get_db)):
    unsubscribe(db, payload.endpoint)
    return {"subscribed": False, **_state(db)}
