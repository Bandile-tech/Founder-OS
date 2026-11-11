"""
Phase 5D — personal-brand analytics acceptance tests.

  1. YouTube adapter on canned JSON: two pages of uploads, shorts flagged by duration,
     videos fetched 50 ids at a time, account numbers captured
  2. a second sync with the same data writes no new snapshot; a changed count writes one
  3. no YOUTUBE_API_KEY: the account goes to `error` with a message, nothing raises
  4. the LinkedIn export fixture imports; a re-import updates and never duplicates

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
