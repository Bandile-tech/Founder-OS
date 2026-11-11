"""
Phase 5D — personal-brand analytics acceptance tests.

  1. YouTube adapter on canned JSON: two pages of uploads, shorts flagged by duration,
     videos fetched 50 ids at a time, account numbers captured
  2. a second sync with the same data writes no new snapshot; a changed count writes one
  3. no YOUTUBE_API_KEY: the account goes to `error` with a message, nothing raises
  4. the LinkedIn export fixture imports; a re-import updates and never duplicates
  5. performance: top posts by engagement rate, null where not measured, format medians,
     a 12-week trend
  6. the weekly insight is cached once per platform-week; refresh regenerates; no key → null
  7. ideas: created by tool, a date schedules, advance without a date is a 409, mark_posted
     links by URL
  8. briefing: today's idea at 58; progress.brand is null with no account
  9. no endpoint or tool returns an estimated number

HTTP is patched at ``brand.adapters.httpx.get``. No network.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import llm
import models
from brand import adapters
from brand import sync as brand_sync
from tests.conftest import TestSessionLocal, client

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_content_sample.csv"
CHANNEL = "UC" + "x" * 22


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-youtube-key")
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class FakeYouTube:
    """Canned YouTube Data API: a channel with 55 uploads over two pages."""

    def __init__(self, videos=55, views_bump=None, subscribers="321", hidden=False):
        self.calls = []
        self.ids = [f"vid{i:03d}" for i in range(1, videos + 1)]
        self.views_bump = views_bump or {}
        self.subscribers, self.hidden = subscribers, hidden

    def __call__(self, url, params=None, timeout=None):
        path = url.rsplit("/", 1)[-1]
        params = dict(params or {})
        self.calls.append((path, params))
        assert params.get("key") == "test-youtube-key"
        if path == "channels" and params.get("forHandle"):
            return _Resp({"items": [{"id": CHANNEL}]} if params["forHandle"] == "@bandile" else {"items": []})
        if path == "channels":
            stats = {"viewCount": "12345", "videoCount": str(len(self.ids)), "hiddenSubscriberCount": self.hidden}
            if not self.hidden:
                stats["subscriberCount"] = self.subscribers
            return _Resp({"items": [{"id": CHANNEL, "statistics": stats, "snippet": {"customUrl": "@bandile"},
                                     "contentDetails": {"relatedPlaylists": {"uploads": "UUuploads"}}}]})
        if path == "playlistItems":
            page = self.ids[50:] if params.get("pageToken") == "page2" else self.ids[:50]
            body = {"items": [{"contentDetails": {"videoId": v}} for v in page]}
            if not params.get("pageToken") and len(self.ids) > 50:
                body["nextPageToken"] = "page2"
            return _Resp(body)
        if path == "videos":
            items = []
            for v in params["id"].split(","):
                n = int(v[3:])
                items.append({
                    "id": v,
                    "snippet": {"title": f"Video {n}", "publishedAt": f"2026-10-{(n % 28) + 1:02d}T08:00:00Z"},
                    "contentDetails": {"duration": "PT45S" if n % 5 == 0 else "PT6M12S"},
                    "statistics": {"viewCount": str(100 * n + self.views_bump.get(v, 0)), "likeCount": str(n),
                                   **({} if n == 7 else {"commentCount": str(n % 3)})},
                })
            return _Resp({"items": items})
        if path == "search":
            return _Resp({"items": []})
        return _Resp({"error": {"message": "unexpected"}}, 404)


def _connect(fake):
    with patch.object(adapters.httpx, "get", side_effect=fake):
        made = client.post("/brand/accounts", json={"platform": "youtube", "handle": "bandile"})
    assert made.status_code == 201, made.text
    return made.json()


def _sync(account_id, fake):
    with patch.object(adapters.httpx, "get", side_effect=fake):
        out = client.post(f"/brand/accounts/{account_id}/sync")
    assert out.status_code == 200, out.text
    return out.json()


# ── 1. the YouTube adapter ────────────────────────────────────

def test_youtube_sync_reads_two_pages_flags_shorts_and_batches_ids():
    fake = FakeYouTube()
    account = _connect(fake)
    assert (account["external_id"], account["handle"], account["access"]) == (CHANNEL, "@bandile", "api_key")
    assert account["followers"] is None and account["posts_stored"] == 0          # connected, not yet synced

    fake.calls.clear()
    out = _sync(account["id"], fake)
    assert (out["ok"], out["posts_new"], out["metrics_rows"], out["status"]) == (True, 55, 55, "connected")
    assert len(out["changes"]) == 55 and out["changes"][0]["table"] == "content_posts"

    paths = [p for p, _ in fake.calls]
    assert paths == ["channels", "playlistItems", "playlistItems", "videos", "videos"]        # five calls for 55 videos
    video_calls = [params["id"].split(",") for p, params in fake.calls if p == "videos"]
    assert [len(ids) for ids in video_calls] == [50, 5]                                       # 50 ids per call
    assert fake.calls[2][1]["pageToken"] == "page2"

    db = _db()
    posts = db.query(models.ContentPost).all()
    assert len(posts) == 55
    shorts = {p.external_id for p in posts if p.kind == "short"}
    assert shorts == {f"vid{i:03d}" for i in range(5, 56, 5)}                                 # the 45-second ones
    one = db.query(models.ContentPost).filter(models.ContentPost.external_id == "vid007").one()
    assert one.url == "https://www.youtube.com/watch?v=vid007" and one.title == "Video 7"
    assert one.posted_at == datetime(2026, 10, 8, 8, 0)
    metric = brand_sync.latest_post_metric(db, one.id)
    assert (metric.views, metric.likes, metric.source) == (700, 7, "api")
    assert metric.comments is None                                    # the API gave no comment count: not 0
    assert metric.shares is None and metric.saves is None and metric.impressions is None and metric.watch_time_s is None
    db.close()

    listed = client.get("/brand/accounts").json()[0]
    assert (listed["followers"], listed["posts_count"], listed["total_views"]) == (321, 55, 12345)
    assert listed["following"] is None and listed["followers_delta_30d"] is None and listed["posts_stored"] == 55
    assert listed["last_synced_at"] is not None and listed["last_error"] is None


def test_hidden_subscriber_count_is_null_not_zero():
    fake = FakeYouTube(videos=3, hidden=True)
    account = _connect(fake)
    _sync(account["id"], fake)
    assert client.get("/brand/accounts").json()[0]["followers"] is None


def test_connecting_reports_the_adapter_message():
    fake = FakeYouTube()
    with patch.object(adapters.httpx, "get", side_effect=fake):
        unknown = client.post("/brand/accounts", json={"platform": "youtube", "handle": "@nobody"})
        by_id = client.post("/brand/accounts", json={"platform": "youtube", "external_id": CHANNEL})
        again = client.post("/brand/accounts", json={"platform": "youtube", "handle": "@bandile"})
    assert unknown.status_code == 400 and unknown.json()["detail"] == "no YouTube channel found for @nobody"
    assert by_id.status_code == 201 and again.status_code == 201
    assert again.json()["id"] == by_id.json()["id"]                                # the same channel: one account
    assert client.post("/brand/accounts", json={"platform": "linkedin"}).status_code == 400
    assert client.post("/brand/accounts", json={"platform": "myspace"}).status_code == 400
    with patch.object(adapters.httpx, "get", side_effect=RuntimeError("socket closed")):
        down = client.post("/brand/accounts", json={"platform": "youtube", "handle": "@bandile"})
    assert down.status_code == 400 and "could not be reached" in down.json()["detail"]


# ── 2. snapshots only when something changed ──────────────────

def test_repeat_sync_writes_no_snapshot_and_a_changed_count_writes_one():
    fake = FakeYouTube(videos=12)
    account = _connect(fake)
    first = _sync(account["id"], fake)
    assert first["metrics_rows"] == 12 and first["account_rows"] == 1

    second = _sync(account["id"], fake)
    assert (second["posts_new"], second["metrics_rows"], second["account_rows"]) == (0, 0, 0)
    db = _db()
    assert db.query(models.ContentMetric).count() == 12 and db.query(models.AccountMetric).count() == 1
    db.close()

    moved = FakeYouTube(videos=12, views_bump={"vid004": 250}, subscribers="330")
    third = _sync(account["id"], moved)
    assert (third["posts_new"], third["metrics_rows"], third["account_rows"]) == (0, 1, 1)   # one post, one account row
    db = _db()
    post = db.query(models.ContentPost).filter(models.ContentPost.external_id == "vid004").one()
    snaps = db.query(models.ContentMetric).filter(models.ContentMetric.post_id == post.id).order_by(models.ContentMetric.id).all()
    assert [s.views for s in snaps] == [400, 650]
    assert db.query(models.ContentMetric).count() == 13
    earlier = db.query(models.AccountMetric).order_by(models.AccountMetric.id).first()
    earlier.captured_at = datetime.utcnow() - timedelta(days=31)                  # the first reading is a month old
    db.commit()
    db.close()
    assert client.get("/brand/accounts").json()[0]["followers_delta_30d"] == 9    # 330 − 321, both measured


# ── 3. no API key ─────────────────────────────────────────────

def test_missing_youtube_key_marks_the_account_error(monkeypatch):
    fake = FakeYouTube(videos=3)
    account = _connect(fake)
    monkeypatch.delenv("YOUTUBE_API_KEY")
    with patch.object(adapters.httpx, "get", side_effect=AssertionError("must not be called")):
        out = client.post(f"/brand/accounts/{account['id']}/sync")
        assert out.status_code == 200
        body = out.json()
        assert (body["ok"], body["status"], body["last_error"]) == (False, "error", "YOUTUBE_API_KEY not set")
        refused = client.post("/brand/accounts", json={"platform": "youtube", "handle": "@bandile"})
        assert refused.status_code == 400 and refused.json()["detail"] == "YOUTUBE_API_KEY not set"
    listed = client.get("/brand/accounts").json()[0]
    assert listed["status"] == "error" and listed["last_error"] == "YOUTUBE_API_KEY not set"
    db = _db()
    assert db.query(models.ContentPost).count() == 0 and db.query(models.ContentMetric).count() == 0
    db.close()

    monkeypatch.setenv("YOUTUBE_API_KEY", "test-youtube-key")
    assert _sync(account["id"], fake)["status"] == "connected"                    # the key is back: it recovers


def test_api_errors_and_disconnect():
    fake = FakeYouTube(videos=3)
    account = _connect(fake)
    _sync(account["id"], fake)
    with patch.object(adapters.httpx, "get", return_value=_Resp({"error": {"message": "quotaExceeded"}}, 403)):
        out = client.post(f"/brand/accounts/{account['id']}/sync").json()
    assert out["status"] == "error" and out["last_error"] == "YouTube answered 403: quotaExceeded"
    db = _db()
    assert db.query(models.ContentPost).count() == 3                              # what was stored stays
    db.close()

    gone = client.delete(f"/brand/accounts/{account['id']}").json()
    assert gone["status"] == "disconnected" and gone["posts_stored"] == 3 and gone["change"]["table"] == "social_accounts"
    assert client.post(f"/changes/{gone['change']['id']}/undo").status_code == 200
    assert client.get("/brand/accounts").json()[0]["status"] == "error"
    assert client.post("/brand/accounts/9999/sync").status_code == 404
    assert client.delete("/brand/accounts/9999").status_code == 404


def test_brand_sync_agent_syncs_api_accounts_and_reports_failures():
    import agents
    from agents import runner
    from agents.brand_sync import SPEC

    fake = FakeYouTube(videos=4)
    account = _connect(fake)
    with open(FIXTURE, "rb") as fh:
        client.post("/brand/import", files={"file": ("content.csv", fh, "text/csv")}, data={"platform": "linkedin"})
    with patch.object(runner, "SessionLocal", TestSessionLocal), patch.dict(agents.REGISTRY, {SPEC.key: SPEC}, clear=True):
        with patch.object(adapters.httpx, "get", side_effect=fake):
            ok = runner.run_agent(SPEC, "manual")
        assert ok["status"] == "completed" and ok["result"] == {"synced": 1, "failed": 0, "posts_new": 4}   # not the export account
        with patch.object(adapters.httpx, "get", return_value=_Resp({}, 500)):
            bad = runner.run_agent(SPEC, "manual")
        assert bad["status"] == "completed" and bad["result"]["failed"] == 1
    db = _db()
    event = db.query(models.AgentEvent).one()
    assert event.kind == "brand.sync_failed" and str(account["id"]) in event.payload_json
    db.close()


# ── 4. the LinkedIn export ────────────────────────────────────

def _import(platform="linkedin", name="content.csv"):
    with open(FIXTURE, "rb") as fh:
        return client.post("/brand/import", files={"file": (name, fh, "text/csv")}, data={"platform": platform})


def test_linkedin_export_imports_and_reimports_without_duplicates():
    resp = _import()
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["rows"], out["posts_new"], out["posts_updated"], out["metrics_rows"]) == (6, 6, 0, 5)
    assert out["account_created"] is True and out["platform"] == "linkedin"

    account = client.get("/brand/accounts").json()[0]
    assert (account["platform"], account["access"], account["status"]) == ("linkedin", "export", "connected")
    assert account["followers"] is None and account["posts_stored"] == 6          # the file has no follower sheet

    db = _db()
    first = db.query(models.ContentPost).filter(models.ContentPost.external_id.like("%1001")).one()
    assert first.title == "How I built a bakery's ordering site in a weekend" and first.kind == "post"
    assert first.posted_at == datetime(2026, 11, 2)
    m = brand_sync.latest_post_metric(db, first.id)
    assert (m.impressions, m.likes, m.comments, m.shares, m.source) == (1240, 38, 6, 4, "export")   # "1,240" read as a number
    assert m.views is None and m.saves is None
    blank = db.query(models.ContentPost).filter(models.ContentPost.external_id.like("%1005")).one()
    assert brand_sync.latest_post_metric(db, blank.id) is None                    # no numbers: no snapshot, no zeros
    partial = db.query(models.ContentPost).filter(models.ContentPost.external_id.like("%1006")).one()
    assert brand_sync.latest_post_metric(db, partial.id).shares is None           # an empty cell stays empty
    db.close()

    again = _import().json()
    assert (again["posts_new"], again["posts_updated"], again["metrics_rows"], again["account_created"]) == (0, 0, 0, False)
    db = _db()
    assert db.query(models.ContentPost).count() == 6 and db.query(models.ContentMetric).count() == 5
    assert db.query(models.SocialAccount).count() == 1
    db.close()

    assert _import(platform="x").status_code == 400                               # no importer for X yet
    bad = client.post("/brand/import", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}, data={"platform": "linkedin"})
    assert bad.status_code == 400 and "expected columns" in bad.json()["detail"]
    sync = client.post(f"/brand/accounts/{account['id']}/sync").json()
    assert sync["ok"] is False and "export file" in sync["error"]                 # an export account has nothing to pull


def test_adapter_helpers_never_invent_numbers():
    assert adapters._int("1,240") == 1240 and adapters._int("12.0") == 12
    assert adapters._int("") is None and adapters._int(None) is None and adapters._int("n/a") is None
    assert adapters._iso_duration_seconds("PT1M5S") == 65 and adapters._iso_duration_seconds("PT45S") == 45
    assert adapters._iso_duration_seconds("P1DT2H") == 93600 and adapters._iso_duration_seconds("soon") is None
    assert adapters._parse_dt("2026-10-08T08:00:00Z") == datetime(2026, 10, 8, 8, 0)
    assert adapters._parse_dt("2026-10-08T10:00:00+02:00") == datetime(2026, 10, 8, 8, 0)
    assert adapters._parse_dt("11/02/2026") == datetime(2026, 11, 2) and adapters._parse_dt("") is None
    assert adapters.PlatformAdapter().fetch_account(None) is None and adapters.PlatformAdapter().fetch_posts(None) == []


def test_m017_is_registered_before_the_schema_check():
    source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    assert "run_m017(db)" in source and source.index("run_m017(db)") < source.index("_validate_schema(engine)   #")
    from migrations import m017_brand
    assert set(m017_brand._TABLES) == {"social_accounts", "content_posts", "content_metrics", "account_metrics",
                                       "content_ideas", "brand_insights"}


# ════════════════════════════════════════════════════════════════
# 5D.2 — performance, insight, ideas and calendar, tools, briefing
# ════════════════════════════════════════════════════════════════

import json
from datetime import date

import brand
import orchestrator_tools as ot
from brand import analytics

TODAY = date.today()
NOW = datetime.utcnow()


def _account(db, platform="youtube", status="connected", **kw):
    a = models.SocialAccount(platform=platform, handle=kw.pop("handle", "@bandile"), access=kw.pop("access", "api_key"),
                             status=status, **kw)
    db.add(a)
    db.commit()
    return a


def _post(db, account, title, days_ago, kind="video", **metrics):
    p = models.ContentPost(account_id=account.id, external_id=title, title=title, kind=kind,
                           url=f"https://example.com/{title.replace(' ', '-')}",
                           posted_at=NOW - timedelta(days=days_ago))
    db.add(p)
    db.flush()
    if metrics:
        db.add(models.ContentMetric(post_id=p.id, captured_at=NOW, source="api", **metrics))
    db.commit()
    return p


# ── 5. performance ────────────────────────────────────────────

def test_performance_ranks_by_engagement_rate_and_keeps_missing_data_null():
    db = _db()
    acc = _account(db)
    _post(db, acc, "Big reach low engagement", 3, views=10000, likes=50, comments=10)           # 0.006
    _post(db, acc, "Small but loved", 5, kind="short", views=400, likes=60, comments=20)       # 0.2
    _post(db, acc, "Solid short", 9, kind="short", views=1000, likes=80, comments=10, shares=10)   # 0.1
    _post(db, acc, "Views only", 12, views=2000)                                              # no engagement measured
    _post(db, acc, "No numbers at all", 15)                                                   # nothing measured
    _post(db, acc, "Old one", 200, views=99999, likes=9999)                                   # outside the window
    old = db.query(models.ContentPost).filter(models.ContentPost.title == "Solid short").one()
    db.add(models.ContentMetric(post_id=old.id, captured_at=NOW - timedelta(days=2), source="api", views=10, likes=1))
    db.commit()
    db.close()

    p = client.get("/brand/performance").json()
    assert p["connected"] is True and p["days"] == 90 and p["insight"] is None
    top = p["top_posts"]
    assert [t["post"]["title"] for t in top] == ["Small but loved", "Solid short", "Big reach low engagement", "Views only"]
    assert top[0]["engagement"] == 80 and top[0]["engagement_rate"] == 0.2
    assert top[1]["engagement"] == 100 and top[1]["engagement_rate"] == 0.1 and top[1]["views"] == 1000   # the latest snapshot
    assert top[3]["views"] == 2000 and top[3]["engagement"] is None and top[3]["engagement_rate"] is None  # null, not 0

    assert p["by_format"]["short"] == {"count": 2, "median_views": 700.0, "median_engagement_rate": 0.15}
    video = p["by_format"]["video"]
    assert video["count"] == 3 and video["median_views"] == 6000.0 and video["median_engagement_rate"] == 0.006
    assert p["cadence"] == {"posts_in_window": 5, "per_week": round(5 / (90 / 7), 2), "gap_days_since_last": 3}
    assert len(p["trend"]) == 12 and sum(w["posts"] for w in p["trend"]) == 5
    this_week = p["trend"][-1]
    assert this_week["week_start"] == (TODAY - timedelta(days=TODAY.weekday())).isoformat()
    empty_weeks = [w for w in p["trend"] if w["posts"] == 0]
    assert empty_weeks and all(w["views"] is None and w["engagement"] is None for w in empty_weeks)
    assert p["posts_last_30d"] == 5 and p["brand_target_posts_30d"] == 8

    assert client.get("/brand/performance", params={"days": 7}).json()["cadence"]["posts_in_window"] == 2
    assert client.get("/brand/performance", params={"platform": "linkedin"}).json()["connected"] is False
    posts = client.get("/brand/posts", params={"limit": 2}).json()
    assert [x["title"] for x in posts] == ["Big reach low engagement", "Small but loved"]              # newest first
    assert posts[0]["platform"] == "youtube" and posts[0]["engagement_rate"] == 0.006


# ── 6. the weekly insight ─────────────────────────────────────

def test_insight_is_cached_per_platform_week(monkeypatch):
    db = _db()
    acc = _account(db)
    _post(db, acc, "Small but loved", 5, views=400, likes=60)
    db.close()
    assert client.get("/brand/insight").json()["text"] is None                     # no key: nothing, and no call

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with patch("llm.chat_text", return_value="Shorts did best. Try two more this week.") as chat:
        first = client.get("/brand/insight").json()
        assert first["text"].startswith("Shorts did best") and first["cached"] is False and first["platform"] == "all"
        kwargs = chat.call_args.kwargs
        assert kwargs["purpose"] == "brand_insight" and kwargs["max_tokens"] == 250
        assert chat.call_args.args[0][0]["content"] == analytics.INSIGHT_PROMPT
        again = client.get("/brand/insight").json()
        assert again["cached"] is True and chat.call_count == 1                    # once per platform-week
        assert client.get("/brand/performance").json()["insight"].startswith("Shorts did best")
        assert chat.call_count == 1                                                # the page never generates it
        per_platform = client.get("/brand/insight", params={"platform": "youtube"}).json()
        assert per_platform["cached"] is False and chat.call_count == 2            # a different platform key
        chat.return_value = "A new reading."
        fresh = client.get("/brand/insight", params={"refresh": 1}).json()
        assert fresh["text"] == "A new reading." and fresh["cached"] is False and chat.call_count == 3
        nothing = client.get("/brand/insight", params={"platform": "linkedin"}).json()
        assert nothing["text"] is None and chat.call_count == 3                    # nothing connected: no call
    db = _db()
    assert db.query(models.BrandInsight).count() == 2
    db.close()


# ── 7. ideas and the calendar ─────────────────────────────────

def test_ideas_schedule_advance_and_mark_posted():
    db = _db()
    made, summary = ot.run_tool("add_content_idea", {"title": "A short on how I built the bakery site",
                                                     "format": "short", "platform": "YouTube"}, db)
    assert made["status"] == "idea" and made["platform"] == "youtube" and "added" in summary
    dated, summary = ot.run_tool("add_content_idea", {"idea_id": made["idea_id"], "scheduled_for": "in 3 days"}, db)
    assert dated["status"] == "scheduled" and dated["scheduled_for"] == TODAY + timedelta(days=3)
    assert "scheduled for" in summary
    direct, _ = ot.run_tool("add_content_idea", {"title": "Pricing mistakes thread", "scheduled_for": "tomorrow"}, db)
    assert direct["status"] == "scheduled"                                         # a date schedules it straight away
    bad, _ = ot.run_tool("add_content_idea", {"title": "X", "scheduled_for": "one day"}, db)
    assert "error" in bad
    blank, _ = ot.run_tool("add_content_idea", {}, db)
    assert blank["error"] == "an idea needs a title"
    db.close()

    loose = client.post("/brand/ideas", json={"title": "Why WhatsApp is enough", "format": "post"}).json()
    assert loose["status"] == "idea" and loose["change"]["table"] == "content_ideas"
    drafted = client.post(f"/brand/ideas/{loose['id']}/advance").json()
    assert drafted["status"] == "drafted"
    stuck = client.post(f"/brand/ideas/{loose['id']}/advance")
    assert stuck.status_code == 409 and stuck.json()["detail"] == "scheduling needs a date"
    patched = client.patch(f"/brand/ideas/{loose['id']}", json={"scheduled_for": TODAY.isoformat(), "score": 9})
    assert patched.json()["status"] == "scheduled" and patched.json()["score"] == 5
    posted = client.post(f"/brand/ideas/{loose['id']}/advance").json()
    assert posted["status"] == "posted"
    assert client.post(f"/brand/ideas/{loose['id']}/advance").status_code == 400    # nothing after posted
    assert client.patch(f"/brand/ideas/{loose['id']}", json={"status": "viral"}).status_code == 400
    assert client.patch("/brand/ideas/9999", json={"title": "x"}).status_code == 404

    db = _db()
    acc = _account(db)
    post = _post(db, acc, "Bakery site short", 0, kind="short", views=120)
    post_id = post.id
    linked, summary = ot.run_tool("mark_posted", {"idea": "bakery site", "url": post.url}, db)
    assert linked["status"] == "posted" and linked["linked_to_a_stored_post"] is True
    assert summary.endswith("marked posted and linked to its post")
    db.refresh(post)
    idea = db.query(models.ContentIdea).filter(models.ContentIdea.id == made["idea_id"]).one()
    assert idea.posted_post_id == post.id and post.idea_id == idea.id
    unlinked, _ = ot.run_tool("mark_posted", {"idea": "Pricing mistakes"}, db)
    assert unlinked["linked_to_a_stored_post"] is False
    gone, _ = ot.run_tool("mark_posted", {"idea": "bakery site"}, db)               # already posted: not an open idea
    assert "error" in gone
    db.close()

    listed = client.get("/brand/ideas", params={"status": "posted"}).json()
    assert len(listed) == 3
    tagged = client.patch(f"/brand/posts/{post_id}", json={"topic_tags": ["build log", " bakery "]}).json()
    assert tagged["topic_tags"] == ["build log", "bakery"] and tagged["change"]["table"] == "content_posts"


def test_calendar_covers_this_week_and_next():
    monday = TODAY - timedelta(days=TODAY.weekday())
    client.post("/brand/ideas", json={"title": "This week", "scheduled_for": (monday + timedelta(days=2)).isoformat()})
    client.post("/brand/ideas", json={"title": "Next week", "scheduled_for": (monday + timedelta(days=9)).isoformat()})
    client.post("/brand/ideas", json={"title": "Far off", "scheduled_for": (monday + timedelta(days=40)).isoformat()})
    client.post("/brand/ideas", json={"title": "Unscheduled backlog idea"})
    db = _db()
    acc = _account(db)
    _post(db, acc, "Went out today", 0)
    db.close()

    cal = client.get("/brand/calendar").json()
    assert cal["from"] == monday.isoformat() and len(cal["days"]) == 14
    by_day = {d["date"]: d for d in cal["days"]}
    assert [i["title"] for i in by_day[(monday + timedelta(days=2)).isoformat()]["ideas"]] == ["This week"]
    assert [i["title"] for i in by_day[(monday + timedelta(days=9)).isoformat()]["ideas"]] == ["Next week"]
    assert sum(len(d["ideas"]) for d in cal["days"]) == 2                           # the far-off one is outside
    assert [p["title"] for p in by_day[TODAY.isoformat()]["posted"]] == ["Went out today"]
    assert by_day[TODAY.isoformat()]["is_today"] is True
    assert [i["title"] for i in cal["backlog"]] == ["Unscheduled backlog idea"]
    custom = client.get("/brand/calendar", params={"from": (monday + timedelta(days=35)).isoformat(),
                                                   "to": (monday + timedelta(days=41)).isoformat()}).json()
    assert len(custom["days"]) == 7 and sum(len(d["ideas"]) for d in custom["days"]) == 1


# ── 8. briefing ───────────────────────────────────────────────

def test_briefing_content_items_and_brand_progress():
    import briefing
    empty = briefing.build_briefing(_db(), with_summary=False)
    assert empty["progress"]["brand"] is None and empty["brand_target_posts_30d"] == 8   # no account: null, not 0

    today_idea = client.post("/brand/ideas", json={"title": "Post the bakery short", "scheduled_for": TODAY.isoformat()}).json()
    late_idea = client.post("/brand/ideas", json={"title": "Overdue thread", "scheduled_for": (TODAY - timedelta(days=3)).isoformat()}).json()
    client.post("/brand/ideas", json={"title": "Later", "scheduled_for": (TODAY + timedelta(days=4)).isoformat()})
    db = _db()
    quiet = _account(db, handle="@quiet")
    _post(db, quiet, "A while ago", 20, views=10)
    broken = _account(db, platform="linkedin", status="error", access="export", handle=None,
                      last_error="the file has none of the expected columns")
    active = _account(db, handle="@active")
    for i in range(3):
        _post(db, active, f"Recent {i}", i + 1, views=10)
    quiet, broken, active = quiet.id, broken.id, active.id
    db.close()

    b = briefing.build_briefing(_db(), with_summary=False)
    by_id = {i["id"]: i for i in b["items"]}
    item = by_id[f"content:content_idea:{today_idea['id']}"]
    assert (item["score"], item["due_label"]) == (58, "Today") and item["suggested_action"] == "Post it, then say 'posted'"
    late = by_id[f"content:content_idea:{late_idea['id']}"]
    assert (late["score"], late["due_label"]) == (66, "3d overdue")
    err = by_id[f"content:social_account:{broken}"]
    assert err["score"] == 20 and err["reason"] == "the file has none of the expected columns"
    assert err["suggested_action"] == "Fix the connection in Settings"
    gap = by_id[f"content:social_account:{quiet}"]
    assert gap["score"] == 22 and gap["reason"] == "Last post 20 days ago"
    assert f"content:social_account:{active}" not in by_id
    assert sum(1 for i in b["items"] if i["category"] == "content") == 4            # the later idea is not due
    assert b["progress"]["brand"] == round(100 * 4 / 8)                             # four posts in 30 days of eight

    client.post(f"/brand/ideas/{today_idea['id']}/advance")                         # scheduled → posted
    after = briefing.build_briefing(_db(), with_summary=False)
    assert f"content:content_idea:{today_idea['id']}" not in {i["id"] for i in after["items"]}


# ── 9. nothing is ever estimated ──────────────────────────────

def test_no_endpoint_or_tool_returns_an_estimated_metric():
    db = _db()
    nothing, summary = ot.run_tool("brand_performance", {}, db)
    assert nothing["connected"] is False and summary == "no social account connected"
    assert "Do not estimate" in nothing["note"]

    acc = _account(db)
    _post(db, acc, "Just published", 1)                                             # an account, a post, no numbers
    result, _ = ot.run_tool("brand_performance", {}, db)
    assert result["connected"] is True
    account = result["accounts"][0]
    assert account["followers"] is None and account["followers_change_30_days"] is None
    assert result["top_posts"] == []                                                # nothing measured to rank
    fmt = result["by_format"]["video"]
    assert fmt == {"count": 1, "median_views": None, "median_engagement_rate": None}
    assert result["cadence"]["posts_in_window"] == 1                                # a count of posts is a fact
    assert result["weekly_insight"] is None and "never estimate" in result["note"]
    shown = json.loads(ot.for_model("brand_performance", result))                   # what the model reads
    assert "followers" not in shown["accounts"][0] and "never estimate" in shown["note"]
    db.close()

    page = client.get("/brand/performance").json()
    assert page["accounts"][0]["followers"] is None and page["accounts"][0]["followers_delta_30d"] is None
    assert all(w["views"] is None and w["engagement"] is None for w in page["trend"])
    post = client.get("/brand/posts").json()[0]
    for key in ("views", "likes", "comments", "shares", "saves", "impressions", "watch_time_s",
                "engagement", "engagement_rate"):
        assert post[key] is None, key
    listed = client.get("/brand/accounts").json()[0]
    for key in ("followers", "followers_delta_30d", "following", "posts_count", "total_views"):
        assert listed[key] is None, key


def test_content_ideas_are_deletable_through_confirmation():
    import actions
    idea = client.post("/brand/ideas", json={"title": "Drop this one"}).json()
    assert "content_ideas" in actions.DELETABLE
    db = _db()
    pending, _ = ot.run_tool("propose_delete", {"table": "content_ideas", "record_id": idea["id"]}, db)
    assert pending["requires_confirmation"] is True and db.query(models.ContentIdea).count() == 1
    db.close()
