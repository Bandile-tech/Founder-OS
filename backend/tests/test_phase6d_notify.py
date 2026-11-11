"""
Phase 6D.1 — notifications: queue, dispatch, settings, Telegram channel, couriers.

  1. queue dedupes by key; dispatch sends once; a second dispatch sends nothing
  2. quiet hours 22:00–05:00: an alert at 23:00 is held until 05:00; a reply goes at once; force sends
  3. daily cap: the third alert is suppressed, and the next morning's text lists it under OVERNIGHT
  6. render_briefing: the documented sections, at most 3,500 characters, empty sections left out
  7. briefing_courier at 06:01 sends once and not again at 06:30; no evening message unless set
  8. alert_courier: one message per rule per day, the severity floor, a payment.overdue event
 10. set_reminder("call the clinic", "17:00") queues 17:00 local; reminder_courier sends it when due
 11. a 9,000-character body becomes three Telegram sends
     plus: not paired and channel off, retries then failed, redacted errors, mute, parse_when,
     the settings endpoints and tool, delivery in the briefing, the migration

(Tests 4, 5 and 9 — the inbound webhook — arrive with 6D.2.) No network: httpx.post is patched.
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agents
import business_common
import change_log
import integrations_telegram as tg
import llm
import models
import notify
import orchestrator_tools as ot
from agents import alert_courier, briefing_courier, reminder_courier, runner, schedule
from tests.conftest import TestSessionLocal, client

BOT_TOKEN = "123456789:AAE-test_token-value_0123456789abcdef"
CHAT = 4455667788
# 12 November 2026 is a Thursday. Lusaka is UTC+2 all year.
NOON = datetime(2026, 11, 12, 10, 0)             # 12:00 local


def utc(hour, minute=0, day=12):
    """A local clock time on a November 2026 day, as naive UTC."""
    return datetime(2026, 11, day, hour, minute) - timedelta(hours=2)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("OPENAI_API_KEY", "TELEGRAM_BOT_USERNAME", "TELEGRAM_WEBHOOK_SECRET", "FOUNDER_OS_API_URL",
                 "FOUNDER_OS_SECRET", "FOUNDER_OS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("FOUNDER_OS_TZ", "Africa/Lusaka")
    llm.reset_client()
    llm.clear_run_context()
    with patch.object(runner, "SessionLocal", TestSessionLocal):
        yield
    llm.clear_run_context()


@pytest.fixture
def sends():
    """Every Telegram call made, with a successful answer for each."""
    calls = []

    def post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 1000 + len(calls)}})

    with patch.object(notify.httpx, "post", side_effect=post):
        yield calls


def _db():
    return TestSessionLocal()


def _pair(db):
    tg.pair(db, CHAT, "bandile")
    db.commit()


def _queue(db, **kw):
    out = notify.queue(db, **kw)
    db.commit()
    return out


def _row(db, notification_id):
    db.expire_all()
    return db.query(models.Notification).filter(models.Notification.id == notification_id).first()


# ── 1. Queue and dispatch ────────────────────────────────────

def test_queue_dedupes_and_dispatch_sends_once(sends):
    db = _db()
    _pair(db)
    first = _queue(db, kind="alert", title="CRITICAL", body="No revenue logged in 30 days.",
                   dedupe_key="alert:no_revenue_30d:2026-11-12")
    again = _queue(db, kind="alert", title="CRITICAL", body="No revenue logged in 30 days.",
                   dedupe_key="alert:no_revenue_30d:2026-11-12")
    assert first["deduped"] is False and again["deduped"] is True and again["id"] == first["id"]
    assert db.query(models.Notification).count() == 1
    assert sends == []                                           # queue never sends

    out = notify.dispatch(db, NOON)
    assert out["sent"] == 1 and len(sends) == 1
    call = sends[0]
    assert call["url"] == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    assert call["json"] == {"chat_id": CHAT, "text": "CRITICAL\nNo revenue logged in 30 days.",
                            "disable_web_page_preview": True}
    assert call["timeout"] == 10
    row = _row(db, first["id"])
    assert row.status == "sent" and row.sent_at == NOON and row.external_id == "1001"

    assert notify.dispatch(db, NOON + timedelta(minutes=15)) == {"sent": 0, "held": 0, "suppressed": 0,
                                                                 "failed": 0, "retrying": 0}
    assert len(sends) == 1
    db.close()


def test_queue_refuses_an_unknown_kind_or_an_empty_body():
    db = _db()
    assert "error" in notify.queue(db, kind="marketing", body="hello")
    assert "error" in notify.queue(db, kind="alert", body="   ")
    assert db.query(models.Notification).count() == 0
    db.close()


def test_a_future_send_at_waits(sends):
    db = _db()
    _pair(db)
    queued = _queue(db, kind="reminder", title="Reminder", body="call the clinic", send_at=utc(17))
    assert notify.dispatch(db, utc(16, 59))["sent"] == 0 and sends == []
    assert notify.dispatch(db, utc(17))["sent"] == 1
    assert sends[0]["json"]["text"] == "Reminder\ncall the clinic" and _row(db, queued["id"]).status == "sent"
    db.close()


# ── 2. Quiet hours ───────────────────────────────────────────

def test_quiet_hours_hold_an_alert_until_five_but_not_a_reply(sends):
    db = _db()
    _pair(db)
    alert = _queue(db, kind="alert", title="Warning", body="Discipline is drifting.", dedupe_key="alert:x:1")
    reply = _queue(db, kind="reply", body="Logged.")
    out = notify.dispatch(db, utc(23))                           # 23:00 local
    assert out["held"] == 1 and out["sent"] == 1
    assert [c["json"]["text"] for c in sends] == ["Logged."]
    held = _row(db, alert["id"])
    assert held.status == "queued" and held.send_at == utc(5, day=13)      # 05:00 the next morning
    assert _row(db, reply["id"]).status == "sent"

    assert notify.dispatch(db, utc(4, 59, day=13))["sent"] == 0  # still quiet
    assert notify.dispatch(db, utc(5, day=13))["sent"] == 1
    assert _row(db, alert["id"]).status == "sent"
    db.close()


def test_force_sends_through_quiet_hours(sends):
    db = _db()
    _pair(db)
    alert = _queue(db, kind="alert", body="Payment overdue.")
    assert notify.dispatch(db, utc(2), force=True)["sent"] == 1
    assert _row(db, alert["id"]).status == "sent"
    db.close()


def test_is_quiet_handles_ranges_across_midnight_and_within_a_day():
    s = SimpleNamespace(quiet_start="22:00", quiet_end="05:00", muted_until=None)
    local = lambda h, m=0: schedule.to_local(utc(h, m))
    assert notify.is_quiet(local(22), s) and notify.is_quiet(local(23, 59), s) and notify.is_quiet(local(4, 59), s)
    assert not notify.is_quiet(local(5), s) and not notify.is_quiet(local(21, 59), s) and not notify.is_quiet(local(12), s)
    day = SimpleNamespace(quiet_start="13:00", quiet_end="14:00", muted_until=None)
    assert notify.is_quiet(local(13, 30), day) and not notify.is_quiet(local(14), day) and not notify.is_quiet(local(2), day)
    off = SimpleNamespace(quiet_start="00:00", quiet_end="00:00", muted_until=None)
    assert not notify.is_quiet(local(3), off)


def test_mute_holds_messages_until_it_ends(sends):
    db = _db()
    _pair(db)
    s = notify.settings(db)
    s.muted_until = utc(14)
    db.commit()
    alert = _queue(db, kind="alert", body="Payment overdue.")
    assert notify.dispatch(db, utc(12))["held"] == 1
    assert _row(db, alert["id"]).send_at == utc(14)
    assert notify.dispatch(db, utc(14))["sent"] == 1
    db.close()


def test_a_reminder_is_sent_at_the_time_asked_even_in_quiet_hours(sends):
    db = _db()
    _pair(db)
    _queue(db, kind="reminder", title="Reminder", body="take the tablets", send_at=utc(23, 30))
    assert notify.dispatch(db, utc(23, 30))["sent"] == 1
    db.close()


# ── 3. Daily cap ─────────────────────────────────────────────

def test_daily_cap_suppresses_the_third_alert_and_the_briefing_lists_it(sends):
    db = _db()
    _pair(db)
    notify.settings(db).daily_cap = 2
    db.commit()
    ids = [_queue(db, kind="alert", title="Warning", body=f"Alert number {n} needs a look.",
                  dedupe_key=f"alert:rule{n}:2026-11-12")["id"] for n in (1, 2, 3)]
    out = notify.dispatch(db, NOON)
    assert out["sent"] == 2 and out["suppressed"] == 1 and len(sends) == 2
    third = _row(db, ids[2])
    assert third.status == "suppressed" and third.suppressed_reason == "daily_cap"
    assert notify.sent_today(db, date(2026, 11, 12)) == 2

    # A briefing and a reply still go out past the cap.
    _queue(db, kind="briefing", body="Good morning.")
    _queue(db, kind="reply", body="Done.")
    assert notify.dispatch(db, NOON + timedelta(minutes=5))["sent"] == 2

    # The next day starts a new count, and the suppressed alert is in the morning text.
    assert notify.sent_today(db, date(2026, 11, 13)) == 0
    overnight = notify.suppressed_overnight(db, datetime.utcnow())
    assert [n["id"] for n in overnight] == [ids[2]]
    text = notify.render_briefing({"date": "2026-11-13", "greeting": "Good morning", "badge": "EXECUTION DAY"},
                                  suppressed_overnight=overnight)
    assert "OVERNIGHT (suppressed): • Alert number 3 needs a look." in text
    db.close()


# ── Not paired, channel off, failures ────────────────────────

def test_not_paired_is_suppressed(sends):
    db = _db()
    queued = _queue(db, kind="alert", body="Payment overdue.")
    assert notify.dispatch(db, NOON)["suppressed"] == 1 and sends == []
    row = _row(db, queued["id"])
    assert row.status == "suppressed" and row.suppressed_reason == "not_paired"
    assert notify.send_test(db) == {"id": row.id + 1, "status": "suppressed", "error": "Telegram is not paired yet",
                                    "suppressed_reason": "not_paired"}
    db.close()


def test_channel_off_or_no_token_is_suppressed(sends, monkeypatch):
    db = _db()
    _pair(db)
    s = notify.settings(db)
    s.channels_json = json.dumps({"telegram": False})
    db.commit()
    a = _queue(db, kind="alert", body="One.")
    notify.dispatch(db, NOON)
    assert _row(db, a["id"]).suppressed_reason == "channel_disabled"

    s.channels_json = json.dumps({"telegram": True})
    db.commit()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    b = _queue(db, kind="alert", body="Two.")
    notify.dispatch(db, NOON)
    assert _row(db, b["id"]).suppressed_reason == "channel_disabled" and sends == []
    db.close()


def test_switched_off_suppresses_alerts_but_still_answers(sends):
    db = _db()
    _pair(db)
    notify.settings(db).enabled = False
    db.commit()
    alert = _queue(db, kind="alert", body="Payment overdue.")
    reply = _queue(db, kind="reply", body="Logged.")
    out = notify.dispatch(db, NOON)
    assert out["suppressed"] == 1 and out["sent"] == 1
    assert _row(db, alert["id"]).suppressed_reason == "channel_disabled" and _row(db, reply["id"]).status == "sent"
    db.close()


def test_a_failed_send_is_retried_and_fails_after_three_attempts():
    db = _db()
    _pair(db)
    queued = _queue(db, kind="alert", body="Payment overdue.")
    refusal = SimpleNamespace(status_code=400, json=lambda: {"ok": False, "description": "Bad Request: chat not found"})
    with patch.object(notify.httpx, "post", return_value=refusal) as post:
        assert notify.dispatch(db, NOON)["retrying"] == 1
        row = _row(db, queued["id"])
        assert row.status == "queued" and row.error == "attempt 1/3: Bad Request: chat not found"
        assert notify.dispatch(db, NOON + timedelta(minutes=15))["retrying"] == 1
        assert notify.dispatch(db, NOON + timedelta(minutes=30))["failed"] == 1
        row = _row(db, queued["id"])
        assert row.status == "failed" and row.error.startswith("attempt 3/3")
        assert notify.dispatch(db, NOON + timedelta(minutes=45))["failed"] == 0
        assert post.call_count == 3
    db.close()


def test_a_send_that_raises_never_leaks_the_token():
    db = _db()
    _pair(db)
    queued = _queue(db, kind="alert", body="Payment overdue.")
    boom = RuntimeError(f"ConnectError for https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")
    with patch.object(notify.httpx, "post", side_effect=boom):
        out = notify.TelegramChannel.send(CHAT, "hello")
        assert out["ok"] is False and BOT_TOKEN not in out["error"]
        notify.dispatch(db, NOON)
    row = _row(db, queued["id"])
    assert row.status == "queued" and BOT_TOKEN not in row.error and "RuntimeError" in row.error
    db.close()


# ── 11. Splitting ────────────────────────────────────────────

def test_a_nine_thousand_character_body_becomes_three_sends(sends):
    db = _db()
    _pair(db)
    paragraphs = [(f"Paragraph {n}. " + "x" * 998)[:998] for n in range(9)]
    body = "\n\n".join(paragraphs)
    assert 8900 < len(body) <= 9000
    queued = _queue(db, kind="reply", body=body)
    assert notify.dispatch(db, NOON)["sent"] == 1
    assert len(sends) == 3
    assert all(len(c["json"]["text"]) <= 4000 for c in sends)
    assert "\n\n".join(c["json"]["text"] for c in sends) == body          # nothing lost, nothing repeated
    assert _row(db, queued["id"]).external_id == "1001"
    db.close()


def test_split_message_breaks_a_single_long_paragraph():
    parts = notify.split_message("line\n" * 2500)                # 12,500 characters, no blank line
    assert len(parts) >= 4 and all(len(p) <= 4000 for p in parts)
    assert notify.split_message("short") == ["short"] and notify.split_message("") == []
    assert all(len(p) <= 4000 for p in notify.split_message("y" * 9000))


# ── 6. The briefing text ─────────────────────────────────────

def _fixture_briefing():
    return {
        "date": "2026-11-05", "weekday": "Thursday", "badge": "EXECUTION DAY", "greeting": "Good morning",
        "greeting_line": "Three things need you today.",
        "items": [
            {"category": "task", "title": "Email the insurance CEO", "due_label": "2d overdue", "score": 96, "target_type": "todo"},
            {"category": "task", "title": "Business essay plan", "due_label": "Today", "score": 75, "target_type": "todo"},
            {"category": "alert", "title": "No revenue logged in 30 days", "due_label": "CRITICAL", "score": 65},
            {"category": "event", "title": "Further Maths P1 past paper", "due_label": "09:00", "score": 60},
            {"category": "non_negotiable", "title": "Scripture & Prayer", "due_label": "Today", "score": 55},
            {"category": "exam", "title": "Further Maths P1 exam", "due_label": "19 days", "score": 64},
            {"category": "syllabus", "title": "Revise complex numbers", "due_label": "", "score": 50},
            {"category": "deadline", "title": "Far-off thing", "due_label": "Due 19 Nov", "score": 35},
        ],
        "counts": {"non_negotiables_done": 0, "non_negotiables_total": 5},
        "exams": [{"subject": "Further Maths", "subject_code": "9231", "paper": "P1", "days_away": 19},
                  {"subject": "Economics", "subject_code": "9708", "paper": "P2", "days_away": 26}],
    }


def test_render_briefing_matches_the_documented_layout():
    overnight = [{"body": "Payment from Wamu's is 15 days overdue", "title": "Payment overdue"}]
    text = notify.render_briefing(_fixture_briefing(), suppressed_overnight=overnight)
    assert text == (
        "Good morning. Thursday 5 November · EXECUTION DAY\n"
        "Three things need you today.\n"
        "\n"
        "ATTENTION (3)\n"
        "• 2d overdue — Email the insurance CEO\n"
        "• Today — Business essay plan\n"
        "• CRITICAL — No revenue logged in 30 days\n"
        "\n"
        "TODAY\n"
        "• 09:00 Further Maths P1 past paper (calendar)\n"
        "• Non-negotiables: 0/5\n"
        "\n"
        "EXAMS  Further Maths P1 in 19 days · Economics P2 in 26 days\n"
        "\n"
        "OVERNIGHT (suppressed): • Payment from Wamu's is 15 days overdue\n"
        "\n"
        "Reply here to log anything. /brief resend · /undo last change"
    )


def test_render_briefing_omits_empty_sections_and_stays_under_the_limit():
    bare = notify.render_briefing({"date": "2026-11-05", "greeting": "Good morning", "badge": "EXECUTION DAY",
                                   "items": [], "counts": {}, "exams": []}, suppressed_overnight=[])
    assert bare == ("Good morning. Thursday 5 November · EXECUTION DAY\n\n"
                    "Reply here to log anything. /brief resend · /undo last change")
    for heading in ("ATTENTION", "TODAY", "EXAMS", "OVERNIGHT"):
        assert heading not in bare

    big = _fixture_briefing()
    big["greeting_line"] = "word " * 2000
    big["items"] = [{"category": "task", "title": f"Task {n} " + "y" * 400, "due_label": "Today", "score": 90}
                    for n in range(30)] + [{"category": "event", "title": f"Meeting {n}", "due_label": "10:00",
                                            "score": 60} for n in range(12)]
    text = notify.render_briefing(big, suppressed_overnight=[{"body": f"Missed {n} " + "z" * 300} for n in range(40)])
    assert len(text) <= 3500
    assert text.count("\n• ") <= 6 + 5                            # six attention lines, five today lines
    assert "ATTENTION (30)" in text and "(+35 more)" in text
    assert text.split("TODAY\n")[1].split("\n\n")[0].count("•") == 5


# ── 7. Briefing courier ──────────────────────────────────────

def test_briefing_courier_sends_once_after_the_morning_time(sends):
    db = _db()
    _pair(db)
    db.add(models.Todo(text="Email the insurance CEO", done=False, due_date=date(2026, 11, 10), priority=2))
    db.commit()
    spec = briefing_courier.SPEC

    early = runner.run_agent(spec, "schedule", now=utc(5, 59))
    assert early["status"] == "skipped" and sends == []

    first = runner.run_agent(spec, "schedule", now=utc(6, 1))
    assert first["status"] == "completed" and "briefing queued" in first["summary"] and len(sends) == 1
    text = sends[0]["json"]["text"]
    assert text.startswith("Good morning. Thursday 12 November · EXECUTION DAY")
    assert "• 2d overdue — Email the insurance CEO" in text and text.endswith("/undo last change")

    second = runner.run_agent(spec, "schedule", now=utc(6, 30))
    assert second["status"] == "skipped" and len(sends) == 1     # the dedupe key makes it a no-op

    rows = db.query(models.Notification).all()
    assert [(n.kind, n.dedupe_key, n.status, n.source) for n in rows] == [
        ("briefing", "briefing:2026-11-12", "sent", "agent:briefing_courier")]
    assert rows[0].agent_run_id == first["id"]
    # No evening review unless a time is set — even late in the day.
    late = runner.run_agent(spec, "schedule", now=utc(21))
    assert late["status"] == "skipped" and db.query(models.Notification).count() == 1
    assert first["changes_count"] == 0 and first["llm_calls"] == 0
    db.close()


def test_briefing_courier_sends_the_evening_review_when_a_time_is_set(sends):
    db = _db()
    _pair(db)
    db.add(models.Todo(text="Business essay plan", done=False, due_date=date(2026, 11, 12), priority=3))
    s = notify.settings(db)
    s.evening_time = "20:00"
    db.commit()
    spec = briefing_courier.SPEC
    runner.run_agent(spec, "schedule", now=utc(19, 59))
    assert [c["json"]["text"][:12] for c in sends] == ["Good evening"]   # only the (late) briefing so far
    runner.run_agent(spec, "schedule", now=utc(20, 15))
    evening = sends[1]["json"]["text"]
    assert evening.startswith("Evening review. Thursday 12 November")
    assert "STILL OPEN (1)\n• Today — Business essay plan" in evening
    assert evening.endswith("What did you get done? Reply here.")
    runner.run_agent(spec, "schedule", now=utc(20, 45))
    assert len(sends) == 2
    keys = {n.dedupe_key for n in db.query(models.Notification).all()}
    assert keys == {"briefing:2026-11-12", "evening:2026-11-12"}
    db.close()


def test_couriers_do_nothing_until_telegram_is_paired(sends):
    db = _db()
    for spec in (briefing_courier.SPEC, alert_courier.SPEC):
        out = runner.run_agent(spec, "schedule", now=utc(9))
        assert out["status"] == "skipped" and out["summary"] == "Telegram is not paired"
    assert db.query(models.Notification).count() == 0 and sends == []
    _pair(db)
    notify.settings(db).enabled = False
    db.commit()
    assert runner.run_agent(briefing_courier.SPEC, "schedule", now=utc(9, 20))["summary"] == "notifications are off"
    db.close()


# ── 8. Alert courier ─────────────────────────────────────────

ALERTS = [{"rule_id": "no_revenue_30d", "check": "no_revenue_since", "severity": "critical",
           "message": "No revenue logged in the last 30 days."},
          {"rule_id": "habit_drift_7d", "check": "non_negotiable_completion_below", "severity": "warn",
           "message": "Non-negotiable completion has dropped under 60%."},
          {"rule_id": "fyi", "check": "x", "severity": "info", "message": "Just so you know."}]


def test_alert_courier_sends_one_message_per_rule_per_day(sends):
    db = _db()
    _pair(db)
    with patch.object(ot, "detect_off_track", return_value=(ALERTS, "3 alert(s)")):
        first = runner.run_agent(alert_courier.SPEC, "schedule", now=utc(9))
        again = runner.run_agent(alert_courier.SPEC, "schedule", now=utc(9, 15))
        tomorrow = runner.run_agent(alert_courier.SPEC, "schedule", now=utc(9, day=13))
    assert first["status"] == "completed" and again["status"] == "skipped"
    texts = [c["json"]["text"] for c in sends]
    assert texts[:2] == ["CRITICAL\nNo revenue logged in the last 30 days.",
                         "Warning\nNon-negotiable completion has dropped under 60%."]   # info is below the floor
    assert len(sends) == 4 and tomorrow["status"] == "completed"                         # a new day, a new message
    keys = sorted(n.dedupe_key for n in db.query(models.Notification).all())
    assert keys == ["alert:habit_drift_7d:2026-11-12", "alert:habit_drift_7d:2026-11-13",
                    "alert:no_revenue_30d:2026-11-12", "alert:no_revenue_30d:2026-11-13"]
    db.close()


def test_alert_floor_of_critical_leaves_warnings_out(sends):
    db = _db()
    _pair(db)
    notify.settings(db).alert_min_severity = "critical"
    db.commit()
    with patch.object(ot, "detect_off_track", return_value=(ALERTS, "3 alert(s)")):
        runner.run_agent(alert_courier.SPEC, "schedule", now=utc(9))
    assert [c["json"]["text"] for c in sends] == ["CRITICAL\nNo revenue logged in the last 30 days."]
    assert db.query(models.Notification).count() == 1            # the warning was never queued
    db.close()


def test_a_payment_overdue_event_names_the_client_and_the_amount(sends):
    db = _db()
    _pair(db)
    wamu = models.Client(name="Wamu's Bakery", status="active")
    db.add(wamu)
    db.commit()
    payment = models.Payment(client_id=wamu.id, amount=1500, currency="ZMW", status="overdue",
                             due_date=date(2026, 10, 28))
    db.add(payment)
    db.commit()
    event = agents.emit(db, "payment.overdue", {"payment_id": payment.id, "client_id": wamu.id, "amount": 1500,
                                                "currency": "ZMW", "days_overdue": 15})
    db.commit()
    with patch.object(ot, "detect_off_track", return_value=([], "no alerts")):
        out = runner.run_agent(alert_courier.SPEC, "event", [event.id], now=utc(9))
        runner.run_agent(alert_courier.SPEC, "event", [event.id], now=utc(9, 30))        # delivered twice: sent once
    assert out["status"] == "completed"
    assert [c["json"]["text"] for c in sends] == [
        "Payment overdue\nPayment from Wamu's Bakery of K1,500 is 15 days overdue."]
    assert db.query(models.Notification).one().dedupe_key == f"payment:{payment.id}:2026-11-12"
    db.close()


def test_other_events_and_exam_marks_become_deadline_messages(sends):
    db = _db()
    _pair(db)
    lead = models.Lead(name="Mercy Banda", status="replied")
    db.add_all([lead, models.ExamDate(subject_code="9231", paper="P1", exam_date=date(2026, 11, 19)),
                models.ExamDate(subject_code="9708", paper="P2", exam_date=date(2026, 11, 22))])
    db.commit()
    events = [agents.emit(db, "lead.replied", {"lead_id": lead.id}),
              agents.emit(db, "application.deadline_near", {"application_id": 7, "name": "Wharton", "days": 3,
                                                            "mark": 3, "deadline": "2026-11-15", "missing": 2}),
              agents.emit(db, "brand.sync_failed", {"account_id": 4, "platform": "youtube",
                                                    "error": f"403 for key in /bot{BOT_TOKEN}/x"}),
              agents.emit(db, "payment.overdue", {"client_id": 1})]                      # no payment id: ignored
    db.commit()
    with patch.object(ot, "detect_off_track", return_value=([], "no alerts")):
        out = runner.run_agent(alert_courier.SPEC, "event", [e.id for e in events], now=utc(9))
    assert out["status"] == "completed"
    texts = sorted(c["json"]["text"] for c in sends)
    assert len(texts) == 4 and all(BOT_TOKEN not in t for t in texts)
    assert "A lead replied\nMercy Banda replied. Open the pipeline to answer." in texts
    assert "Application deadline\nWharton is due in 3 days (15 Nov). 2 requirements still open." in texts
    assert any(t.startswith("Brand sync failed\nYoutube could not be synced: 403") for t in texts)
    assert any(t.startswith("Exam countdown\n") and "exam in 7 days (19 Nov)." in t for t in texts)   # 10 days is no mark
    keys = {n.dedupe_key for n in db.query(models.Notification).all()}
    assert keys == {f"lead_replied:{lead.id}", "deadline:application:7:3", "brand_sync_failed:4:2026-11-12",
                    "deadline:exam:9231:P1:7"}
    assert {n.kind for n in db.query(models.Notification).all()} == {"alert", "deadline"}
    db.close()


def test_the_couriers_are_registered_last_and_may_notify():
    agents.load_all()
    keys = [s.key for s in agents.ordered_specs()]
    assert keys[-3:] == ["briefing_courier", "alert_courier", "reminder_courier"]
    for key in keys[-3:]:
        spec = agents.REGISTRY[key]
        assert spec.permissions.may_notify and spec.permissions.tables_write == [] and spec.permissions.tools == []
        assert spec.schedule.interval_minutes == 15
    assert alert_courier.SPEC.events == ["payment.overdue", "lead.replied", "application.deadline_near",
                                         "brand.sync_failed"]
    assert alert_courier.SPEC.permissions.max_llm_calls == 0 and reminder_courier.SPEC.permissions.max_llm_calls == 0


def test_an_every_tick_agent_survives_a_timer_that_fires_a_moment_early():
    row = SimpleNamespace(last_run_at=NOON, interval_minutes=None, at_time=None, weekdays_json=None, enabled=True)
    spec = reminder_courier.SPEC
    early = schedule.to_local(NOON + timedelta(minutes=14, seconds=59))
    assert schedule.due(spec, row, early) is True
    assert schedule.due(spec, row, schedule.to_local(NOON + timedelta(minutes=14))) is False


# ── 10. Reminders ────────────────────────────────────────────

class _Clock(datetime):
    """datetime with a fixed utcnow(): 10:00 local on 12 November 2026."""

    @classmethod
    def utcnow(cls):
        return cls(2026, 11, 12, 8, 0)


def test_set_reminder_queues_for_the_local_time_and_the_courier_sends_it(sends):
    db = _db()
    _pair(db)
    with patch.object(ot, "datetime", _Clock):
        result, summary = ot.run_tool("set_reminder", {"text": "call the clinic", "when": "17:00"}, db)
    assert summary == "reminder set for 17:00" and result["when"] == "17:00" and "note" not in result
    row = db.query(models.Notification).one()
    assert row.kind == "reminder" and row.body == "call the clinic" and row.status == "queued"
    assert row.send_at == datetime(2026, 11, 12, 15, 0)          # 17:00 in Lusaka is 15:00 UTC
    assert result["change"]["table"] == "notifications" and result["change"]["action"] == "create"

    before = runner.run_agent(reminder_courier.SPEC, "schedule", now=utc(16, 45))
    assert before["status"] == "skipped" and sends == []
    due = runner.run_agent(reminder_courier.SPEC, "schedule", now=utc(17))
    assert due["status"] == "completed" and due["summary"] == "1 sent"
    assert [c["json"]["text"] for c in sends] == ["Reminder\ncall the clinic"]
    assert due["changes_count"] == 0                             # the courier writes nothing to the change log

    # Undo removes a reminder that has not gone yet.
    with patch.object(ot, "datetime", _Clock):
        second, _ = ot.run_tool("set_reminder", {"text": "stretch", "when": "in 2 hours"}, db)
    change_log.undo_change(db, second["change"]["id"])
    assert db.query(models.Notification).filter_by(body="stretch").count() == 0
    db.close()


def test_set_reminder_explains_what_it_cannot_do():
    db = _db()
    with patch.object(ot, "datetime", _Clock):
        unreadable, summary = ot.run_tool("set_reminder", {"text": "call the clinic", "when": "whenever"}, db)
        empty, _ = ot.run_tool("set_reminder", {"text": " ", "when": "17:00"}, db)
        unpaired, _ = ot.run_tool("set_reminder", {"text": "call the clinic", "when": "tomorrow 07:30"}, db)
    assert "error" in unreadable and "whenever" in summary and "error" in empty
    assert unpaired["when"] == "tomorrow 07:30" and "not paired" in unpaired["note"]
    assert db.query(models.Notification).count() == 1
    db.close()


def test_parse_when_reads_the_documented_forms():
    now = schedule.to_local(datetime(2026, 11, 12, 12, 0))       # Thursday 14:00 local
    local = lambda *a: schedule.to_utc(datetime(*a, tzinfo=now.tzinfo))
    cases = {
        "17:00": local(2026, 11, 12, 17, 0),
        "5pm": local(2026, 11, 12, 17, 0),
        "at 5": local(2026, 11, 12, 17, 0),                      # the next time the clock reads 5
        "at 9": local(2026, 11, 12, 21, 0),
        "09:00": local(2026, 11, 13, 9, 0),                      # already past today
        "5.30 pm": local(2026, 11, 12, 17, 30),
        "in 2 hours": local(2026, 11, 12, 16, 0),
        "in 30 minutes": local(2026, 11, 12, 14, 30),
        "in an hour": local(2026, 11, 12, 15, 0),
        "tomorrow 17:00": local(2026, 11, 13, 17, 0),
        "tomorrow at 5": local(2026, 11, 13, 17, 0),             # on another day 1–6 means the afternoon
        "tomorrow at 9": local(2026, 11, 13, 9, 0),
        "friday 9am": local(2026, 11, 13, 9, 0),
        "tomorrow": local(2026, 11, 13, 9, 0),                   # a day with no time means 09:00
        "15 November 14:30": local(2026, 11, 15, 14, 30),
        "May 12": local(2027, 5, 12, 9, 0),                      # a date, not twelve o'clock
        "today at 18:00": local(2026, 11, 12, 18, 0),
    }
    for text, expected in cases.items():
        assert business_common.parse_when(text, now) == expected, text
    for text in ("", "whenever", "today", "25:00", "13pm", "yesterday 10:00", "today at 13:00"):
        assert business_common.parse_when(text, now) is None, text


# ── Settings: endpoints and the tool ─────────────────────────

def test_settings_endpoint_returns_defaults_and_pairing_details(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "FounderOsBot")
    body = client.get("/notifications/settings").json()
    assert body["enabled"] is True and body["morning_time"] == "06:00" and body["evening_time"] is None
    assert (body["quiet_start"], body["quiet_end"]) == ("22:00", "05:00")
    assert body["alert_min_severity"] == "warn" and body["daily_cap"] == 12
    assert body["channels"] == {"telegram": True} and body["deadline_days"] == [30, 14, 7, 3, 1]
    assert body["paired"] is False and body["channel_configured"] is True
    assert body["bot_link"] == "https://t.me/FounderOsBot" and body["pair_code"] == tg.pair_code()
    assert BOT_TOKEN not in json.dumps(body)


def test_patch_settings_is_logged_and_undoable():
    r = client.patch("/notifications/settings", json={"morning_time": "5:45", "evening_time": "20:30",
                                                      "alert_min_severity": "critical", "daily_cap": 6})
    assert r.status_code == 200
    body = r.json()
    assert body["morning_time"] == "05:45" and body["evening_time"] == "20:30" and body["daily_cap"] == 6
    assert body["change"]["table"] == "notification_settings"
    assert "morning briefing at 05:45" in body["change"]["summary"]

    assert client.patch("/notifications/settings", json={"evening_time": "off"}).json()["evening_time"] is None
    for bad in ({"morning_time": "25:00"}, {"alert_min_severity": "loud"}, {"daily_cap": 500}, {"quiet_end": "soon"}):
        assert client.patch("/notifications/settings", json=bad).status_code == 422
    assert client.get("/notifications/settings").json()["morning_time"] == "05:45"       # a bad value changes nothing

    assert client.post(f"/changes/{body['change']['id']}/undo").status_code == 200
    restored = client.get("/notifications/settings").json()
    assert restored["morning_time"] == "06:00" and restored["daily_cap"] == 12


def test_notification_settings_tool_reads_and_changes():
    db = _db()
    read, summary = ot.run_tool("notification_settings", {}, db)
    assert summary == "nothing changed" and read["morning_time"] == "06:00" and read["evening_time"] == "off"
    assert read["quiet_hours"] == "22:00 to 05:00" and read["telegram_paired"] is False and read["change"] is None
    changed, summary = ot.run_tool("notification_settings", {"morning_time": "5:45", "quiet_start": "21:00"}, db)
    assert changed["morning_time"] == "05:45" and changed["quiet_hours"] == "21:00 to 05:00"
    assert summary == "Notifications: morning briefing at 05:45, quiet hours from 21:00"
    assert changed["change"]["table"] == "notification_settings"
    bad, summary = ot.run_tool("notification_settings", {"morning_time": "noonish"}, db)
    assert "error" in bad and "noonish" in summary
    off, _ = ot.run_tool("notification_settings", {"enabled": False}, db)
    assert off["enabled"] is False
    db.close()


def test_list_cancel_test_and_brief_now_endpoints(sends):
    db = _db()
    a = _queue(db, kind="reminder", title="Reminder", body="call the clinic", send_at=datetime(2030, 1, 1))
    _queue(db, kind="alert", body="Payment overdue.")
    listed = client.get("/notifications").json()
    assert [n["kind"] for n in listed] == ["alert", "reminder"]                           # newest first
    assert [n["id"] for n in client.get("/notifications?kind=reminder&status=queued").json()] == [a["id"]]

    assert client.post(f"/notifications/{a['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/notifications/{a['id']}/cancel").status_code == 409
    assert client.post("/notifications/99999/cancel").status_code == 404

    assert client.post("/notifications/test").json() == {"status": "suppressed", "error": "Telegram is not paired yet"}
    _pair(db)
    assert client.post("/notifications/test").json() == {"status": "sent", "error": None}
    assert sends[-1]["json"]["text"] == "Founder OS connected"

    assert client.post("/notifications/brief-now").json() == {"status": "sent", "error": None}
    assert "Reply here to log anything." in sends[-1]["json"]["text"]
    manual = db.query(models.Notification).filter(models.Notification.kind == "briefing").one()
    assert manual.dedupe_key.startswith("briefing:") and ":manual:" in manual.dedupe_key
    # Only the message asked for went out: the queued alert was left for the next dispatch.
    assert db.query(models.Notification).filter_by(kind="alert").one().status == "queued"
    db.close()


def test_briefing_reports_when_it_was_delivered(sends):
    db = _db()
    body = client.get("/briefing/today").json()
    assert body["delivery"] == {"last_sent_at": None, "channel": "telegram", "paired": False}
    _pair(db)
    assert client.post("/notifications/brief-now").json()["status"] == "sent"
    delivery = client.get("/briefing/today").json()["delivery"]
    assert delivery["paired"] is True and delivery["last_sent_at"] is not None
    db.close()


def test_agent_context_notify_reaches_the_queue():
    from agents.base import AgentContext, AgentSpec, Permissions, Schedule
    db = _db()
    spec = AgentSpec(key="teller", name="Teller", description="", schedule=Schedule(), events=[],
                     permissions=Permissions(may_notify=True), run=lambda ctx: None)
    ctx = AgentContext(db=db, now=NOON, now_local=schedule.to_local(NOON), today=date(2026, 11, 12),
                       run=SimpleNamespace(log_text=""), spec=spec, row=None)
    out = ctx.notify(kind="alert", title="Warning", body="Something moved.", dedupe_key="alert:teller:1")
    db.commit()
    assert out["deduped"] is False and out["source"] == "agent:teller" and out["status"] == "queued"
    assert ctx.notify(kind="alert", title="Warning", body="Something moved.", dedupe_key="alert:teller:1")["deduped"]
    db.close()


def test_migration_creates_the_settings_row_once():
    from sqlalchemy import text
    from database import engine as app_engine
    from migrations.m022_notifications import run
    run()
    run()
    with app_engine.connect() as conn:
        rows = conn.execute(text("SELECT id, morning_time, quiet_start, quiet_end, alert_min_severity, daily_cap, "
                                 "channels_json, deadline_days_json FROM notification_settings")).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == (1, "06:00", "22:00", "05:00", "warn", 12, '{"telegram": true}', "[30, 14, 7, 3, 1]")
