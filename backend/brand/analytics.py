"""
Brand analytics — deterministic, from stored platform data only (Phase 5D).

  performance(db, platform=None, days=90)   accounts, top posts, formats, cadence, trend
  insight(db, platform, refresh=False)      one cached model call per platform per ISO week
  posts_last_30d(db), progress(db)          the brand progress bar

"No data" is ``None``, never 0: a post whose platform gave no view count has ``views:
None``, and a rate that cannot be computed is ``None``. Nothing here estimates.
"""

import json
from datetime import date, datetime, timedelta
from statistics import median

import llm
import models
from brand import sync as brand_sync

TARGET_POSTS_30D = 8
TREND_WEEKS = 12
INSIGHT_MAX_TOKENS = 250

INSIGHT_PROMPT = (
    "You read one creator's content numbers and write a short weekly note for him. Three or "
    "four plain sentences: what worked, what did not, and one experiment to try next week. "
    "Use only the numbers given. Where a number is null it was not measured: say nothing "
    "about it and never guess it. No lists, no emojis, no hype."
)


def _accounts(db, platform: str | None = None) -> list:
    q = db.query(models.SocialAccount).filter(models.SocialAccount.status != "disconnected")
    if platform:
        q = q.filter(models.SocialAccount.platform == platform)
    return q.order_by(models.SocialAccount.id).all()


def _sum_known(values) -> int | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _median(values) -> float | None:
    known = [v for v in values if v is not None]
    return round(float(median(known)), 4) if known else None


def post_numbers(db, post: models.ContentPost) -> dict:
    """The latest snapshot of a post as measured values (None where not measured), plus the
    derived engagement and engagement rate."""
    m = brand_sync.latest_post_metric(db, post.id)
    values = {k: (getattr(m, k) if m is not None else None)
              for k in ("views", "likes", "comments", "shares", "saves", "impressions", "watch_time_s")}
    engagement = _sum_known([values["likes"], values["comments"], values["shares"], values["saves"]])
    reach = values["views"] if values["views"] is not None else values["impressions"]
    rate = round(engagement / max(reach, 1), 4) if (engagement is not None and reach is not None) else None
    return {**values, "engagement": engagement, "engagement_rate": rate,
            "captured_at": m.captured_at if m is not None else None}


def serialize_post(db, post: models.ContentPost, account: models.SocialAccount | None = None) -> dict:
    if account is None:
        account = db.query(models.SocialAccount).filter(models.SocialAccount.id == post.account_id).first()
    return {"id": post.id, "account_id": post.account_id, "platform": account.platform if account else None,
            "external_id": post.external_id, "url": post.url, "title": post.title, "kind": post.kind,
            "posted_at": post.posted_at, "idea_id": post.idea_id,
            "topic_tags": [t.strip() for t in (post.topic_tags or "").split(",") if t.strip()],
            **post_numbers(db, post)}


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def performance(db, platform: str | None = None, days: int = 90) -> dict:
    """What the stored platform data says. Read-only."""
    from brand import serialize_account
    days = max(1, int(days or 90))
    accounts = _accounts(db, platform)
    now = datetime.utcnow()
    since = now - timedelta(days=days)
    by_account = {a.id: a for a in accounts}
    posts = (db.query(models.ContentPost).filter(models.ContentPost.account_id.in_(list(by_account))).all()
             if by_account else [])
    rows = [serialize_post(db, p, by_account[p.account_id]) for p in posts]
    window = [r for r in rows if r["posted_at"] is not None and r["posted_at"] >= since]

    ranked = sorted((r for r in window if r["engagement_rate"] is not None or r["views"] is not None),
                    key=lambda r: (-(r["engagement_rate"] if r["engagement_rate"] is not None else -1),
                                   -(r["views"] if r["views"] is not None else -1), r["id"]))
    top = [{"post": {k: r[k] for k in ("id", "platform", "title", "url", "kind")}, "views": r["views"],
            "impressions": r["impressions"], "engagement": r["engagement"],
            "engagement_rate": r["engagement_rate"], "posted_at": r["posted_at"]} for r in ranked[:5]]

    by_format = {}
    for kind in sorted({r["kind"] or "post" for r in window}):
        group = [r for r in window if (r["kind"] or "post") == kind]
        by_format[kind] = {"count": len(group), "median_views": _median([r["views"] for r in group]),
                           "median_engagement_rate": _median([r["engagement_rate"] for r in group])}

    dated = [r["posted_at"] for r in rows if r["posted_at"] is not None]
    cadence = {"posts_in_window": len(window), "per_week": round(len(window) / (days / 7), 2),
               "gap_days_since_last": (now - max(dated)).days if dated else None}

    this_week = _week_start(now.date())
    trend = []
    for back in range(TREND_WEEKS - 1, -1, -1):
        start = this_week - timedelta(weeks=back)
        group = [r for r in rows if r["posted_at"] is not None and _week_start(r["posted_at"].date()) == start]
        trend.append({"week_start": start, "posts": len(group),
                      "views": _sum_known([r["views"] for r in group]),
                      "engagement": _sum_known([r["engagement"] for r in group])})

    return {
        "connected": bool(accounts), "platform": platform, "days": days,
        "accounts": [{k: a[k] for k in ("id", "platform", "handle", "followers", "followers_delta_30d",
                                        "last_synced_at", "status", "last_error")}
                     for a in (serialize_account(db, acc) for acc in accounts)],
        "top_posts": top, "by_format": by_format, "cadence": cadence, "trend": trend,
    }


