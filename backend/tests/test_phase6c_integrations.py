"""
Phase 6C.1 — integrations table, redaction, Telegram pairing.

  7. redact() removes env tokens, every secret_json value and bot-token URL paths
  8. GET /integrations lists the four providers and never returns a secret
     plus: the pairing code is deterministic and tied to the server secret, pair / unpair,
     set-webhook (503 until configured, posts the secret token, never echoes the bot token),
     get_or_create is safe on a race, stored errors are redacted, and the outreach message
     serialiser carries replied_at

No network: httpx is patched in the module under test.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).parent.parent))

import integrations
import integrations_telegram as tg
import models
from tests.conftest import TestSessionLocal, client

BOT_TOKEN = "123456789:AAE-test_token-value_0123456789abcdef"
HOOK_SECRET = "hook-secret-0123456789"
SECRET_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_USERNAME", "TELEGRAM_WEBHOOK_SECRET", "FOUNDER_OS_API_URL",
               "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "JOBS_TOKEN",
               "YOUTUBE_API_KEY", "OPENAI_API_KEY", "FOUNDER_OS_SECRET", "FOUNDER_OS_PASSWORD")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in SECRET_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


def _db():
    return TestSessionLocal()


def _resp(payload):
    return SimpleNamespace(status_code=200, json=lambda: payload, text=json.dumps(payload))


# ── 7. Redaction ─────────────────────────────────────────────

def test_redact_removes_env_tokens(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-client-secret")
    monkeypatch.setenv("JOBS_TOKEN", "jobs-token-0123456789")
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSy-youtube-key")
    text = (f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage failed; login abcd efgh ijkl mnop; "
            "secret GOCSPX-client-secret; tick jobs-token-0123456789; key=AIzaSy-youtube-key")
    out = integrations.redact(text)
    for secret in (BOT_TOKEN, "abcd efgh ijkl mnop", "GOCSPX-client-secret", "jobs-token-0123456789",
                   "AIzaSy-youtube-key"):
        assert secret not in out
    assert "***" in out and "sendMessage failed" in out


def test_redact_removes_any_bot_token_shaped_path_even_when_env_is_unset():
    out = integrations.redact("GET https://api.telegram.org/bot987654321:ZZ_other-token/getMe -> 401")
    assert "987654321" not in out and "ZZ_other-token" not in out
    assert out.endswith("/getMe -> 401")


def test_redact_removes_every_secret_json_value_including_nested():
    db = _db()
    row = integrations.get_or_create(db, "google_calendar")
    row.secret_json = json.dumps({"access_token": "ya29.access-token-value", "refresh_token": "1//refresh-token-value",
                                  "nested": {"id_token": "eyJ.nested.token"}, "list": ["list-secret-value"],
                                  "expires_in": 3600})
    db.commit()
    out = integrations.redact("401 for ya29.access-token-value / 1//refresh-token-value / eyJ.nested.token / "
                              "list-secret-value", db)
    for secret in ("ya29.access-token-value", "1//refresh-token-value", "eyJ.nested.token", "list-secret-value"):
        assert secret not in out
    # Without a session only the environment is known.
    assert "ya29.access-token-value" in integrations.redact("ya29.access-token-value")
    db.close()


def test_redact_leaves_ordinary_text_and_short_values_alone(monkeypatch):
    monkeypatch.setenv("JOBS_TOKEN", "abc")          # too short to be treated as a secret
    assert integrations.redact("abc is the start of the alphabet") == "abc is the start of the alphabet"
    assert integrations.redact(None) == "None"


def test_set_error_stores_a_redacted_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    db = _db()
    row = integrations.get_or_create(db, "telegram")
    integrations.set_error(db, row, f"HTTP 401 from /bot{BOT_TOKEN}/sendMessage")
    db.commit()
    stored = db.query(models.Integration).filter_by(provider="telegram").first()
    assert stored.status == "error" and BOT_TOKEN not in stored.last_error and "401" in stored.last_error
    db.close()


# ── 8. GET /integrations ─────────────────────────────────────

def test_integrations_lists_four_providers_as_disconnected_with_nothing_set():
    r = client.get("/integrations")
    assert r.status_code == 200
    items = r.json()
    assert [i["provider"] for i in items] == ["google_calendar", "email", "telegram", "youtube"]
    for item in items:
        assert item["status"] == "disconnected" and item["configured"] is False and item["actions"] == []
        assert "secret_json" not in item and "config_json" not in item
    # One row per provider, however often the list is read.
    client.get("/integrations")
    db = _db()
    assert db.query(models.Integration).count() == 4
    db.close()


def test_integrations_never_returns_a_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", HOOK_SECRET)
    monkeypatch.setenv("EMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id.apps.example")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-client-secret")
    monkeypatch.setenv("FOUNDER_OS_API_URL", "https://founder.example")
    db = _db()
    row = integrations.get_or_create(db, "google_calendar")
    row.status, row.account_label = "connected", "me@example.com"
    row.secret_json = json.dumps({"access_token": "ya29.access-token-value", "refresh_token": "1//refresh-token-value"})
    db.commit()
    db.close()

    r = client.get("/integrations")
    body = r.text
    for secret in (BOT_TOKEN, HOOK_SECRET, "abcd efgh ijkl mnop", "GOCSPX-client-secret",
                   "ya29.access-token-value", "1//refresh-token-value", "secret_json"):
        assert secret not in body
    by = {i["provider"]: i for i in r.json()}
    assert by["google_calendar"]["status"] == "connected" and by["google_calendar"]["actions"] == ["sync", "disconnect"]
    assert by["google_calendar"]["account_label"] == "me@example.com"
    assert by["email"]["status"] == "pending" and by["email"]["account_label"] == "me@example.com"
    assert by["telegram"]["status"] == "pending" and by["telegram"]["actions"] == ["pair", "set_webhook"]
    assert by["youtube"]["status"] == "disconnected"


def test_last_error_is_only_shown_while_the_provider_is_failing():
    db = _db()
    row = integrations.get_or_create(db, "google_calendar")
    row.status, row.last_error = "error", "token expired"
    db.commit()
    assert integrations.serialize(db, row)["last_error"] == "token expired"
    row.status = "connected"
    db.commit()
    assert integrations.serialize(db, row)["last_error"] is None
    db.close()


def test_get_or_create_reads_the_winner_when_two_requests_race():
    db = _db()
    winner = integrations.get_or_create(db, "email")
    calls = {"n": 0}
    real_find = integrations._find

    def find(session, provider):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(session, provider)     # the first look misses

    with patch.object(integrations, "_find", side_effect=find):
        row = integrations.get_or_create(db, "email")
    assert row.id == winner.id and calls["n"] == 2
    assert db.query(models.Integration).filter_by(provider="email").count() == 1
    db.close()


def test_provider_is_unique_in_the_table():
    db = _db()
    integrations.get_or_create(db, "youtube")
    db.add(models.Integration(provider="youtube", status="disconnected"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


# ── Telegram pairing ─────────────────────────────────────────

def test_pair_code_is_random_stored_and_not_derived_from_the_password(monkeypatch):
    """Review: the code was HMAC(server secret), and with no FOUNDER_OS_SECRET the server
    secret is derived from the password, so a code seen in a chat was something to test
    password guesses against. It also never changed."""
    import base64
    import hashlib
    import hmac as hmac_lib
    import auth
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "password-one")
    db = _db()
    code = tg.pair_code(db)
    assert len(code) == 6 and set(code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert tg.pair_code(db) == code                                # the same while it waits to be used
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "password-two")
    assert tg.pair_code(db) == code                                # nothing to do with the password
    derived = base64.b32encode(hmac_lib.new(auth._secret(), b"telegram-pair", hashlib.sha256).digest()).decode()[:6]
    codes = {code}
    for _ in range(3):                                             # each use spends the code
        tg.pair(db, 42, "bandile")
        db.commit()
        tg.unpair(db)
        db.commit()
        codes.add(tg.pair_code(db))
    assert len(codes) == 4 and derived not in codes
    db.close()


def test_pair_then_unpair(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    db = _db()
    assert tg.is_paired(db) is False and tg.chat_id(db) is None
    tg.pair(db, 4455667788, "bandile")
    db.commit()
    assert tg.is_paired(db) is True and tg.chat_id(db) == 4455667788
    item = {i["provider"]: i for i in client.get("/integrations").json()}["telegram"]
    assert item["status"] == "connected" and item["account_label"] == "bandile"
    assert item["actions"] == ["set_webhook", "disconnect"]

    r = client.post("/integrations/telegram/unpair")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    db.expire_all()
    assert tg.is_paired(db) is False
    db.close()


def test_bot_link_comes_from_the_username(monkeypatch):
    assert tg.bot_link() is None
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@FounderOsBot")
    assert tg.bot_link() == "https://t.me/FounderOsBot"


# ── set-webhook ──────────────────────────────────────────────

def test_set_webhook_is_503_until_everything_is_set(monkeypatch):
    with patch.object(tg.httpx, "post") as post:
        r = client.post("/integrations/telegram/set-webhook")
        assert r.status_code == 503
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
        r = client.post("/integrations/telegram/set-webhook")
        assert r.status_code == 503 and "FOUNDER_OS_API_URL" in r.json()["detail"]
        assert BOT_TOKEN not in r.text
        monkeypatch.setenv("FOUNDER_OS_API_URL", "https://founder.example/")
        r = client.post("/integrations/telegram/set-webhook")
        assert r.status_code == 503 and "TELEGRAM_WEBHOOK_SECRET" in r.json()["detail"]
    post.assert_not_called()


def test_set_webhook_posts_the_url_and_secret_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("FOUNDER_OS_API_URL", "https://founder.example/")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", HOOK_SECRET)
    with patch.object(tg.httpx, "post", return_value=_resp({"ok": True, "description": "Webhook was set"})) as post:
        r = client.post("/integrations/telegram/set-webhook")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["description"] == "Webhook was set"
    assert body["url"] == "https://founder.example/integrations/telegram/webhook"
    assert BOT_TOKEN not in r.text and HOOK_SECRET not in r.text

    (url,), kwargs = post.call_args
    assert url == f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    assert kwargs["json"] == {"url": "https://founder.example/integrations/telegram/webhook",
                              "secret_token": HOOK_SECRET, "allowed_updates": ["message"]}
    assert kwargs["timeout"]


def test_set_webhook_reports_a_refusal_without_leaking_the_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("FOUNDER_OS_API_URL", "https://founder.example")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", HOOK_SECRET)
    refusal = {"ok": False, "description": f"Bad Request: bad webhook for bot{BOT_TOKEN}"}
    with patch.object(tg.httpx, "post", return_value=_resp(refusal)):
        r = client.post("/integrations/telegram/set-webhook")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert BOT_TOKEN not in r.text and "Bad Request" in r.json()["description"]

    with patch.object(tg.httpx, "post", side_effect=RuntimeError(f"connect failed for /bot{BOT_TOKEN}/setWebhook")):
        r = client.post("/integrations/telegram/set-webhook")
    assert r.status_code == 200 and r.json()["ok"] is False and BOT_TOKEN not in r.text


def test_failed_webhook_call_prints_nothing_secret(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("FOUNDER_OS_API_URL", "https://founder.example")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", HOOK_SECRET)
    with patch.object(tg.httpx, "post", side_effect=RuntimeError(f"POST /bot{BOT_TOKEN}/setWebhook secret {HOOK_SECRET}")):
        client.post("/integrations/telegram/set-webhook")
    printed = capsys.readouterr().out
    assert "setWebhook failed" in printed and BOT_TOKEN not in printed and HOOK_SECRET not in printed


# ── Schema ───────────────────────────────────────────────────

def test_calendar_events_are_unique_per_provider_and_external_id():
    db = _db()
    db.add(models.CalendarEvent(provider="google", external_id="evt-1", title="One"))
    db.commit()
    db.add(models.CalendarEvent(provider="google", external_id="evt-1", title="Again"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_email_message_ids_are_unique():
    db = _db()
    db.add(models.EmailMessage(message_id="<a@x>", direction="in"))
    db.commit()
    db.add(models.EmailMessage(message_id="<a@x>", direction="in"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_migration_runs_twice_and_adds_replied_at():
    from sqlalchemy import inspect
    from database import engine as app_engine
    from migrations.m021_integrations import run
    run()
    run()
    inspector = inspect(app_engine)
    tables = set(inspector.get_table_names())
    assert {"integrations", "calendar_events", "email_messages"} <= tables
    if "outreach_messages" in tables:
        assert "replied_at" in {c["name"] for c in inspector.get_columns("outreach_messages")}


def test_outreach_message_serialiser_carries_replied_at():
    import leads
    db = _db()
    message = models.OutreachMessage(lead_id=1, channel="email", body="Hello", status="sent",
                                     replied_at=datetime(2026, 11, 12, 9, 0))
    db.add(message)
    db.commit()
    assert leads.serialize_message(db, message)["replied_at"] == datetime(2026, 11, 12, 9, 0)
    db.close()


# ════════════════════════════════════════════════════════════════
# 6C.2 — Google Calendar (spec tests 1–4, 8 and 9)
# ════════════════════════════════════════════════════════════════

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import agents
import briefing
import change_log
import integrations_google as gcal
import notify
import orchestrator_tools as ot
from agents import calendar_sync, runner, schedule

ACCESS = "ya29.access-token-value-0001"
REFRESH = "1//refresh-token-value-0001"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id.apps.example")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-client-secret")
    monkeypatch.setenv("FOUNDER_OS_API_URL", "https://founder.example")
    monkeypatch.setenv("FOUNDER_OS_SECRET", "server-secret-for-state")
    monkeypatch.setenv("FOUNDER_OS_TZ", "Africa/Lusaka")
    with patch.object(runner, "SessionLocal", TestSessionLocal):
        yield


def _connect(db, *, expiry="2099-01-01T00:00:00", refresh=REFRESH):
    row = integrations.get_or_create(db, "google_calendar")
    row.status, row.account_label = "connected", "primary"
    row.secret_json = json.dumps({"access_token": ACCESS, "refresh_token": refresh, "expiry": expiry})
    db.commit()
    return row


def _http(status=200, payload=None):
    return SimpleNamespace(status_code=status, json=lambda: payload if payload is not None else {},
                           content=b"{}" if status != 204 else b"")


def _state_of(url):
    return parse_qs(urlparse(url).query)["state"][0]


# ── 1. OAuth ─────────────────────────────────────────────────

def test_oauth_start_builds_the_url_and_remembers_a_state(google, monkeypatch):
    r = client.get("/integrations/google/start")
    assert r.status_code == 200
    url = urlparse(r.json()["url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["client_id"] == "client-id.apps.example" and q["response_type"] == "code"
    assert q["redirect_uri"] == "https://founder.example/integrations/google/callback"
    assert q["scope"] == "https://www.googleapis.com/auth/calendar.events"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    nonce = q["state"].split(".")[0]
    db = _db()
    assert integrations.config(integrations.get_or_create(db, "google_calendar"))["pending_state"] == nonce
    assert "GOCSPX-client-secret" not in r.text
    db.close()

    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    assert client.get("/integrations/google/start").status_code == 503


def test_callback_with_a_bad_state_stores_nothing(google):
    good = _state_of(client.get("/integrations/google/start").json()["url"])
    nonce, stamp, signature = good.split(".")
    forged = [f"{nonce}.{stamp}.{'0' * 64}", f"other.{stamp}.{signature}", "nonsense", ""]
    with patch.object(gcal.httpx, "post") as post:
        for state in forged:
            r = client.get("/integrations/google/callback", params={"code": "abc", "state": state},
                           follow_redirects=False)
            assert r.status_code == 302 and r.headers["location"] == "/app/#settings?error=google_state"
        denied = client.get("/integrations/google/callback", params={"error": "access_denied", "state": good},
                            follow_redirects=False)
        assert denied.headers["location"] == "/app/#settings?error=google_denied"
        with patch.object(gcal.time, "time", return_value=gcal.time.time() + 601):     # ten minutes have passed
            late = client.get("/integrations/google/callback", params={"code": "abc", "state": good},
                              follow_redirects=False)
        assert late.headers["location"] == "/app/#settings?error=google_state"
    post.assert_not_called()
    db = _db()
    row = integrations.get_or_create(db, "google_calendar")
    assert row.status == "disconnected" and row.secret_json is None
    db.close()


def test_callback_with_a_good_state_connects_and_never_returns_the_token(google):
    state = _state_of(client.get("/integrations/google/start").json()["url"])
    token = _http(200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599})
    with patch.object(gcal.httpx, "post", return_value=token) as post, \
            patch.object(gcal.httpx, "get", return_value=_http(200, {"items": []})):
        r = client.get("/integrations/google/callback", params={"code": "auth-code", "state": state},
                       follow_redirects=False)
        again = client.get("/integrations/google/callback", params={"code": "auth-code", "state": state},
                           follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/app/#settings?connected=google"
    assert again.headers["location"] == "/app/#settings?error=google_state"            # a state works once
    (url,), kwargs = post.call_args
    assert url == "https://oauth2.googleapis.com/token" and post.call_count == 1
    assert kwargs["data"] == {"code": "auth-code", "client_id": "client-id.apps.example",
                              "client_secret": "GOCSPX-client-secret",
                              "redirect_uri": "https://founder.example/integrations/google/callback",
                              "grant_type": "authorization_code"}
    db = _db()
    row = integrations.get_or_create(db, "google_calendar")
    held = json.loads(row.secret_json)
    assert row.status == "connected" and held["access_token"] == ACCESS and held["refresh_token"] == REFRESH
    assert row.last_sync_at is not None and "pending_state" not in integrations.config(row)
    listed = client.get("/integrations")
    assert ACCESS not in listed.text and REFRESH not in listed.text and "GOCSPX" not in listed.text
    item = {i["provider"]: i for i in listed.json()}["google_calendar"]
    assert item["status"] == "connected" and item["actions"] == ["sync", "disconnect"]
    db.close()


def test_a_refused_exchange_redirects_with_an_error(google):
    state = _state_of(client.get("/integrations/google/start").json()["url"])
    with patch.object(gcal.httpx, "post", return_value=_http(400, {"error": "invalid_grant"})):
        r = client.get("/integrations/google/callback", params={"code": "bad", "state": state},
                       follow_redirects=False)
    assert r.headers["location"] == "/app/#settings?error=google_exchange"
    db = _db()
    assert integrations.get_or_create(db, "google_calendar").secret_json is None
    db.close()


# ── 8. What needs a session ──────────────────────────────────

def test_only_the_callback_is_reachable_without_a_session(google, monkeypatch):
    import auth
    assert "/integrations/google/callback" in auth.EXEMPT_EXACT
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "a-long-password")
    assert client.get("/integrations").status_code == 401
    assert client.get("/integrations/google/start").status_code == 401
    assert client.get("/calendar/today").status_code == 401
    r = client.get("/integrations/google/callback", params={"code": "x", "state": "y"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/app/#settings?error=google_state"


def test_the_oauth_state_is_not_signed_with_anything_derived_from_the_password(google, monkeypatch):
    """Review: with FOUNDER_OS_SECRET unset the state was signed with sha256("founder-os:" +
    password). The state travels through Google, the browser history and the access log, so
    whoever saw one could test password guesses offline."""
    import hashlib
    import hmac as hmac_lib
    monkeypatch.delenv("FOUNDER_OS_SECRET", raising=False)
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "hunter2")
    db = _db()
    state = gcal.new_state(db)
    nonce, stamp, signature = state.split(".")
    from_password = hmac_lib.new(hashlib.sha256(b"founder-os:hunter2").digest(), f"{nonce}:{stamp}".encode(),
                                 hashlib.sha256).hexdigest()
    assert signature != from_password
    assert gcal.check_state(db, state) is True and gcal.check_state(db, state) is False      # still single use
    forged = f"{nonce}.{stamp}.{from_password}"
    gcal.new_state(db)
    assert gcal.check_state(db, forged) is False
    db.close()


# ── 2. Token refresh ─────────────────────────────────────────

def test_an_expired_token_is_refreshed(google):
    db = _db()
    _connect(db, expiry="2020-01-01T00:00:00")
    fresh = _http(200, {"access_token": "ya29.fresh-token-value-0002", "expires_in": 3599})
    with patch.object(gcal.httpx, "post", return_value=fresh) as post:
        assert gcal._token(db) == "ya29.fresh-token-value-0002"
        assert gcal._token(db) == "ya29.fresh-token-value-0002"                        # still good: no second call
    assert post.call_count == 1
    assert post.call_args.kwargs["data"] == {"client_id": "client-id.apps.example",
                                             "client_secret": "GOCSPX-client-secret",
                                             "refresh_token": REFRESH, "grant_type": "refresh_token"}
    held = integrations.secrets(integrations.get_or_create(db, "google_calendar"))
    assert held["refresh_token"] == REFRESH and held["expiry"] > "2026"
    db.close()


def test_invalid_grant_puts_the_integration_in_error(google):
    db = _db()
    _connect(db, expiry="2020-01-01T00:00:00")
    assert not any(a["rule_id"] == "integration_error" for a in ot.detect_off_track(db)[0])
    with patch.object(gcal.httpx, "post", return_value=_http(400, {"error": "invalid_grant"})):
        assert gcal._token(db) is None
        counts = gcal.sync(db)
    assert counts["ok"] is False and counts["error"] == "Reconnect Google Calendar"
    item = {i["provider"]: i for i in client.get("/integrations").json()}["google_calendar"]
    assert item["status"] == "error" and item["last_error"] == "Reconnect Google Calendar"
    assert item["actions"] == ["connect", "disconnect"]
    alert = next(a for a in ot.detect_off_track(db)[0] if a["rule_id"] == "integration_error")
    assert alert["severity"] == "warn" and "Settings" in alert["message"]
    assert client.get("/briefing/today").json()["integrations"]["google_calendar"] == "error"
    db.close()


# ── 3. Sync ──────────────────────────────────────────────────

NOW = datetime(2026, 11, 12, 10, 0)                  # Thursday, 12:00 in Lusaka

TIMED = {"id": "evt-timed", "summary": "Further Maths P1 past paper", "status": "confirmed",
         "start": {"dateTime": "2026-11-13T09:00:00+02:00"}, "end": {"dateTime": "2026-11-13T10:30:00+02:00"},
         "location": "Library", "htmlLink": "https://calendar.example/evt-timed",
         "updated": "2026-11-10T08:00:00.000Z", "description": "d" * 1500}
ALL_DAY = {"id": "evt-day", "summary": "Mock exams", "start": {"date": "2026-11-14"},
           "end": {"date": "2026-11-15"}, "updated": "2026-11-10T08:00:00.000Z"}
CANCELLED = {"id": "evt-gone", "summary": "Dropped", "status": "cancelled",
             "start": {"dateTime": "2026-11-13T12:00:00+02:00"}, "end": {"dateTime": "2026-11-13T13:00:00+02:00"}}
ZULU = {"id": "evt-zulu", "start": {"dateTime": "2026-11-12T22:30:00Z"}, "end": {"dateTime": "2026-11-12T23:00:00Z"}}


def _events():
    return db_events(_db())


def db_events(db):
    rows = db.query(models.CalendarEvent).order_by(models.CalendarEvent.external_id).all()
    out = {e.external_id: e for e in rows}
    return out


def test_sync_upserts_converts_and_soft_deletes(google):
    db = _db()
    _connect(db)
    pages = [_http(200, {"items": [TIMED, CANCELLED], "nextPageToken": "page-2"}),
             _http(200, {"items": [ALL_DAY, ZULU]})]
    with patch.object(gcal.httpx, "get", side_effect=pages) as get:
        counts = gcal.sync(db, now=NOW)
    assert counts == {"ok": True, "created": 3, "updated": 0, "deleted": 0, "unchanged": 0, "error": None}
    first, second = get.call_args_list
    assert first.args == (EVENTS_URL,) and first.kwargs["headers"] == {"Authorization": f"Bearer {ACCESS}"}
    assert first.kwargs["params"] == {"timeMin": "2026-11-11T10:00:00Z", "timeMax": "2026-12-12T10:00:00Z",
                                      "singleEvents": "true", "orderBy": "startTime", "maxResults": 250}
    assert second.kwargs["params"]["pageToken"] == "page-2"

    events = db_events(db)
    assert set(events) == {"evt-timed", "evt-day", "evt-zulu"}                         # the cancelled one is not kept
    timed = events["evt-timed"]
    assert timed.title == "Further Maths P1 past paper" and timed.location == "Library"
    assert timed.start_at == datetime(2026, 11, 13, 7, 0) and timed.end_at == datetime(2026, 11, 13, 8, 30)   # UTC
    assert timed.start_date == date(2026, 11, 13) and timed.all_day is False and timed.created_by_us is False
    assert len(timed.description) == 1000 and timed.remote_updated_at == datetime(2026, 11, 10, 8, 0)
    day = events["evt-day"]
    assert day.all_day is True and day.start_date == date(2026, 11, 14)
    assert day.start_at == datetime(2026, 11, 13, 22, 0) and day.end_at == datetime(2026, 11, 14, 22, 0)     # local midnights
    zulu = events["evt-zulu"]
    assert zulu.title == "(no title)" and zulu.start_date == date(2026, 11, 13)        # 00:30 local the next day

    emitted = db.query(models.AgentEvent).filter_by(kind="calendar.changed").all()
    assert len(emitted) == 1 and json.loads(emitted[0].payload_json) == {"created": 3, "updated": 0, "deleted": 0}
    assert db.query(models.ChangeLog).count() == 0                                     # a cache: nothing is logged
    row = integrations.get_or_create(db, "google_calendar")
    assert row.last_sync_at == NOW and row.status == "connected"

    # The same answer again changes nothing and emits nothing.
    same = [_http(200, {"items": [TIMED, CANCELLED, ALL_DAY, ZULU]})]
    with patch.object(gcal.httpx, "get", side_effect=same):
        again = gcal.sync(db, now=NOW + timedelta(minutes=30))
    assert (again["created"], again["updated"], again["deleted"], again["unchanged"]) == (0, 0, 0, 3)
    assert db.query(models.AgentEvent).filter_by(kind="calendar.changed").count() == 1

    # One event moved, one vanished.
    moved = {**TIMED, "start": {"dateTime": "2026-11-13T11:00:00+02:00"}, "end": {"dateTime": "2026-11-13T12:00:00+02:00"}}
    with patch.object(gcal.httpx, "get", side_effect=[_http(200, {"items": [moved, ZULU]})]):
        third = gcal.sync(db, now=NOW + timedelta(hours=1))
    assert (third["created"], third["updated"], third["deleted"], third["unchanged"]) == (0, 1, 1, 1)
    db.expire_all()
    events = db_events(db)
    assert events["evt-timed"].start_at == datetime(2026, 11, 13, 9, 0)
    assert events["evt-day"].deleted_at == NOW + timedelta(hours=1)                    # kept, marked gone
    assert db.query(models.AgentEvent).filter_by(kind="calendar.changed").count() == 2

    # It comes back upstream: the row is revived.
    with patch.object(gcal.httpx, "get", side_effect=[_http(200, {"items": [moved, ALL_DAY, ZULU]})]):
        fourth = gcal.sync(db, now=NOW + timedelta(hours=2))
    assert fourth["updated"] == 1
    db.expire_all()
    assert db_events(db)["evt-day"].deleted_at is None
    db.close()


def test_sync_reports_failure_without_raising(google):
    db = _db()
    assert gcal.sync(db)["error"] == "Calendar not connected"
    _connect(db)
    boom = RuntimeError(f"connect failed with Bearer {ACCESS}")
    with patch.object(gcal.httpx, "get", side_effect=boom):
        counts = gcal.sync(db, now=NOW)
    assert counts["ok"] is False and ACCESS not in counts["error"]
    row = integrations.get_or_create(db, "google_calendar")
    assert row.status == "connected" and row.last_error is None        # a network failure is retried, not an error
    with patch.object(gcal.httpx, "get", return_value=_http(401, {"error": {"message": "Invalid Credentials"}})):
        assert gcal.sync(db, now=NOW)["error"] == "Reconnect Google Calendar"
    db.close()


def test_calendar_sync_agent_runs_only_when_connected(google):
    db = _db()
    spec = calendar_sync.SPEC
    assert spec.schedule.interval_minutes == 30 and spec.permissions.max_llm_calls == 0
    assert spec.permissions.tables_write == ["calendar_events", "integrations"]
    assert spec.permissions.may_emit == ["calendar.changed"]
    assert runner.run_agent(spec, "schedule", now=NOW)["summary"] == "calendar not connected"
    _connect(db)
    with patch.object(gcal.httpx, "get", side_effect=[_http(200, {"items": [TIMED]})]):
        out = runner.run_agent(spec, "schedule", now=NOW)
    assert out["status"] == "completed" and out["summary"] == "1 new, 0 changed, 0 removed"
    with patch.object(gcal.httpx, "get", side_effect=[_http(200, {"items": [TIMED]})]):
        assert runner.run_agent(spec, "schedule", now=NOW + timedelta(minutes=30))["status"] == "skipped"
    db.close()


# ── 4. Creating an event, and undoing it ─────────────────────

class _Clock(datetime):
    """datetime with a fixed utcnow(): Thursday 12 November 2026, 10:00 in Lusaka."""

    @classmethod
    def utcnow(cls):
        return cls(2026, 11, 12, 8, 0)


def test_calendar_add_event_creates_remotely_and_undo_deletes_it(google):
    db = _db()
    _connect(db)
    created = _http(200, {"id": "evt-new", "htmlLink": "https://calendar.example/evt-new", "status": "confirmed"})
    with patch.object(ot, "datetime", _Clock), patch.object(gcal.httpx, "post", return_value=created) as post:
        result, summary = ot.run_tool("calendar_add_event", {"title": "Clinic demo", "when": "Friday 14:00",
                                                             "location": "Kabulonga"}, db)
    assert summary == "'Clinic demo' added to the calendar for tomorrow 14:00"
    (url,), kwargs = post.call_args
    assert url == EVENTS_URL and kwargs["headers"] == {"Authorization": f"Bearer {ACCESS}"}
    assert kwargs["json"] == {"summary": "Clinic demo", "location": "Kabulonga",
                              "start": {"dateTime": "2026-11-13T14:00:00+02:00", "timeZone": "Africa/Lusaka"},
                              "end": {"dateTime": "2026-11-13T15:00:00+02:00", "timeZone": "Africa/Lusaka"}}
    event = db.query(models.CalendarEvent).one()
    assert event.external_id == "evt-new" and event.created_by_us is True
    assert event.start_at == datetime(2026, 11, 13, 12, 0) and event.start_date == date(2026, 11, 13)
    change = result["change"]
    assert change["table"] == "calendar_events" and change["action"] == "create"
    assert change["summary"] == "Calendar: Clinic demo 14:00"

    with patch.object(gcal.httpx, "delete", return_value=_http(204)) as delete:
        change_log.undo_change(db, change["id"])
    assert delete.call_args.args == (f"{EVENTS_URL}/evt-new",)
    assert db.query(models.CalendarEvent).count() == 0
    db.close()


def test_undo_still_removes_the_row_when_google_refuses(google):
    db = _db()
    _connect(db)
    created = _http(200, {"id": "evt-two"})
    with patch.object(ot, "datetime", _Clock), patch.object(gcal.httpx, "post", return_value=created):
        result, _ = ot.run_tool("calendar_add_event", {"title": "Call Wamu", "when": "tomorrow at 9am",
                                                       "duration_minutes": 30}, db)
    assert result["when"] == "tomorrow 09:00" and result["ends"] == "09:30"
    with patch.object(gcal.httpx, "delete", side_effect=RuntimeError("network down")):
        change_log.undo_change(db, result["change"]["id"])
    assert db.query(models.CalendarEvent).count() == 0
    db.close()


def test_calendar_add_event_needs_a_time_and_a_connection(google):
    db = _db()
    with patch.object(ot, "datetime", _Clock), patch.object(gcal.httpx, "post") as post:
        unconnected, summary = ot.run_tool("calendar_add_event", {"title": "Clinic demo", "when": "Friday 14:00"}, db)
        assert unconnected == {"error": "Calendar not connected"} and summary == "Calendar not connected"
        _connect(db)
        no_time, summary = ot.run_tool("calendar_add_event", {"title": "Clinic demo", "when": "Friday"}, db)
        assert no_time == {"error": "need a time"} and summary == "need a time"
        unreadable, _ = ot.run_tool("calendar_add_event", {"title": "Clinic demo", "when": "sometime soon"}, db)
        untitled, _ = ot.run_tool("calendar_add_event", {"title": " ", "when": "Friday 14:00"}, db)
        assert "error" in unreadable and "error" in untitled
    post.assert_not_called()
    with patch.object(ot, "datetime", _Clock), \
            patch.object(gcal.httpx, "post", return_value=_http(403, {"error": {"message": "Forbidden"}})):
        refused, _ = ot.run_tool("calendar_add_event", {"title": "Clinic demo", "when": "Friday 14:00"}, db)
    assert refused == {"error": "Forbidden"}
    assert db.query(models.CalendarEvent).count() == 0 and db.query(models.ChangeLog).count() == 0
    db.close()


def test_calendar_today_tool_and_endpoints(google):
    db = _db()
    off, summary = ot.run_tool("calendar_today", {}, db)
    assert off == {"connected": False, "message": "Calendar not connected"} and summary == "Calendar not connected"
    assert client.get("/calendar/today").json() == {"connected": False, "events": []}
    assert client.post("/calendar/events", json={"title": "x", "start": "2030-01-01T10:00"}).status_code == 503

    _connect(db)
    with patch.object(gcal.httpx, "get", side_effect=[_http(200, {"items": [TIMED, ALL_DAY]})]):
        gcal.sync(db, now=NOW)
    with patch.object(ot, "datetime", _Clock):
        tomorrow, summary = ot.run_tool("calendar_today", {"date": "tomorrow"}, db)
        nothing, quiet = ot.run_tool("calendar_today", {}, db)
    assert summary == "1 event(s) 2026-11-13" and quiet == "nothing in the calendar today"
    assert tomorrow["events"] == [{"title": "Further Maths P1 past paper", "starts": "09:00", "ends": "10:30",
                                   "location": "Library"}]
    assert nothing["events"] == []

    day = client.get("/calendar/today", params={"date": "2026-11-14"}).json()
    assert day["connected"] is True and [e["start_local"] for e in day["events"]] == ["all day"]
    week = client.get("/calendar/week", params={"from": "2026-11-12"}).json()["days"]
    assert len(week) == 7 and [len(d["events"]) for d in week[:3]] == [0, 1, 1]
    assert client.get("/calendar/today", params={"date": "12 Nov"}).status_code == 422

    made = _http(200, {"id": "evt-ui"})
    with patch.object(gcal.httpx, "post", return_value=made) as post:
        r = client.post("/calendar/events", json={"title": "Site visit", "start": "2030-03-01T08:30",
                                                  "duration_minutes": 45})
        assert client.post("/calendar/events", json={"title": "Site visit", "start": "someday"}).status_code == 422
    assert r.status_code == 200 and r.json()["start_local"] == "08:30" and r.json()["end_local"] == "09:15"
    assert r.json()["change"]["table"] == "calendar_events"
    assert post.call_args.kwargs["json"]["start"]["dateTime"] == "2030-03-01T08:30:00+02:00"
    db.close()


def test_disconnect_revokes_forgets_and_retires_the_cache(google):
    db = _db()
    _connect(db)
    with patch.object(gcal.httpx, "get", side_effect=[_http(200, {"items": [TIMED]})]):
        gcal.sync(db, now=NOW)
    with patch.object(gcal.httpx, "post", return_value=_http(200)) as post:
        r = client.post("/integrations/google/disconnect")
    assert post.call_args.args == ("https://oauth2.googleapis.com/revoke",)
    assert post.call_args.kwargs["params"] == {"token": REFRESH}
    assert r.json()["status"] == "disconnected" and r.json()["actions"] == ["connect"]
    db.expire_all()
    assert integrations.get_or_create(db, "google_calendar").secret_json is None
    assert db.query(models.CalendarEvent).one().deleted_at is not None
    assert gcal.connected(db) is False and client.post("/integrations/google/sync").json()["ok"] is False
    db.close()


# ── 9. Briefing ──────────────────────────────────────────────

def _event(db, title, start_local, minutes=60, **kw):
    start = schedule.to_utc(start_local.replace(tzinfo=schedule.tz()))
    row = models.CalendarEvent(provider="google", external_id=f"evt-{title}", title=title, start_at=start,
                               end_at=start + timedelta(minutes=minutes), start_date=start_local.date(),
                               status="confirmed", **kw)
    db.add(row)
    db.commit()
    return row


def test_briefing_lists_timed_events_between_fifty_and_seventy(google):
    db = _db()
    clock = datetime(2026, 11, 12, 8, 0)                                               # 08:00 local
    assert briefing.build_briefing(db, now=clock, with_summary=False)["calendar"] == {
        "connected": False, "events_today": [], "all_day": []}
    _connect(db)
    _event(db, "Standup", datetime(2026, 11, 12, 7, 30))                               # under way until 08:30
    _event(db, "Past paper", datetime(2026, 11, 12, 9, 0), location="Library")
    _event(db, "Evening call", datetime(2026, 11, 12, 20, 0))
    _event(db, "Early run", datetime(2026, 11, 12, 5, 0))                              # already over
    _event(db, "Tomorrow thing", datetime(2026, 11, 13, 9, 0))
    _event(db, "Mock exams", datetime(2026, 11, 12, 0, 0), minutes=1440, all_day=True)
    gone = _event(db, "Cancelled", datetime(2026, 11, 12, 10, 0))
    gone.deleted_at = datetime.utcnow()
    db.commit()

    b = briefing.build_briefing(db, now=clock, with_summary=False)
    events = {i["title"]: i for i in b["items"] if i["category"] == "event"}
    assert set(events) == {"Standup", "Past paper", "Evening call"}
    assert events["Standup"]["score"] == 70 and events["Past paper"]["score"] == 68 and events["Evening call"]["score"] == 50
    assert events["Past paper"]["due_label"] == "09:00" and events["Past paper"]["reason"] == "Library"
    assert events["Evening call"]["reason"] == "Calendar" and events["Past paper"]["suggested_action"] == "Be there"
    assert events["Past paper"]["target_type"] == "calendar_event" and events["Past paper"]["completable"] is False

    cal = b["calendar"]
    assert cal["connected"] is True
    assert [e["title"] for e in cal["events_today"]] == ["Early run", "Standup", "Past paper", "Evening call"]
    assert [e["title"] for e in cal["all_day"]] == ["Mock exams"] and b["today_events"] == ["Mock exams"]
    assert b["integrations"] == {"email": "disconnected", "telegram": False, "google_calendar": "connected"}

    text = notify.render_briefing(b, suppressed_overnight=[])
    assert "TODAY\n" in text and "• 09:00 Past paper (calendar)" in text and "• 20:00 Evening call (calendar)" in text
    db.close()


# ── Review round: transient failures ─────────────────────────

def _status(db):
    db.expire_all()
    row = integrations.get_or_create(db, "google_calendar")
    return row.status, row.last_error


def test_a_transient_google_failure_never_puts_the_integration_in_error(google):
    """Review item 5: a timeout or a 5xx is retried on the next sync; only invalid_grant or
    a revoked token (401) means the user has to reconnect."""
    import httpx

    db = _db()
    _connect(db)
    # The Calendar API times out, then answers 503, then 500 with no JSON body.
    with patch.object(gcal.httpx, "get", side_effect=httpx.ReadTimeout("timed out")):
        counts = gcal.sync(db, now=NOW)
    assert counts["ok"] is False and counts["transient"] is True and "could not be reached" in counts["error"]
    assert _status(db) == ("connected", None)
    with patch.object(gcal.httpx, "get", return_value=_http(503, {"error": {"message": "Backend Error"}})):
        counts = gcal.sync(db, now=NOW)
    assert counts["ok"] is False and counts["transient"] is True
    assert _status(db) == ("connected", None)
    broken = SimpleNamespace(status_code=500, json=lambda: (_ for _ in ()).throw(ValueError("no json")), content=b"<html>")
    with patch.object(gcal.httpx, "get", return_value=broken):
        assert gcal.sync(db, now=NOW)["ok"] is False
    assert _status(db) == ("connected", None)
    assert client.get("/integrations").json()[0]["provider"]          # the listing still renders
    item = {i["provider"]: i for i in client.get("/integrations").json()}["google_calendar"]
    assert item["status"] == "connected" and item["last_error"] is None
    assert not any(a["rule_id"] == "integration_error" for a in ot.detect_off_track(db)[0])

    # The token refresh times out, then Google's token endpoint answers 500: tokens are kept.
    _connect(db, expiry="2020-01-01T00:00:00")
    with patch.object(gcal.httpx, "post", side_effect=httpx.ConnectTimeout("timed out")):
        assert gcal.sync(db, now=NOW)["transient"] is True
    assert _status(db) == ("connected", None)
    with patch.object(gcal.httpx, "post", return_value=_http(500, {"error": "internal_failure"})):
        assert gcal.sync(db, now=NOW)["transient"] is True
    assert _status(db) == ("connected", None)
    held = integrations.secrets(integrations.get_or_create(db, "google_calendar"))
    assert held["refresh_token"] == REFRESH and held["access_token"] == ACCESS

    # The next sync simply works.
    fresh = _http(200, {"access_token": "ya29.fresh-token-value-0003", "expires_in": 3599})
    with patch.object(gcal.httpx, "post", return_value=fresh), \
            patch.object(gcal.httpx, "get", return_value=_http(200, {"items": [TIMED]})):
        counts = gcal.sync(db, now=NOW)
    assert counts["ok"] is True and counts["created"] == 1
    assert _status(db) == ("connected", None)

    # A revoked token is not transient.
    with patch.object(gcal.httpx, "get", return_value=_http(401, {"error": {"message": "Invalid Credentials"}})):
        counts = gcal.sync(db, now=NOW)
    assert counts["transient"] is False and _status(db) == ("error", "Reconnect Google Calendar")
    db.close()


def test_a_transient_failure_does_not_clear_a_real_error(google):
    db = _db()
    _connect(db, expiry="2020-01-01T00:00:00")
    with patch.object(gcal.httpx, "post", return_value=_http(400, {"error": "invalid_grant"})):
        assert gcal.sync(db, now=NOW)["transient"] is False
    assert _status(db) == ("error", "Reconnect Google Calendar")
    import httpx
    with patch.object(gcal.httpx, "post", side_effect=httpx.ConnectError("dns")):
        gcal.sync(db, now=NOW)
    assert _status(db) == ("error", "Reconnect Google Calendar")
    db.close()


# ── Review round: multi-day events ───────────────────────────

CONFERENCE = {"id": "evt-conf", "summary": "Kitwe trade fair", "start": {"date": "2026-11-16"},
              "end": {"date": "2026-11-19"}}                                   # 16th, 17th and 18th (end is exclusive)
NIGHT = {"id": "evt-night", "summary": "Overnight bus to Livingstone",
         "start": {"dateTime": "2026-11-20T22:00:00+02:00"}, "end": {"dateTime": "2026-11-21T05:30:00+02:00"}}
RETREAT = {"id": "evt-retreat", "summary": "Youth retreat",
           "start": {"dateTime": "2026-11-23T17:00:00+02:00"}, "end": {"dateTime": "2026-11-25T12:00:00+02:00"}}


def test_a_multi_day_event_is_on_every_day_it_spans(google):
    """Review item 6: an event was only listed on the day it starts."""
    db = _db()
    _connect(db)
    with patch.object(gcal.httpx, "get", side_effect=[_http(200, {"items": [CONFERENCE, NIGHT, RETREAT, TIMED]})]):
        assert gcal.sync(db, now=NOW)["created"] == 4

    def titles(day):
        return [(e["title"], e["start_local"], e["end_local"]) for e in gcal.today(db, date.fromisoformat(day))]

    fair = ("Kitwe trade fair", "all day", None)
    assert titles("2026-11-15") == []
    assert titles("2026-11-16") == [fair] and titles("2026-11-17") == [fair] and titles("2026-11-18") == [fair]
    assert titles("2026-11-19") == []                                         # Google's end date is exclusive
    # A timed event that runs past midnight is on both days; the second day says when it ends.
    assert titles("2026-11-20") == [("Overnight bus to Livingstone", "22:00", "05:30")]
    assert titles("2026-11-21") == [("Overnight bus to Livingstone", "until 05:30", "05:30")]
    assert titles("2026-11-22") == []
    # Three days: the evening it starts, the whole middle day, the morning it ends.
    assert titles("2026-11-23") == [("Youth retreat", "17:00", "12:00")]
    assert titles("2026-11-24") == [("Youth retreat", "all day", None)]
    assert titles("2026-11-25") == [("Youth retreat", "until 12:00", "12:00")]
    assert titles("2026-11-13") == [("Further Maths P1 past paper", "09:00", "10:30")]   # single-day: unchanged

    week = client.get("/calendar/week", params={"from": "2026-11-16"}).json()["days"]
    assert [len(d["events"]) for d in week] == [1, 1, 1, 0, 1, 1, 0]
    with patch.object(ot, "datetime", _Clock):
        result, summary = ot.run_tool("calendar_today", {"date": "2026-11-17"}, db)
    assert summary == "1 event(s) 2026-11-17" and result["events"][0]["title"] == "Kitwe trade fair"

    # The briefing on the middle day of the fair lists it; on the retreat's last morning it is an item.
    block = briefing.build_briefing(db, now=datetime(2026, 11, 17, 8, 0), with_summary=False)
    assert block["today_events"] == ["Kitwe trade fair"]
    last = briefing.build_briefing(db, now=datetime(2026, 11, 25, 8, 0), with_summary=False)
    assert [(e["title"], e["start_local"]) for e in last["calendar"]["events_today"]] == [("Youth retreat", "until 12:00")]
    assert any(i["category"] == "event" and i["title"] == "Youth retreat" for i in last["items"])
    middle = briefing.build_briefing(db, now=datetime(2026, 11, 24, 8, 0), with_summary=False)
    assert middle["today_events"] == ["Youth retreat"]
    db.close()


# ════════════════════════════════════════════════════════════════
# 6C.3 — email over IMAP and SMTP (spec tests 5, 6 and the email half of 7)
# ════════════════════════════════════════════════════════════════

import base64
import imaplib

import integrations_email as mail
from agents import email_sync

MAILBOX = "bandile@example.com"
APP_PASSWORD = "abcd efgh ijkl mnop"
OUTREACH_ID = "<outreach-0001@example.com>"


@pytest.fixture
def mailbox(monkeypatch):
    monkeypatch.setenv("EMAIL_ADDRESS", MAILBOX)
    monkeypatch.setenv("EMAIL_APP_PASSWORD", APP_PASSWORD)
    monkeypatch.setenv("FOUNDER_OS_TZ", "Africa/Lusaka")
    with patch.object(runner, "SessionLocal", TestSessionLocal):
        yield


def _raw(sender, subject, body, *, message_id, in_reply_to=None, thread="1700",
         content_type="text/plain; charset=utf-8", encoding=None, date_text="Thu, 12 Nov 2026 09:30:00 +0200",
         auth="pass"):
    """One IMAP FETCH response, shaped as imaplib returns it. ``auth`` is the
    Authentication-Results header(s) the receiving server put on top: "pass" builds a passing
    one for the sender's own domain, None leaves it out, a list gives the headers top-down."""
    from email.utils import parseaddr
    domain = parseaddr(sender)[1].split("@")[-1].lower()
    if auth == "pass":
        auth = [f"mx.google.com; dkim=pass header.i=@{domain} header.s=s1; spf=pass smtp.mailfrom=bounce@{domain}; "
                f"dmarc=pass (p=NONE sp=NONE dis=NONE) header.from={domain}"]
    lines = [f"Authentication-Results: {value}" for value in (auth or [])]
    lines += [f"From: {sender}", f"To: {MAILBOX}", f"Subject: {subject}", f"Date: {date_text}",
              f"Message-ID: {message_id}", f"Content-Type: {content_type}"]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if encoding:
        lines.append(f"Content-Transfer-Encoding: {encoding}")
    header = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    text = body if isinstance(body, bytes) else body.encode("utf-8")
    return [(b"1 (X-GM-THRID " + thread.encode() + b" BODY[HEADER.FIELDS (FROM TO SUBJECT)] {" +
             str(len(header)).encode() + b"}", header),
            (b" BODY[TEXT]<0> {" + str(len(text)).encode() + b"}", text[:2000]), b")"]


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL: the class is called with the host, then used."""

    def __init__(self, messages, login_error=None):
        self.messages, self.login_error = messages, login_error
        self.calls, self.closed = [], False

    def __call__(self, host, timeout=None):
        self.calls.append(("connect", host))
        return self

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.login_error:
            raise self.login_error

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [b"7"] if code == "UIDVALIDITY" else [None]

    def uid(self, command, *args):
        """UID SEARCH and UID FETCH: a message's UID is its place in the mailbox, from 1."""
        if command.upper() == "SEARCH":
            self.calls.append(("search", args))
            return "OK", [" ".join(str(n + 1) for n in range(len(self.messages))).encode()]
        uid, items = args
        self.calls.append(("fetch", items))
        self.fetched = getattr(self, "fetched", []) + [int(uid)]
        return "OK", self.messages[int(uid) - 1]

    def logout(self):
        self.closed = True


