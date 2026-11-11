"""
Personal-brand analytics (Phase 5D).

Endpoints: GET/POST /brand/accounts, POST /brand/accounts/{id}/sync,
           DELETE /brand/accounts/{id}, POST /brand/import

Modules:
  adapters  — YouTube Data API (API key) and the LinkedIn analytics export
  sync      — sync(db, account), import_export(db, platform, text, filename)

Every number shown comes from the platform itself. Where a platform is not connected the
answer is "not connected"; where it gives no number the value is null. Nothing is
estimated, and nothing is typed in by hand.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

import change_log
import models
from brand import adapters
from brand import sync as _sync
from database import get_db

router = APIRouter()


def serialize_account(db: Session, a: models.SocialAccount) -> dict:
    latest = _sync.latest_account_metric(db, a.id)
    month_ago = datetime.utcnow() - timedelta(days=30)
    earlier = db.query(models.AccountMetric).filter(
        models.AccountMetric.account_id == a.id, models.AccountMetric.captured_at <= month_ago,
    ).order_by(models.AccountMetric.captured_at.desc()).first()
    followers = latest.followers if latest else None
    delta = (followers - earlier.followers) if (followers is not None and earlier is not None
                                                and earlier.followers is not None) else None
    return {
        "id": a.id, "platform": a.platform, "handle": a.handle, "external_id": a.external_id,
        "access": a.access, "status": a.status, "last_synced_at": a.last_synced_at,
        "last_error": a.last_error, "created_at": a.created_at, "updated_at": a.updated_at,
        "followers": followers, "followers_delta_30d": delta,
        "following": latest.following if latest else None,
        "posts_count": latest.posts_count if latest else None,
        "total_views": latest.total_views if latest else None,
        "metrics_captured_at": latest.captured_at if latest else None,
        "posts_stored": db.query(models.ContentPost).filter(models.ContentPost.account_id == a.id).count(),
    }


class AccountCreate(BaseModel):
    platform: str
    handle: str | None = None
    external_id: str | None = None


def _account_or_404(db: Session, account_id: int) -> models.SocialAccount:
    account = db.query(models.SocialAccount).filter(models.SocialAccount.id == account_id).first()
    if account is None:
        raise HTTPException(404, "Account not found")
    return account


@router.get("/brand/accounts")
def list_accounts(db: Session = Depends(get_db)):
    rows = db.query(models.SocialAccount).order_by(models.SocialAccount.id).all()
    return [serialize_account(db, a) for a in rows]


@router.post("/brand/accounts", status_code=201)
def connect_account(payload: AccountCreate, db: Session = Depends(get_db)):
    """Connect a platform that has an API adapter (YouTube today). The handle is resolved
    to the channel id now; a failure is a 400 carrying the adapter's message."""
    platform = (payload.platform or "").strip().lower()
    if platform not in adapters.API_ADAPTERS:
        if platform in adapters.EXPORT_ADAPTERS:
            raise HTTPException(400, f"{platform} is connected by importing its analytics export")
        raise HTTPException(400, f"{platform or 'that platform'} is not supported yet")
    adapter = adapters.API_ADAPTERS[platform]()
    try:
        resolved = adapter.resolve_channel(payload.external_id or payload.handle or "")
    except adapters.AdapterError as exc:
        raise HTTPException(400, str(exc))
    now = datetime.utcnow()
    with change_log.batch_scope(db, "ui"):
        account = db.query(models.SocialAccount).filter(
            models.SocialAccount.platform == platform,
            models.SocialAccount.external_id == resolved["external_id"]).first()
        if account is None:
            account = models.SocialAccount(platform=platform, handle=resolved["handle"],
                                           external_id=resolved["external_id"], access="api_key",
                                           status="connected", created_at=now, updated_at=now)
            db.add(account)
            change = change_log.record_create(db, account, f"Connected {platform} {resolved['handle']}")
        else:
            before = change_log.snapshot(account)
            account.status, account.handle, account.last_error, account.updated_at = "connected", resolved["handle"], None, now
            change = change_log.record_update(db, account, before, f"Reconnected {platform} {resolved['handle']}")
        db.commit()
    return {**serialize_account(db, account), "change": change}


@router.post("/brand/accounts/{account_id}/sync")
def sync_account(account_id: int, db: Session = Depends(get_db)):
    account = _account_or_404(db, account_id)
    with change_log.batch_scope(db, "ui"):
        result = _sync.sync(db, account)
        changes = change_log.current_batch_changes(db)
        db.commit()
    return {**result, "account": serialize_account(db, account), "changes": changes}


@router.delete("/brand/accounts/{account_id}")
def disconnect_account(account_id: int, db: Session = Depends(get_db)):
    """Disconnect: the account stops syncing. Nothing is deleted; its posts and numbers stay."""
    account = _account_or_404(db, account_id)
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(account)
        account.status, account.updated_at = "disconnected", datetime.utcnow()
        change = change_log.record_update(db, account, before, f"Disconnected {account.platform} {account.handle or ''}".strip())
        db.commit()
    return {**serialize_account(db, account), "change": change}


@router.post("/brand/import")
async def import_file(file: UploadFile = File(...), platform: str = Form(...), db: Session = Depends(get_db)):
    """Multipart: file (the CSV saved from the platform's analytics export), platform."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    with change_log.batch_scope(db, "ui"):
        try:
            result = _sync.import_export(db, (platform or "").strip().lower(), text, file.filename)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(400, str(exc))
        db.commit()
    return result