def posts_last_30d(db) -> int:
    since = datetime.utcnow() - timedelta(days=30)
    ids = [a.id for a in _accounts(db)]
    if not ids:
        return 0
    return db.query(models.ContentPost).filter(
        models.ContentPost.account_id.in_(ids), models.ContentPost.posted_at >= since).count()


def progress(db) -> int | None:
    """Posts in the last 30 days against the target of eight. None with no account."""
    if not _accounts(db):
        return None
    return min(100, round(100 * posts_last_30d(db) / TARGET_POSTS_30D))


def cached_insight(db, platform: str | None, week_start: date | None = None):
    week_start = week_start or _week_start(date.today())
    return db.query(models.BrandInsight).filter(
        models.BrandInsight.week_start == week_start, models.BrandInsight.platform == (platform or "all")).first()


def insight(db, platform: str | None = None, week_start: date | None = None, refresh: bool = False) -> dict:
    """The weekly note: at most one model call per platform per ISO week, cached in
    ``brand_insights``; ``refresh`` regenerates it. ``text`` is None with no key or when
    there is nothing measured to talk about. Commits when it writes."""
    week_start = week_start or _week_start(date.today())
    key = platform or "all"
    row = cached_insight(db, platform, week_start)
    if row is not None and not refresh:
        return {"week_start": week_start, "platform": key, "text": row.text, "cached": True}
    if not llm.is_configured():
        return {"week_start": week_start, "platform": key, "text": row.text if row else None, "cached": row is not None}
    data = performance(db, platform)
    if not data["connected"] or not data["cadence"]["posts_in_window"]:
        return {"week_start": week_start, "platform": key, "text": row.text if row else None, "cached": row is not None}
    try:
        text = (llm.chat_text(
            [{"role": "system", "content": INSIGHT_PROMPT},
             {"role": "user", "content": json.dumps(data, default=str)}],
            db=db, purpose="brand_insight", max_tokens=INSIGHT_MAX_TOKENS) or "").strip()
    except Exception as exc:                              # noqa: BLE001 — the page still renders
        print(f"[brand] insight failed: {type(exc).__name__}")
        text = ""
    if not text:
        return {"week_start": week_start, "platform": key, "text": row.text if row else None, "cached": row is not None}
    if row is None:
        row = models.BrandInsight(week_start=week_start, platform=key)
        db.add(row)
    row.text, row.model, row.created_at = text, llm.assistant_model(), datetime.utcnow()
    db.commit()
    return {"week_start": week_start, "platform": key, "text": text, "cached": False}
