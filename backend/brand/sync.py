"""
Brand sync — writes what the adapters fetched (Phase 5D).

  sync(db, account)                         one account through its API adapter
  import_export(db, platform, text, name)   a platform's analytics export file

Posts are upserted by ``(account_id, external_id)``. Metric snapshots are append-only: a
row is added only when a number changed since the last snapshot, so a daily sync of a quiet
channel writes nothing. New posts are in the change log; metric snapshots are data, not
changes, and are not logged. The caller commits.
"""

import json
from datetime import datetime

import change_log
import models
from brand import adapters

_POST_METRICS = adapters.METRIC_KEYS
_ACCOUNT_METRICS = ("followers", "following", "posts_count", "total_views")


def latest_post_metric(db, post_id: int):
    return db.query(models.ContentMetric).filter(
        models.ContentMetric.post_id == post_id,
    ).order_by(models.ContentMetric.captured_at.desc(), models.ContentMetric.id.desc()).first()


def latest_account_metric(db, account_id: int):
    return db.query(models.AccountMetric).filter(
        models.AccountMetric.account_id == account_id,
    ).order_by(models.AccountMetric.captured_at.desc(), models.AccountMetric.id.desc()).first()


def _has_any(values: dict, keys) -> bool:
    return any(values.get(k) is not None for k in keys)


def _store_posts(db, account, posts: list[dict], source: str) -> dict:
    counts = {"posts_new": 0, "posts_updated": 0, "metrics_rows": 0}
    now = datetime.utcnow()
    for item in posts:
        external_id = item.get("external_id")
        if not external_id:
            continue
        post = db.query(models.ContentPost).filter(
            models.ContentPost.account_id == account.id, models.ContentPost.external_id == str(external_id)).first()
        if post is None:
            post = models.ContentPost(account_id=account.id, external_id=str(external_id), url=item.get("url"),
                                      title=item.get("title"), kind=item.get("kind"),
                                      posted_at=item.get("posted_at"), created_at=now, updated_at=now)
            db.add(post)
            change_log.record_create(db, post, f"New {account.platform} post: {(post.title or post.url or '')[:60]}")
            counts["posts_new"] += 1
        else:
            changed = False
            for field in ("url", "title", "kind", "posted_at"):
                value = item.get(field)
                if value is not None and getattr(post, field) != value:
                    setattr(post, field, value)
                    changed = True
            if changed:
                post.updated_at = now
                counts["posts_updated"] += 1
        metrics = item.get("metrics") or {}
        if not _has_any(metrics, _POST_METRICS):
            continue                                    # the platform gave no numbers: store none
        last = latest_post_metric(db, post.id)
        if last is not None and all(getattr(last, k) == metrics.get(k) for k in _POST_METRICS):
            continue                                    # nothing moved since the last snapshot
        db.add(models.ContentMetric(post_id=post.id, captured_at=now, source=source,
                                    raw_json=json.dumps(item.get("raw") or {}, default=str),
                                    **{k: metrics.get(k) for k in _POST_METRICS}))
        counts["metrics_rows"] += 1
    return counts


def _store_account(db, account, stats: dict | None, source: str) -> int:
    if not stats or not _has_any(stats, _ACCOUNT_METRICS):
        return 0
    last = latest_account_metric(db, account.id)
    if last is not None and all(getattr(last, k) == stats.get(k) for k in _ACCOUNT_METRICS):
        return 0
    db.add(models.AccountMetric(account_id=account.id, captured_at=datetime.utcnow(), source=source,
                                raw_json=json.dumps(stats.get("raw") or {}, default=str),
                                **{k: stats.get(k) for k in _ACCOUNT_METRICS}))
    return 1


def sync(db, account) -> dict:
    """Fetch an account through its API adapter and store what changed. Never raises: a
    failure sets ``status='error'`` and ``last_error`` on the account."""
    adapter_cls = adapters.API_ADAPTERS.get(account.platform)
    result = {"ok": False, "posts_new": 0, "posts_updated": 0, "metrics_rows": 0}
    if adapter_cls is None or account.access == "export":
        result.update({"status": account.status, "last_error": account.last_error,
                       "error": f"{account.platform} is updated by importing its export file"})
        return result
    try:
        adapter = adapter_cls()
        stats = adapter.fetch_account(account)
        posts = adapter.fetch_posts(account) if adapter.error is None else []
        error = adapter.error
    except Exception as exc:                            # noqa: BLE001 — an adapter must not break a sync
        stats, posts, error = None, [], f"sync failed ({type(exc).__name__})"
    now = datetime.utcnow()
    if error:
        account.status, account.last_error, account.updated_at = "error", error[:500], now
        db.flush()
        result.update({"status": "error", "last_error": account.last_error, "error": account.last_error})
        return result
    counts = _store_posts(db, account, posts, "api")
    counts["account_rows"] = _store_account(db, account, stats, "api")
    account.status, account.last_error, account.last_synced_at, account.updated_at = "connected", None, now, now
    db.flush()
    result.update({"ok": True, **counts, "status": "connected", "last_error": None})
    return result


def import_export(db, platform: str, text: str, filename: str | None = None) -> dict:
    """Read a platform's analytics export (CSV text) into its ``export`` account, creating
    the account on first use. Returns counts; ``error`` when the file could not be read."""
    adapter_cls = adapters.EXPORT_ADAPTERS.get(platform)
    if adapter_cls is None:
        raise ValueError(f"there is no importer for {platform}")
    adapter = adapter_cls(text)
    posts = adapter.fetch_posts()
    if adapter.error:
        raise ValueError(adapter.error)
    now = datetime.utcnow()
    account = db.query(models.SocialAccount).filter(
        models.SocialAccount.platform == platform, models.SocialAccount.access == "export").first()
    created = account is None
    if created:
        account = models.SocialAccount(platform=platform, access="export", status="connected",
                                       created_at=now, updated_at=now)
        db.add(account)
        db.flush()
        change_log.record_create(db, account, f"Connected {platform} (export)")
    counts = _store_posts(db, account, posts, "export")
    account.status, account.last_error, account.last_synced_at, account.updated_at = "connected", None, now, now
    try:
        config = json.loads(account.config_json) if account.config_json else {}
    except (ValueError, TypeError):
        config = {}
    config["last_file"] = filename
    account.config_json = json.dumps(config)
    db.flush()
    return {"account_id": account.id, "platform": platform, "account_created": created,
            "rows": len(posts), **counts, "batch_id": change_log.current_batch_id(db)}