class FakeSMTP:
    """Stands in for smtplib.SMTP_SSL."""
    sent = []
    fail = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.user, self.password = user, password
        if FakeSMTP.fail:
            raise FakeSMTP.fail

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.fixture
def smtp():
    FakeSMTP.sent, FakeSMTP.fail = [], None
    with patch.object(mail.smtplib, "SMTP_SSL", FakeSMTP):
        yield FakeSMTP
    FakeSMTP.sent, FakeSMTP.fail = [], None


def _people(db):
    """A contacted lead with a sent outreach email and an open follow-up, and a client's contact."""
    lead = models.Lead(name="Mercy Banda", company="Sunrise Pharmacy", email="mercy@sunrise.example",
                       status="contacted")
    wamu = models.Client(name="Wamu's Bakery", status="active")
    db.add_all([lead, wamu])
    db.commit()
    contact = models.Contact(client_id=wamu.id, name="Wamu Phiri", email="Wamu@Bakery.Example", is_primary=True)
    sent = models.OutreachMessage(lead_id=lead.id, channel="email", direction="out", subject="Stock alerts",
                                  body="Dear Mercy,", status="sent", external_id=OUTREACH_ID)
    follow_up = models.Todo(text="Follow up with Mercy Banda", done=False, category="outreach", lead_id=lead.id,
                            priority=3)
    db.add_all([contact, sent, follow_up])
    db.commit()
    return lead.id, wamu.id, contact.id, sent.id, follow_up.id


