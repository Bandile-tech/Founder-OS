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

def test_pair_code_is_six_base32_characters_and_stable(monkeypatch):
    monkeypatch.setenv("FOUNDER_OS_SECRET", "server-secret-one")
    code = tg.pair_code()
    assert len(code) == 6 and code == code.upper()
    assert set(code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert tg.pair_code() == code                                  # nothing is stored: same every time
    monkeypatch.setenv("FOUNDER_OS_SECRET", "server-secret-two")
    assert tg.pair_code() != code                                  # tied to the server's secret


def test_pair_code_follows_the_password_when_no_secret_is_set(monkeypatch):
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "password-one")
    first = tg.pair_code()
    monkeypatch.setenv("FOUNDER_OS_PASSWORD", "password-two")
    assert tg.pair_code() != first


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
