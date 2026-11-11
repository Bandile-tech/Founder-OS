"""
Personal-brand analytics (Phase 5D).

Endpoints: GET/POST /brand/accounts, POST /brand/accounts/{id}/sync,
           DELETE /brand/accounts/{id}, POST /brand/import, GET /brand/performance,
           GET /brand/posts, PATCH /brand/posts/{id}, GET/POST /brand/ideas,
           PATCH /brand/ideas/{id}, POST /brand/ideas/{id}/advance, GET /brand/calendar,
           GET /brand/insight

Modules:
  adapters  — YouTube Data API (API key) and the LinkedIn analytics export
  sync      — sync(db, account), import_export(db, platform, text, filename)
  analytics — performance(), insight() (cached per platform per week), progress()

Every number shown comes from the platform itself. Where a platform is not connected the
answer is "not connected"; where it gives no number the value is null. Nothing is
estimated, and nothing is typed in by hand.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
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


# ════════════════════════════════════════════════════════════════
# Performance, posts, ideas, the calendar, the weekly insight
# ════════════════════════════════════════════════════════════════

from datetime import date

from brand import analytics

IDEA_STATUSES = ("idea", "drafted", "scheduled", "posted", "dropped")
IDEA_CYCLE = ("idea", "drafted", "scheduled", "posted")
FORMATS = ("short", "video", "post", "article", "thread")


def serialize_idea(i: models.ContentIdea) -> dict:
    return {"id": i.id, "title": i.title, "angle": i.angle, "platform": i.platform, "format": i.format,
            "status": i.status, "scheduled_for": i.scheduled_for, "posted_post_id": i.posted_post_id,
            "source": i.source, "score": i.score, "created_at": i.created_at, "updated_at": i.updated_at}


def find_idea(db: Session, ref):
    """(idea | None, candidates) — an id, or a fuzzy match on the title."""
    import business_common
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        idea = db.query(models.ContentIdea).filter(models.ContentIdea.id == int(ref)).first()
        return idea, ([(1.0, idea)] if idea else [])
    rows = db.query(models.ContentIdea).filter(models.ContentIdea.status.notin_(("posted", "dropped"))).all()
    return business_common.fuzzy_pick(rows, lambda i: i.title, str(ref or ""))


def create_idea(db: Session, title: str, *, angle: str | None = None, platform: str | None = None,
                format: str | None = None, scheduled_for: date | None = None,
                source: str = "conversation") -> dict:
    """A new idea. Giving it a date schedules it. The caller commits."""
    title = (title or "").strip()
    if not title:
        raise ValueError("an idea needs a title")
    idea = models.ContentIdea(
        title=title[:300], angle=(angle or "").strip() or None,
        platform=(platform or "").strip().lower() or None,
        format=format if format in FORMATS else None,
        status="scheduled" if scheduled_for else "idea", scheduled_for=scheduled_for, source=source,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(idea)
    change = change_log.record_create(db, idea, f"Content idea: {title[:60]}")
    return {**serialize_idea(idea), "change": change}


def update_idea(db: Session, idea: models.ContentIdea, *, title: str | None = None, angle: str | None = None,
                platform: str | None = None, format: str | None = None, status: str | None = None,
                scheduled_for: date | None = None, score: int | None = None,
                posted_post_id: int | None = None) -> dict:
    """Change an idea. A date on an idea that is not yet posted schedules it."""
    if status is not None and status not in IDEA_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(IDEA_STATUSES)}")
    before = change_log.snapshot(idea)
    if title is not None and title.strip():
        idea.title = title.strip()[:300]
    if angle is not None:
        idea.angle = angle.strip() or None
    if platform is not None:
        idea.platform = platform.strip().lower() or None
    if format in FORMATS:
        idea.format = format
    if score is not None:
        idea.score = max(1, min(5, int(score)))
    if posted_post_id is not None:
        idea.posted_post_id = posted_post_id
    if scheduled_for is not None:
        idea.scheduled_for = scheduled_for
        if status is None and idea.status in ("idea", "drafted"):
            idea.status = "scheduled"
    if status is not None:
        if status == "scheduled" and idea.scheduled_for is None:
            raise ValueError("scheduling needs a date")
        idea.status = status
    if change_log.snapshot(idea) != before:
        idea.updated_at = datetime.utcnow()
    change = change_log.record_update(db, idea, before, f"Content idea '{idea.title[:40]}' → {idea.status}")
    return {**serialize_idea(idea), "change": change}


def advance_idea(db: Session, idea: models.ContentIdea) -> dict:
    """One tap: idea → drafted → scheduled → posted. Scheduling needs a date."""
    if idea.status not in IDEA_CYCLE or idea.status == "posted":
        raise ValueError(f"an idea that is {idea.status} cannot be advanced")
    nxt = IDEA_CYCLE[IDEA_CYCLE.index(idea.status) + 1]
    if nxt == "scheduled" and idea.scheduled_for is None:
        raise LookupError("scheduling needs a date")
    return update_idea(db, idea, status=nxt)


def mark_posted(db: Session, idea: models.ContentIdea, url: str | None = None) -> dict:
    """The idea was published. When ``url`` is a post already on file, the two are linked."""
    changes = []
    post = None
    if url and url.strip():
        post = db.query(models.ContentPost).filter(models.ContentPost.url == url.strip()).first()
    result = update_idea(db, idea, status="posted", posted_post_id=post.id if post else None)
    if result.get("change"):
        changes.append(result["change"])
    if post is not None and post.idea_id != idea.id:
        before = change_log.snapshot(post)
        post.idea_id, post.updated_at = idea.id, datetime.utcnow()
        changes.append(change_log.record_update(db, post, before, f"Linked post to idea '{idea.title[:40]}'"))
    return {**serialize_idea(idea), "linked_post_id": post.id if post else None,
            "change": result.get("change"), "changes": [c for c in changes if c]}


def calendar(db: Session, start: date | None = None, end: date | None = None) -> dict:
    """Ideas by scheduled day and posts by publishing day. Default: this week and next."""
    today = date.today()
    start = start or (today - timedelta(days=today.weekday()))
    end = end or (start + timedelta(days=13))
    if end < start:
        start, end = end, start
    end = min(end, start + timedelta(days=62))
    ideas = db.query(models.ContentIdea).filter(
        models.ContentIdea.scheduled_for >= start, models.ContentIdea.scheduled_for <= end,
        models.ContentIdea.status != "dropped").all()
    # Posts are stored in UTC; the calendar is in the user's days. Fetch a day either side
    # and place each post on its local date.
    from agents import schedule
    lo = datetime(start.year, start.month, start.day) - timedelta(days=1)
    hi = datetime(end.year, end.month, end.day) + timedelta(days=2)
    posts = db.query(models.ContentPost).filter(models.ContentPost.posted_at >= lo, models.ContentPost.posted_at < hi).all()
    local_day = {p.id: schedule.to_local(p.posted_at).date() for p in posts}
    days = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        days.append({
            "date": day, "is_today": day == today,
            "ideas": [serialize_idea(i) for i in ideas if i.scheduled_for == day],
            "posted": [{"id": p.id, "title": p.title, "url": p.url, "kind": p.kind}
                       for p in posts if local_day[p.id] == day],
        })
    unscheduled = db.query(models.ContentIdea).filter(
        models.ContentIdea.status.in_(("idea", "drafted")), models.ContentIdea.scheduled_for.is_(None),
    ).order_by(models.ContentIdea.id.desc()).limit(30).all()
    return {"from": start, "to": end, "days": days, "backlog": [serialize_idea(i) for i in unscheduled]}


class PostPatch(BaseModel):
    topic_tags: list[str] | None = None
    idea_id: int | None = None


class IdeaCreate(BaseModel):
    title: str
    angle: str | None = None
    platform: str | None = None
    format: str | None = None
    scheduled_for: date | None = None


class IdeaPatch(BaseModel):
    title: str | None = None
    angle: str | None = None
    platform: str | None = None
    format: str | None = None
    status: str | None = None
    scheduled_for: date | None = None
    score: int | None = None
    posted_post_id: int | None = None


def _idea_or_404(db: Session, idea_id: int) -> models.ContentIdea:
    idea = db.query(models.ContentIdea).filter(models.ContentIdea.id == idea_id).first()
    if idea is None:
        raise HTTPException(404, "Idea not found")
    return idea


@router.get("/brand/performance")
def get_performance(platform: str | None = None, days: int = 90, db: Session = Depends(get_db)):
    """The performance summary plus the cached weekly insight (never generated here)."""
    data = analytics.performance(db, platform, days)
    row = analytics.cached_insight(db, platform)
    return {**data, "insight": row.text if row else None, "brand_target_posts_30d": analytics.TARGET_POSTS_30D,
            "posts_last_30d": analytics.posts_last_30d(db) if data["connected"] else None}


@router.get("/brand/posts")
def list_posts(platform: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
    accounts = {a.id: a for a in analytics._accounts(db, platform)}
    if not accounts:
        return []
    rows = db.query(models.ContentPost).filter(models.ContentPost.account_id.in_(list(accounts))).all()
    rows.sort(key=lambda p: (p.posted_at is None, -(p.posted_at.timestamp() if p.posted_at else 0), -p.id))
    return [analytics.serialize_post(db, p, accounts[p.account_id]) for p in rows[:max(1, min(int(limit), 200))]]


@router.patch("/brand/posts/{post_id}")
def patch_post(post_id: int, payload: PostPatch, db: Session = Depends(get_db)):
    post = db.query(models.ContentPost).filter(models.ContentPost.id == post_id).first()
    if post is None:
        raise HTTPException(404, "Post not found")
    data = payload.model_dump(exclude_unset=True)
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(post)
        if "topic_tags" in data:
            post.topic_tags = ",".join(t.strip() for t in (data["topic_tags"] or []) if t and t.strip()) or None
        if "idea_id" in data:
            if data["idea_id"] is not None:
                _idea_or_404(db, data["idea_id"])
            post.idea_id = data["idea_id"]
        post.updated_at = datetime.utcnow()
        change = change_log.record_update(db, post, before, f"Updated post: {(post.title or post.url or '')[:60]}")
        db.commit()
    return {**analytics.serialize_post(db, post), "change": change}


@router.get("/brand/ideas")
def list_ideas(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(models.ContentIdea)
    if status:
        q = q.filter(models.ContentIdea.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    rows = q.all()
    rows.sort(key=lambda i: (i.scheduled_for is None, i.scheduled_for or date.max, -i.id))
    return [serialize_idea(i) for i in rows]


@router.post("/brand/ideas", status_code=201)
def post_idea(payload: IdeaCreate, db: Session = Depends(get_db)):
    with change_log.batch_scope(db, "ui"):
        try:
            result = create_idea(db, payload.title, angle=payload.angle, platform=payload.platform,
                                 format=payload.format, scheduled_for=payload.scheduled_for, source="ui")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.patch("/brand/ideas/{idea_id}")
def patch_idea(idea_id: int, payload: IdeaPatch, db: Session = Depends(get_db)):
    idea = _idea_or_404(db, idea_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = update_idea(db, idea, **payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.post("/brand/ideas/{idea_id}/advance")
def advance_idea_endpoint(idea_id: int, db: Session = Depends(get_db)):
    """One tap. 409 when the next step is `scheduled` and the idea has no date yet."""
    idea = _idea_or_404(db, idea_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = advance_idea(db, idea)
        except LookupError as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.get("/brand/calendar")
def get_calendar(from_: date | None = Query(None, alias="from"), to: date | None = None,
                 db: Session = Depends(get_db)):
    return calendar(db, from_, to)


@router.get("/brand/insight")
def get_insight(platform: str | None = None, refresh: int = 0, db: Session = Depends(get_db)):
    return analytics.insight(db, platform, refresh=bool(refresh))