def _inbox():
    long_body = ("Hello Bandile, yes we would like the stock alerts. " * 30 +
                 "\r\n> Dear Mercy,\r\n> an earlier line\r\nOn Wed, 11 Nov 2026 at 10:00, Bandile wrote:\r\nold thread")
    return [
        _raw("Mercy Banda <Mercy@Sunrise.example>", "Re: Stock alerts", long_body,
             message_id="<reply-1@sunrise.example>", in_reply_to=OUTREACH_ID),
        _raw("wamu@bakery.example", "Invoice question", "Can you resend the October invoice?",
             message_id="<q-2@bakery.example>", thread="1701"),
        _raw("Newsletter <news@shop.example>", "50% off everything", "Buy now. " * 50,
             message_id="<spam-3@shop.example>", thread="1702"),
    ]


# ── 5. Fetch ─────────────────────────────────────────────────

def test_fetch_keeps_mail_from_known_people_and_nothing_else(mailbox):
    db = _db()
    lead_id, client_id, contact_id, outreach_id, todo_id = _people(db)
    imap = FakeIMAP(_inbox())
    with patch.object(mail.imaplib, "IMAP4_SSL", imap):
        counts = mail.fetch(db, since=date(2026, 11, 9))
    assert counts == {"ok": True, "fetched": 3, "stored": 2, "matched_leads": 1, "matched_clients": 1,
                      "ignored": 1, "error": None, "backlog": 0, "unverified": 0}
    assert ("connect", "imap.gmail.com") in imap.calls and ("login", MAILBOX, APP_PASSWORD) in imap.calls
    assert ("select", "INBOX", True) in imap.calls                          # read-only: nothing is marked read
    assert ("search", ("SINCE", "09-Nov-2026")) in imap.calls and imap.closed
    items = next(c[1] for c in imap.calls if c[0] == "fetch")
    assert "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO" in items
    assert "BODY.PEEK[TEXT]<0.2000>" in items and "X-GM-THRID" in items and "AUTHENTICATION-RESULTS" in items

    stored = {m.message_id: m for m in db.query(models.EmailMessage).all()}
    assert set(stored) == {"<reply-1@sunrise.example>", "<q-2@bakery.example>"}       # the newsletter is not kept
    reply = stored["<reply-1@sunrise.example>"]
    assert reply.direction == "in" and reply.from_addr == "mercy@sunrise.example" and reply.lead_id == lead_id
    assert reply.outreach_message_id == outreach_id and reply.thread_key == "1700"
    assert reply.received_at == datetime(2026, 11, 12, 7, 30)                          # UTC
    assert reply.snippet.startswith("Hello Bandile, yes we would like the stock alerts.")
    assert len(reply.snippet) <= 500 and "Dear Mercy" not in reply.snippet and "old thread" not in reply.snippet
    question = stored["<q-2@bakery.example>"]
    assert question.client_id == client_id and question.contact_id == contact_id and question.lead_id is None
    assert question.snippet == "Can you resend the October invoice?"

    # The lead replied: status, follow-up, the outreach message, the event.
    db.expire_all()
    assert db.query(models.Lead).filter_by(id=lead_id).one().status == "replied"
    assert db.query(models.Todo).filter_by(id=todo_id).one().done is True
    assert db.query(models.OutreachMessage).filter_by(id=outreach_id).one().replied_at == datetime(2026, 11, 12, 7, 30)
    events = db.query(models.AgentEvent).filter_by(kind="lead.replied").all()
    assert len(events) == 1 and json.loads(events[0].payload_json) == {"lead_id": lead_id, "email_message_id": reply.id}

    comms = db.query(models.Communication).order_by(models.Communication.id).all()
    assert [(c.channel, c.direction, c.source) for c in comms] == [("email", "in", "email_sync")] * 2
    assert comms[0].lead_id == lead_id and comms[0].summary.startswith("Re: Stock alerts: Hello Bandile")
    assert len(comms[0].summary) <= len("Re: Stock alerts: ") + 140
    assert comms[1].client_id == client_id and comms[1].contact_id == contact_id
    assert comms[1].summary == "Invoice question: Can you resend the October invoice?"
    assert "Buy now" not in json.dumps([m.snippet for m in stored.values()])

    item = {i["provider"]: i for i in client.get("/integrations").json()}["email"]
    assert item["status"] == "connected" and item["account_label"] == MAILBOX and item["last_sync_at"]
    assert APP_PASSWORD not in client.get("/integrations").text

    # A second run stores nothing new and announces nothing twice.
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP(_inbox())):
        again = mail.fetch(db, since=date(2026, 11, 9))
    assert again["stored"] == 0 and again["fetched"] == 3 and again["ignored"] == 1
    assert db.query(models.EmailMessage).count() == 2 and db.query(models.Communication).count() == 2
    assert db.query(models.AgentEvent).filter_by(kind="lead.replied").count() == 1
    db.close()


def _clock_at(monkeypatch, moment):
    import user_clock
    monkeypatch.setattr(user_clock, "utcnow", lambda: moment)


def test_fetch_looks_back_to_the_last_successful_sync_not_a_fixed_three_days(mailbox, monkeypatch):
    """Review item 10: after more than three days without a sync (the free instance asleep,
    or the mail server refusing), the mail in between was never looked at."""
    db = _db()
    _people(db)

    def since_of(imap):
        return next(c[1] for c in imap.calls if c[0] == "search")

    _clock_at(monkeypatch, datetime(2026, 11, 12, 8, 0))
    first = FakeIMAP([])
    with patch.object(mail.imaplib, "IMAP4_SSL", first):
        assert mail.fetch(db)["ok"] is True
    assert since_of(first) == ("SINCE", "09-Nov-2026")                      # the first sync: three days

    _clock_at(monkeypatch, datetime(2026, 11, 21, 8, 0))                    # nine days with no sync
    later = FakeIMAP([])
    with patch.object(mail.imaplib, "IMAP4_SSL", later):
        assert mail.fetch(db)["ok"] is True
    assert since_of(later) == ("SINCE", "11-Nov-2026")                      # the last success, less a day

    _clock_at(monkeypatch, datetime(2026, 11, 25, 8, 0))                    # a failed sync moves nothing
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP([], login_error=mail.imaplib.IMAP4.error("down"))):
        assert mail.fetch(db)["ok"] is False
    _clock_at(monkeypatch, datetime(2026, 11, 26, 8, 0))
    after = FakeIMAP([])
    with patch.object(mail.imaplib, "IMAP4_SSL", after):
        assert mail.fetch(db)["ok"] is True
    assert since_of(after) == ("SINCE", "20-Nov-2026")
    db.close()


def test_a_backlog_is_read_oldest_first_and_nothing_is_skipped(mailbox, monkeypatch):
    """A long gap can hold more mail than one sync reads (200). The old code read the newest
    200 and never the rest. Now each sync continues where the last one stopped."""
    db = _db()
    _people(db)
    _clock_at(monkeypatch, datetime(2026, 11, 12, 8, 0))
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP([])):
        mail.fetch(db)

    news = [_raw("Newsletter <news@shop.example>", f"Offer {n}", "Buy now.", message_id=f"<n-{n}@shop.example>")
            for n in range(449)]
    reply = _raw("mercy@sunrise.example", "Re: Stock alerts", "Yes please.", message_id="<old-reply@sunrise.example>",
                 in_reply_to=OUTREACH_ID, date_text="Sat, 14 Nov 2026 09:30:00 +0200")
    inbox = [reply] + news                                                   # the reply is the OLDEST message
    _clock_at(monkeypatch, datetime(2026, 11, 22, 8, 0))
    runs, windows = [], []
    for _ in range(4):
        imap = FakeIMAP(inbox)
        with patch.object(mail.imaplib, "IMAP4_SSL", imap):
            counts = mail.fetch(db)
        runs.append((counts["fetched"], counts["backlog"], getattr(imap, "fetched", [])[:1]))
        windows.append(next(c[1] for c in imap.calls if c[0] == "search")[1])
    assert runs == [(200, 250, [1]), (200, 50, [201]), (50, 0, [401]), (0, 0, [])]
    # The window stays put while mail is waiting, then moves up to the last sync again.
    assert windows == ["11-Nov-2026", "11-Nov-2026", "11-Nov-2026", "21-Nov-2026"]
    assert db.query(models.EmailMessage).filter_by(message_id="<old-reply@sunrise.example>").count() == 1
    db.close()


def _reply_with(auth, message_id="<spoof-1@sunrise.example>"):
    return _raw("Mercy Banda <mercy@sunrise.example>", "Re: Stock alerts", "Yes, send me the invoice details.",
                message_id=message_id, in_reply_to=OUTREACH_ID, auth=auth)


@pytest.mark.parametrize("auth", [
    None,                                                                       # no header at all
    ["mx.google.com; dkim=fail header.i=@sunrise.example; spf=fail smtp.mailfrom=x@evil.example; "
     "dmarc=fail (p=NONE) header.from=sunrise.example"],                        # the checks failed
    ["mx.google.com; dkim=pass header.i=@evil.example; spf=pass smtp.mailfrom=x@evil.example"],  # passes for another domain
    ["mx.google.com; spf=softfail smtp.mailfrom=x@evil.example; dmarc=fail header.from=sunrise.example",
     "mx.google.com; dkim=pass header.i=@sunrise.example; dmarc=pass header.from=sunrise.example"],  # a forged header underneath
    ["mail.evil.example; dkim=pass header.i=@sunrise.example; dmarc=pass header.from=sunrise.example"],  # not our server's header
])
def test_an_unverified_sender_changes_nothing_and_becomes_a_suggestion(mailbox, auth):
    """Review item 11: the From line is all that tied a message to a lead, and anyone can
    write a From line. Only a message whose authentication results pass may move a lead."""
    db = _db()
    lead_id, _client_id, _contact_id, outreach_id, todo_id = _people(db)
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP([_reply_with(auth)])):
        counts = mail.fetch(db, since=date(2026, 11, 9))
    assert (counts["stored"], counts["unverified"], counts["matched_leads"]) == (1, 1, 1)

    db.expire_all()
    stored = db.query(models.EmailMessage).one()                               # it is kept, marked unverified
    assert stored.lead_id == lead_id and stored.auth_result in ("fail", "none")
    assert db.query(models.Lead).filter_by(id=lead_id).one().status == "contacted"
    assert db.query(models.Todo).filter_by(id=todo_id).one().done is False
    assert db.query(models.OutreachMessage).filter_by(id=outreach_id).one().replied_at is None
    assert db.query(models.AgentEvent).filter_by(kind="lead.replied").count() == 0
    assert db.query(models.Communication).count() == 0 and db.query(models.ChangeLog).count() == 0

    suggestion = db.query(models.Suggestion).one()
    payload = json.loads(suggestion.payload_json)
    assert suggestion.kind == "lead_reply" and suggestion.status == "pending"
    assert payload["lead_id"] == lead_id and payload["email_message_id"] == stored.id
    assert "Mercy Banda" in payload["text"] and "verified" in suggestion.reason

    # The same message again files nothing twice.
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP([_reply_with(auth)])):
        mail.fetch(db, since=date(2026, 11, 9))
    assert db.query(models.Suggestion).count() == 1 and db.query(models.EmailMessage).count() == 1

    # One tap accepts it: now it is Bandile's word that she replied.
    accepted = client.post(f"/suggestions/{suggestion.id}/accept").json()
    assert accepted["applied"] is True and accepted["changes"]
    db.expire_all()
    assert db.query(models.Lead).filter_by(id=lead_id).one().status == "replied"
    assert db.query(models.Todo).filter_by(id=todo_id).one().done is True
    assert db.query(models.OutreachMessage).filter_by(id=outreach_id).one().replied_at is not None
    assert db.query(models.EmailMessage).one().auth_result == "confirmed"
    db.close()


@pytest.mark.parametrize("auth", [
    ["mx.google.com; dkim=pass header.i=@sunrise.example; spf=pass smtp.mailfrom=b@sunrise.example; "
     "dmarc=pass (p=REJECT) header.from=sunrise.example"],
    ["mx.google.com; dkim=pass header.i=@mail.sunrise.example header.s=k1"],     # no DMARC record: aligned DKIM
    ["mx.google.com; spf=pass (google.com: domain of mercy@sunrise.example designates 1.2.3.4) "
     "smtp.mailfrom=mercy@sunrise.example"],                                     # aligned SPF
])
def test_a_verified_reply_still_moves_the_lead(mailbox, auth):
    db = _db()
    lead_id, *_ = _people(db)
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP([_reply_with(auth, "<real-1@sunrise.example>")])):
        counts = mail.fetch(db, since=date(2026, 11, 9))
    assert counts["unverified"] == 0
    db.expire_all()
    assert db.query(models.Lead).filter_by(id=lead_id).one().status == "replied"
    assert db.query(models.EmailMessage).one().auth_result == "pass"
    assert db.query(models.Suggestion).count() == 0
    db.close()


def test_the_trusted_authentication_server_is_configurable(mailbox, monkeypatch):
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.fastmail.example")
    db = _db()
    lead_id, *_ = _people(db)
    header = ["mx.fastmail.example; dkim=pass header.i=@sunrise.example; dmarc=pass header.from=sunrise.example"]
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP([_reply_with(header, "<fm-1@sunrise.example>")])):
        assert mail.fetch(db, since=date(2026, 11, 9))["unverified"] == 1       # nobody is trusted until it is named
    monkeypatch.setenv("EMAIL_AUTHSERV_ID", "mx.fastmail.example")
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP([_reply_with(header, "<fm-2@sunrise.example>")])):
        assert mail.fetch(db, since=date(2026, 11, 9))["unverified"] == 0
    db.expire_all()
    assert db.query(models.Lead).filter_by(id=lead_id).one().status == "replied"
    db.close()


def test_fetch_decodes_html_multipart_and_base64(mailbox):
    db = _db()
    _people(db)
    html = "<html><style>p{color:red}</style><body><p>Thanks, <b>Tuesday</b> works.</p></body></html>"
    multipart = ("--BOUND\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSee you on Tuesday.\r\n"
                 "--BOUND\r\nContent-Type: text/html\r\n\r\n<p>See you</p>\r\n--BOUND--\r\n")
    encoded = base64.b64encode("Zikomo kwambiri — sent from my phone".encode("utf-8"))
    inbox = [
        _raw("mercy@sunrise.example", "Re: demo", html, message_id="<h-1@x>", content_type="text/html; charset=utf-8"),
        _raw("mercy@sunrise.example", "Re: demo 2", multipart, message_id="<m-2@x>",
             content_type='multipart/alternative; boundary="BOUND"'),
        _raw("mercy@sunrise.example", "=?utf-8?B?" + base64.b64encode("Zikomo".encode()).decode() + "?=", encoded,
             message_id="<b-3@x>", encoding="base64"),
        _raw("mercy@sunrise.example", "No id at all", "Plain.", message_id=""),
    ]
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP(inbox)):
        counts = mail.fetch(db)
    assert counts["stored"] == 4
    by = {m.subject: m for m in db.query(models.EmailMessage).all()}
    assert by["Re: demo"].snippet == "Thanks, Tuesday works."
    assert by["Re: demo 2"].snippet == "See you on Tuesday."
    assert by["Zikomo"].snippet == "Zikomo kwambiri — sent from my phone"
    assert by["No id at all"].message_id.startswith("<no-id-")
    db.close()


def test_a_failed_sign_in_is_recorded_without_the_password(mailbox, capsys):
    db = _db()
    refused = imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] Invalid credentials for {MAILBOX} / {APP_PASSWORD}")
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP([], login_error=refused)):
        counts = mail.fetch(db)
    assert counts["ok"] is False and APP_PASSWORD not in counts["error"] and "AUTHENTICATIONFAILED" in counts["error"]
    row = integrations.get_or_create(db, "email")
    assert row.status == "error" and APP_PASSWORD not in row.last_error and "***" in row.last_error
    assert APP_PASSWORD not in capsys.readouterr().out
    item = {i["provider"]: i for i in client.get("/integrations").json()}["email"]
    assert item["status"] == "error" and APP_PASSWORD not in item["last_error"]
    with patch.object(mail.imaplib, "IMAP4_SSL", side_effect=OSError("network unreachable")):
        assert mail.fetch(db)["ok"] is False                                  # a socket error does not raise either
    db.close()


def test_email_sync_agent_stays_inside_its_tables(mailbox, monkeypatch):
    db = _db()
    spec = email_sync.SPEC
    assert spec.schedule.interval_minutes == 30 and spec.permissions.max_llm_calls == 0
    assert spec.permissions.tables_write == ["email_messages", "communications", "leads", "todos", "integrations",
                                             "suggestions"]
    assert spec.permissions.may_emit == ["lead.replied"]
    _people(db)
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP(_inbox())):
        out = runner.run_agent(spec, "schedule")
    assert out["status"] == "completed" and out["changes_count"] >= 3        # two communications, the lead, the task
    assert out["summary"] == "2 new message(s): 1 from leads, 1 from clients"
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP(_inbox())):
        assert runner.run_agent(spec, "schedule")["status"] == "skipped"
    monkeypatch.delenv("EMAIL_APP_PASSWORD")
    assert runner.run_agent(spec, "schedule")["summary"] == "email not configured"
    db.close()


# ── 6. Send ──────────────────────────────────────────────────

def test_send_goes_through_smtp_and_stores_an_out_row(mailbox, smtp):
    db = _db()
    lead_id, client_id, *_ = _people(db)
    out = mail.send(db, to="Mercy Banda <mercy@sunrise.example>", subject="Stock\r\nalerts", body="Dear Mercy,\n\nHello.",
                    lead_id=lead_id, outreach_message_id=7)
    db.commit()
    assert out["ok"] is True and out["error"] is None and out["message_id"].endswith("@example.com>")
    assert (smtp.last.host, smtp.last.port, smtp.last.timeout) == ("smtp.gmail.com", 465, 20)
    assert (smtp.last.user, smtp.last.password) == (MAILBOX, APP_PASSWORD)
    sent = smtp.sent[0]
    assert sent["From"] == f"Bandile Masocha <{MAILBOX}>" and sent["To"] == "mercy@sunrise.example"
    assert sent["Subject"] == "Stock alerts" and sent["Message-ID"] == out["message_id"]    # no header injection
    assert sent.get_content_type() == "text/plain" and "Dear Mercy," in sent.get_content()
    row = db.query(models.EmailMessage).one()
    assert row.direction == "out" and row.message_id == out["message_id"] and row.to_addr == "mercy@sunrise.example"
    assert row.lead_id == lead_id and row.outreach_message_id == 7 and row.snippet == "Dear Mercy, Hello."

    assert mail.send(db, to="not an address", subject="x", body="y")["ok"] is False
    assert mail.send(db, to="a@b.example", subject="x", body="  ")["ok"] is False
    smtp.fail = OSError(f"connection reset while sending {APP_PASSWORD}")
    failed = mail.send(db, to="a@b.example", subject="x", body="y")
    assert failed["ok"] is False and APP_PASSWORD not in failed["error"]
    assert len(smtp.sent) == 1 and db.query(models.EmailMessage).count() == 1
    db.close()


def test_send_says_so_when_email_is_not_configured(smtp, monkeypatch):
    monkeypatch.delenv("EMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("EMAIL_APP_PASSWORD", raising=False)
    db = _db()
    assert mail.send(db, to="a@b.example", subject="x", body="y") == {
        "ok": False, "message_id": None, "error": "email sending is not configured"}
    assert smtp.sent == [] and mail.fetch(db)["error"] == "Email is not configured"
    assert client.post("/integrations/email/sync").status_code == 503
    assert client.post("/integrations/email/test").status_code == 503
    db.close()


def test_approved_outreach_email_now_sends(mailbox, smtp):
    import outreach
    db = _db()
    lead = models.Lead(name="Mercy Banda", company="Sunrise Pharmacy", email="mercy@sunrise.example", status="qualified")
    db.add(lead)
    db.commit()
    lead_id = lead.id
    with patch("llm.chat_text", return_value=json.dumps({"subject": "Stock alerts", "body": "Dear Mercy,"})):
        mid = outreach.draft(db, lead, "email", "intro")["id"]
    db.commit()
    db.close()

    out = client.post(f"/outreach/{mid}/send").json()
    assert out["message"]["status"] == "sent" and out["message"]["error"] is None
    assert out["message"]["external_id"] == smtp.sent[0]["Message-ID"]
    assert smtp.sent[0]["To"] == "mercy@sunrise.example" and smtp.sent[0]["Subject"] == "Stock alerts"
    db = _db()
    assert db.query(models.Lead).filter_by(id=lead_id).one().status == "contacted"
    stored = db.query(models.EmailMessage).one()
    assert stored.direction == "out" and stored.outreach_message_id == mid and stored.lead_id == lead_id
    assert db.query(models.Todo).filter_by(lead_id=lead_id, category="outreach").count() == 1   # the follow-up
    db.close()


def test_email_endpoints_and_lookup_tool(mailbox, smtp):
    db = _db()
    lead_id, client_id, *_ = _people(db)
    off, summary = ot.run_tool("email_lookup", {"who": "Mercy"}, db)
    assert off["messages"] == [] and summary == "no stored email for Mercy Banda"
    with patch.object(mail.imaplib, "IMAP4_SSL", FakeIMAP(_inbox())):
        synced = client.post("/integrations/email/sync").json()
    assert synced["stored"] == 2
    assert [m["subject"] for m in client.get("/email/messages", params={"lead_id": lead_id}).json()] == ["Re: Stock alerts"]
    assert [m["subject"] for m in client.get("/email/messages", params={"client_id": client_id}).json()] == ["Invoice question"]
    listed = client.get("/email/messages").json()
    assert len(listed) == 2 and all("body" not in m and len(m["snippet"]) <= 500 for m in listed)

    found, summary = ot.run_tool("email_lookup", {"who": "Wamu"}, db)
    assert summary == "1 email(s) with Wamu's Bakery"
    assert found["messages"] == [{"direction": "from them", "subject": "Invoice question",
                                  "snippet": "Can you resend the October invoice?", "when": "2026-11-12"}]
    nobody, _ = ot.run_tool("email_lookup", {"who": "Zacharias Quillfeather"}, db)
    assert "error" in nobody

    tested = client.post("/integrations/email/test").json()
    assert tested == {"ok": True, "error": None} and smtp.sent[-1]["To"] == MAILBOX
    assert db.query(models.EmailMessage).filter_by(direction="out").count() == 0       # the test message is not kept
    db.close()


def test_email_lookup_says_when_email_is_not_connected():
    db = _db()
    out, summary = ot.run_tool("email_lookup", {"who": "Mercy"}, db)
    assert out == {"connected": False, "message": "Email is not connected"} and summary == "Email is not connected"
    db.close()


# ── YouTube's row reports the Brand account's own state ──────

def test_youtube_row_shows_the_brand_accounts_error(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSy-youtube-key")
    db = _db()
    assert {i["provider"]: i for i in client.get("/integrations").json()}["youtube"]["status"] == "pending"
    db.add(models.SocialAccount(platform="youtube", handle="@bandile", external_id="UCx", access="api_key",
                                status="error", last_error="quota exceeded for key AIzaSy-youtube-key"))
    db.commit()
    item = {i["provider"]: i for i in client.get("/integrations").json()}["youtube"]
    assert item["status"] == "error" and item["account_label"] == "@bandile" and item["actions"] == []
    assert item["last_error"] == "quota exceeded for key ***"
    db.close()
