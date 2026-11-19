"""
Phase 7, Stage 4 — proactive delivery over Web Push.

No network: the one function that posts to a push service (``push._deliver``) is replaced.
The keys below are made for the test and sign nothing real.

  1. subscribing a device, and which endpoints the server will post to
  2. a notification leaves by push when a device is on, with a view to open
  3. what is pushed, all of it deterministic: the morning briefing (greeting line and the
     top three items), the evening check, reminders 7, 3 and 1 days out, payments due,
     the cost-cap alert
  4. QUIET_HOURS: nothing at all inside the window
  5. the schedule: three wakes a day, "last scheduled run" in the briefing, an alert past 26 hours
  6. the service worker and the settings the screen needs
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import briefing
import llm
import models
import notify
import push
from agents import alert_courier, briefing_courier, reminder_courier, runner
from tests.conftest import TestSessionLocal, client

ENDPOINT = "https://fcm.googleapis.com/fcm/send/synthetic-device-1"
KEYS = {"p256dh": "BSyntheticPublicKeyForATestDevice", "auth": "c3ludGhldGljLWF1dGg"}
TODAY = date(2026, 11, 12)                     # a Thursday


def utc(hour, minute=0, day=12):
    """A local (Lusaka) clock time on a November 2026 day, as naive UTC."""
    return datetime(2026, 11, day, hour, minute) - timedelta(hours=2)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "FOUNDER_OS_PASSWORD", "QUIET_HOURS",
                 "JOBS_TOKEN", "MONTHLY_LLM_CAP_USD", "VAPID_SUBJECT", "FOUNDER_OS_API_URL"):
        monkeypatch.delenv(name, raising=False)
    from tools.vapid_keys import make_pair
    public, private = make_pair()
    monkeypatch.setenv("VAPID_PUBLIC_KEY", public)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    monkeypatch.setenv("FOUNDER_OS_TZ", "Africa/Lusaka")
    llm.reset_client()
    llm.clear_run_context()
    with patch.object(runner, "SessionLocal", TestSessionLocal), patch("database.SessionLocal", TestSessionLocal):
        yield
    llm.clear_run_context()


@pytest.fixture
def pushed():
    """Every push the server would post, decoded: [{endpoint, title, body, url, tag}]."""
    seen = []

    def deliver(subscription, payload):
        seen.append({"endpoint": subscription["endpoint"], **json.loads(payload)})

    with patch.object(push, "_deliver", side_effect=deliver):
        yield seen


def _db():
    return TestSessionLocal()


def _device(endpoint=ENDPOINT):
    r = client.post("/push/subscribe", json={"endpoint": endpoint, "keys": KEYS})
    assert r.status_code == 200, r.text
    return r.json()


def _run(spec, now):
    return runner.run_agent(spec, "schedule", now=now)


# ════════════════════════════════════════════════════════════════
# 1. Subscribing a device
# ════════════════════════════════════════════════════════════════

def test_a_device_subscribes_once_and_can_leave():
    assert client.get("/push/config").json()["subscriptions"] == 0
    state = _device()
    assert state["subscribed"] is True and state["configured"] is True and state["subscriptions"] == 1
    assert state["public_key"] and len(state["public_key"]) > 80             # what the browser subscribes with
    assert _device()["subscriptions"] == 1                                   # the same device again: still one
    assert _device("https://updates.push.services.mozilla.com/wpush/v2/synthetic-2")["subscriptions"] == 2
    assert client.get("/notifications/settings").json()["channel"] == "push"
    left = client.post("/push/unsubscribe", json={"endpoint": ENDPOINT}).json()
    assert left == {"subscribed": False, "configured": True, "public_key": state["public_key"], "subscriptions": 1}
    assert "VAPID_PRIVATE_KEY" not in json.dumps(state) and "private" not in json.dumps(state).lower()


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x",                 # not https
    "https://example.com/collect",                          # not a push service
    "https://fcm.googleapis.com.evil.example/fcm/send/x",   # a look-alike host
    "https://127.0.0.1/internal", "not a url",
])
def test_only_real_push_services_are_ever_posted_to(endpoint):
    r = client.post("/push/subscribe", json={"endpoint": endpoint, "keys": KEYS})
    assert r.status_code == 400 and client.get("/push/config").json()["subscriptions"] == 0


def test_without_keys_nothing_subscribes_and_nothing_is_sent(monkeypatch, pushed):
    monkeypatch.delenv("VAPID_PRIVATE_KEY")
    assert client.get("/push/config").json() == {"configured": False, "public_key": client.get("/push/config").json()["public_key"],
                                                 "subscriptions": 0}
    assert client.post("/push/subscribe", json={"endpoint": ENDPOINT, "keys": KEYS}).status_code == 503
    assert client.post("/notifications/test").json()["status"] == "suppressed" and pushed == []


# ════════════════════════════════════════════════════════════════
# 2. A notification leaves by push, and says which view to open
# ════════════════════════════════════════════════════════════════

def test_a_notification_goes_to_every_device_and_opens_its_view(pushed):
    _device()
    _device("https://web.push.apple.com/synthetic-3")
    sent = client.post("/notifications/test").json()
    assert sent == {"status": "sent", "error": None}
    assert [p["endpoint"] for p in pushed] == [ENDPOINT, "https://web.push.apple.com/synthetic-3"]
    assert pushed[0]["title"] == "Founder OS" and pushed[0]["body"] == "Founder OS connected"
    assert pushed[0]["url"] == "/app/#go=settings"
    row = client.get("/notifications?limit=1").json()[0]
    assert (row["channel"], row["status"], row["target"]) == ("push", "sent", "settings")


def test_a_device_whose_subscription_ended_is_forgotten_and_a_failing_one_is_reported():
    _device()
    _device("https://web.push.apple.com/synthetic-gone")

    def deliver(subscription, payload):
        if subscription["endpoint"].endswith("gone"):
            error = RuntimeError("gone")
            error.response = SimpleNamespace(status_code=410)
            raise error

    with patch.object(push, "_deliver", side_effect=deliver):
        assert client.post("/notifications/test").json()["status"] == "sent"       # one device still took it
    assert client.get("/push/config").json()["subscriptions"] == 1

    def refuse(subscription, payload):
        error = RuntimeError("push service said no")
        error.response = SimpleNamespace(status_code=500)
        raise error

    with patch.object(push, "_deliver", side_effect=refuse):
        db = _db()
        queued = notify.queue(db, kind="alert", body="Something.")
        db.commit()
        out = notify.dispatch(db, utc(12))
        row = db.query(models.Notification).filter(models.Notification.id == queued["id"]).one()
        assert out["retrying"] == 1 and row.status == "queued" and "attempt 1/3" in row.error
        db.close()


def test_telegram_is_still_used_when_no_device_is_on(monkeypatch, pushed):
    import integrations_telegram as tg
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567:AASyntheticTokenForTests")
    db = _db()
    tg.pair(db, 4455667788, "someone")
    db.commit()
    posts = []
    with patch.object(notify.httpx, "post", side_effect=lambda url, json=None, timeout=None: posts.append(json) or SimpleNamespace(
            status_code=200, json=lambda: {"ok": True, "result": {"message_id": 1}})):
        assert notify.active_channel(db) == "telegram"
        assert client.post("/notifications/test").json()["status"] == "sent"
        _device()                                            # a device switches on: push takes over
        assert notify.active_channel(db) == "push"
        assert client.post("/notifications/test").json()["status"] == "sent"
    db.close()
    assert len(posts) == 1 and len(pushed) == 1


# ════════════════════════════════════════════════════════════════
# 3. What is pushed
# ════════════════════════════════════════════════════════════════

def _seed_day(db):
    db.add(models.Todo(text="Email the insurance CEO", done=False, due_date=date(2026, 11, 10), priority=1))
    db.add(models.Todo(text="Finish the Economics essay", done=False, due_date=TODAY, priority=2))
    db.add(models.Todo(text="Book the dentist", done=False, due_date=TODAY, priority=3))
    db.add(models.Todo(text="Read chapter 4", done=False, due_date=TODAY + timedelta(days=20), priority=5))
    db.commit()


def test_the_morning_briefing_is_the_greeting_line_and_the_top_three_items(pushed):
    _device()
    db = _db()
    _seed_day(db)
    assert _run(briefing_courier.SPEC, utc(5, 40))["status"] == "skipped" and pushed == []      # before 05:45
    first = _run(briefing_courier.SPEC, utc(5, 47))
    assert first["status"] == "completed" and len(pushed) == 1
    note = pushed[0]
    assert note["title"] == "Good morning, Bandile" and note["url"] == "/app/#go=home"
    assert note["tag"] == "briefing:2026-11-12"
    lines = note["body"].split("\n")
    assert len(lines) == 4                                   # the greeting line, then three items
    assert lines[1].startswith("1. Email the insurance CEO") and "overdue" in lines[1]
    assert lines[2].startswith("2. ") and lines[3].startswith("3. ")
    assert first["llm_calls"] == 0                           # no key here: the line is the deterministic one
    assert _run(briefing_courier.SPEC, utc(12, 31))["status"] == "skipped" and len(pushed) == 1   # once a day
    db.close()


def test_the_only_model_call_in_a_whole_day_of_pushes_is_the_one_briefing_summary(monkeypatch, pushed):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _device()
    db = _db()
    _seed_day(db)
    db.add(models.Todo(text="Submit the form", done=False, due_date=TODAY + timedelta(days=3)))
    db.commit()
    calls = []
    with patch("llm.chat_text", side_effect=lambda messages, **kw: calls.append(kw.get("purpose")) or
               "Two things are late. Start with the CEO email."):
        for spec, at in ((briefing_courier.SPEC, utc(5, 47)), (alert_courier.SPEC, utc(5, 48)),
                         (reminder_courier.SPEC, utc(5, 49)), (alert_courier.SPEC, utc(12, 31)),
                         (briefing_courier.SPEC, utc(19, 46)), (alert_courier.SPEC, utc(19, 47))):
            _run(spec, at)
    assert calls == ["briefing_summary"]                     # once, in the morning; the evening check makes none
    morning = next(p for p in pushed if p["tag"] == "briefing:2026-11-12")
    assert morning["body"].split("\n")[0] == "Two things are late."         # the greeting line is its first sentence
    assert {p["tag"] for p in pushed} >= {"briefing:2026-11-12", "evening:2026-11-12"}
    db.close()


def test_the_evening_check_says_what_is_still_open_and_what_carried_over(pushed):
    _device()
    db = _db()
    _seed_day(db)
    assert _run(briefing_courier.SPEC, utc(19, 40))["result"]["queued"] == ["briefing"]          # 19:45 not reached
    evening = _run(briefing_courier.SPEC, utc(19, 46))
    assert evening["result"]["queued"] == ["evening check"] and evening["llm_calls"] == 0
    note = pushed[-1]
    assert note["title"] == "Evening check" and note["url"] == "/app/#go=home"
    lines = note["body"].split("\n")
    assert lines[0].startswith("2 still open today, 1 carried over")
    assert lines[1].startswith("• Email the insurance CEO")                   # what carried over comes first
    assert {line.split(" (")[0] for line in lines[2:]} == {"• Finish the Economics essay", "• Book the dentist"}

    for todo in db.query(models.Todo).all():                 # a clear day says so
        todo.done = True
    db.commit()
    title, body = notify.evening_message(db, utc(19, 46, day=13), "push")
    assert body.startswith("Everything for today is done") or "non-negotiable" in body
    db.close()


def test_reminders_go_out_seven_three_and_one_days_before_and_only_once(pushed):
    _device()
    db = _db()
    for days, text in ((7, "Scholarship essay"), (3, "Renew the domain"), (1, "Pay the exam fee"),
                       (5, "Not on a mark"), (0, "Due today"), (14, "Two weeks out")):
        db.add(models.Todo(text=text, done=False, due_date=TODAY + timedelta(days=days)))
    db.add(models.Todo(text="Already done", done=True, due_date=TODAY + timedelta(days=3)))
    db.add(models.ExamDate(subject_code="9709", paper="P1", exam_date=TODAY + timedelta(days=3)))
    db.add(models.ExamDate(subject_code="9708", paper="P2", exam_date=TODAY + timedelta(days=30)))
    db.commit()
    assert notify.deadline_days(notify.settings(db)) == [7, 3, 1]

    _run(alert_courier.SPEC, utc(5, 47))
    deadlines = {p["tag"]: p for p in pushed if p["tag"].startswith("deadline:")}
    todo_notes = sorted(p["body"] for tag, p in deadlines.items() if tag.startswith("deadline:todo:"))
    assert todo_notes == ["Pay the exam fee is due tomorrow (13 Nov).", "Renew the domain is due in 3 days (15 Nov).",
                          "Scholarship essay is due in 7 days (19 Nov)."]
    exam = next(p for tag, p in deadlines.items() if tag.startswith("deadline:exam:"))
    assert "exam in 3 days (15 Nov)" in exam["body"] and exam["url"] == "/app/#go=syllabus"
    assert len(deadlines) == 4                               # nothing for 5, 14 or 30 days out, or a finished task
    count = len(pushed)
    _run(alert_courier.SPEC, utc(12, 31))
    _run(alert_courier.SPEC, utc(19, 47))
    assert len(pushed) == count                              # each reminder once, however often the schedule runs
    db.close()


def test_a_client_payment_due_today_and_one_overdue_are_pushed_to_the_clients_tab(pushed):
    """A whole scheduled tick, on the real clock: the housekeeper flags what is overdue,
    and the alert courier, which runs last, sends both."""
    import user_clock
    _device()
    db = _db()
    settings = notify.settings(db)
    settings.quiet_start = settings.quiet_end = "00:00"      # no quiet window: this test runs at any hour
    today = user_clock.today()
    client_row = models.Client(name="Sample Client Ltd", status="active", currency="ZMW")
    db.add(client_row)
    db.flush()
    db.add(models.Payment(client_id=client_row.id, amount=1250.0, currency="ZMW", status="due", due_date=today,
                          title="Balance"))
    db.add(models.Payment(client_id=client_row.id, amount=450.0, currency="ZMW", status="due",
                          due_date=today - timedelta(days=4), title="Hosting"))
    db.commit()
    out = runner.tick("github")
    assert out["errors"] == 0, out
    notes = {p["title"]: p for p in pushed}
    assert notes["Payment due today"]["body"] == "Payment from Sample Client Ltd of K1,250 is due today."
    assert notes["Payment overdue"]["body"] == "Payment from Sample Client Ltd of K450 is 4 days overdue."
    assert {notes["Payment due today"]["url"], notes["Payment overdue"]["url"]} == {"/app/#go=business:clients"}
    # The alert courier again, later the same day: neither is sent a second time.
    runner.run_agent(alert_courier.SPEC, "schedule")
    assert len([p for p in pushed if p["title"] in ("Payment due today", "Payment overdue")]) == 2
    db.close()


def test_the_cost_cap_alert_is_pushed_once_a_month(monkeypatch, pushed):
    monkeypatch.setenv("MONTHLY_LLM_CAP_USD", "10")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _device()
    db = _db()
    db.add(models.LLMUsage(created_at=datetime.utcnow(), model="gpt-5.4-mini", purpose="assistant",
                           provider="openai", task="assistant", estimated_cost_usd=8.5))
    db.commit()
    llm.month_to_date(db, fresh=True)
    now = datetime.utcnow()
    _run(alert_courier.SPEC, now)
    _run(alert_courier.SPEC, now + timedelta(hours=7))
    caps = [p for p in pushed if p["title"] == "AI spend"]
    assert len(caps) == 1 and "85% of this month's $10.00 cap" in caps[0]["body"]
    assert caps[0]["url"] == "/app/#go=settings"
    db.close()


# ════════════════════════════════════════════════════════════════
# 4. QUIET_HOURS: nothing at all inside the window
# ════════════════════════════════════════════════════════════════

def test_quiet_hours_from_the_environment_hold_everything_until_the_window_ends(monkeypatch, pushed):
    monkeypatch.setenv("QUIET_HOURS", "21:00-05:30")
    _device()
    db = _db()
    assert client.get("/notifications/settings").json()["quiet_hours_env"] == "21:00-05:30"
    for kind in ("alert", "reminder", "reply", "deadline"):   # reminders and replies too: nothing means nothing
        notify.queue(db, kind=kind, body=f"A {kind}.")
    db.commit()
    assert notify.dispatch(db, utc(23, 15))["held"] == 4 and pushed == []
    assert notify.dispatch(db, utc(4, 0, day=13))["held"] == 0 and pushed == []          # still waiting for 05:30
    assert notify.dispatch(db, utc(5, 29, day=13)) == {"sent": 0, "held": 0, "suppressed": 0, "failed": 0, "retrying": 0}
    out = notify.dispatch(db, utc(5, 31, day=13))
    assert out["sent"] == 4 and len(pushed) == 4
    # It replaces the window in Settings: 21:30 is quiet here although Settings says 22:00.
    notify.queue(db, kind="alert", body="Half past nine.")
    db.commit()
    assert notify.dispatch(db, utc(21, 30, day=13))["held"] == 1
    db.close()


@pytest.mark.parametrize("value", ["", "nonsense", "25:00-05:00", "22:00"])
def test_an_unreadable_QUIET_HOURS_falls_back_to_the_window_in_settings(monkeypatch, value, pushed):
    monkeypatch.setenv("QUIET_HOURS", value)
    _device()
    db = _db()
    assert notify.env_quiet_hours() is None
    notify.queue(db, kind="alert", body="Late.")
    notify.queue(db, kind="reminder", body="Call the clinic.")
    db.commit()
    out = notify.dispatch(db, utc(23, 0))                    # 22:00 to 05:00 from Settings; a reminder is exempt there
    assert (out["held"], out["sent"]) == (1, 1)
    db.close()


# ════════════════════════════════════════════════════════════════
# 5. The schedule
# ════════════════════════════════════════════════════════════════

def test_last_scheduled_run_is_in_the_briefing_and_an_alert_is_raised_past_26_hours(monkeypatch):
    def tick(hours_ago, trigger="github"):
        db = _db()
        db.add(models.JobTick(trigger=trigger, started_at=datetime.utcnow() - timedelta(hours=hours_ago)))
        db.commit()
        db.close()

    def scheduler():
        return client.get("/briefing/today").json()

    # Not set up at all: nothing is expected, so nothing is wrong.
    body = scheduler()
    assert body["scheduler"] == {"last_run_at": None, "label": None, "hours_ago": None, "expected": False, "stale": False}
    assert not [a for a in body["alerts"] if a["rule_id"] == "scheduler_stale"]

    monkeypatch.setenv("JOBS_TOKEN", "a-synthetic-jobs-token-of-32-chars!!")
    body = scheduler()                                       # set up, and it has never run
    assert body["scheduler"]["stale"] is True
    alert, = [a for a in body["alerts"] if a["rule_id"] == "scheduler_stale"]
    assert "has not run yet" in alert["message"] and "60 days" in alert["message"]

    tick(30, trigger="in_process")                           # the app being open is not the schedule
    tick(27)
    body = scheduler()
    assert body["scheduler"]["stale"] is True and body["scheduler"]["hours_ago"] == 27.0
    assert "27 hours ago" in next(a for a in body["alerts"] if a["rule_id"] == "scheduler_stale")["message"]

    tick(25.5)                                               # inside 26 hours: fine
    body = scheduler()
    assert body["scheduler"]["stale"] is False and body["scheduler"]["hours_ago"] == 25.5
    assert not [a for a in body["alerts"] if a["rule_id"] == "scheduler_stale"]
    tick(0.1)
    state = scheduler()["scheduler"]
    assert state["stale"] is False and len(state["label"]) in (5, 15) and ":" in state["label"]


def test_a_stopped_schedule_is_pushed_once_a_day_while_the_app_is_open(monkeypatch, pushed):
    """The schedule cannot report its own silence; the in-process tick, which runs while the
    app is open, can."""
    monkeypatch.setenv("JOBS_TOKEN", "a-synthetic-jobs-token-of-32-chars!!")
    _device()
    db = _db()
    settings = notify.settings(db)
    settings.quiet_start = settings.quiet_end = "00:00"
    db.add(models.JobTick(trigger="github", started_at=datetime.utcnow() - timedelta(hours=50)))
    db.commit()
    state = briefing.scheduler_state(db)
    assert state["stale"] is True and state["hours_ago"] == 50.0 and state["label"]
    runner.run_agent(alert_courier.SPEC, "in_process")
    runner.run_agent(alert_courier.SPEC, "in_process")
    stopped = [p for p in pushed if p["title"] == "Schedule"]
    assert len(stopped) == 1 and "has not run since" in stopped[0]["body"] and stopped[0]["url"] == "/app/#go=agents"
    db.close()


def test_the_three_wakes_cover_the_morning_briefing_and_the_evening_check():
    text = (Path(__file__).parent.parent.parent / ".github" / "workflows" / "jobs-tick.yml").read_text(encoding="utf-8")
    crons = [line.split('"')[1] for line in text.splitlines() if line.strip().startswith("- cron:")]
    local = sorted(f"{(int(c.split()[1]) + 2) % 24:02d}:{int(c.split()[0]):02d}" for c in crons)   # Lusaka is UTC+2
    assert local == ["05:45", "12:30", "19:45"]
    assert notify.DEFAULTS["morning_time"] == local[0] and notify.DEFAULTS["evening_time"] == local[2]


# ════════════════════════════════════════════════════════════════
# 6. The service worker and what the screen needs
# ════════════════════════════════════════════════════════════════

def test_the_service_worker_is_served_inside_the_manifest_scope():
    frontend = Path(__file__).parent.parent.parent / "frontend"
    manifest = json.loads((frontend / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["scope"] == "./" and manifest["start_url"] == "./"
    worker = (frontend / "sw.js").read_text(encoding="utf-8")
    for needed in ('addEventListener("push"', 'addEventListener("notificationclick"', "showNotification", "openWindow"):
        assert needed in worker
    assert "caches" not in worker.replace("It caches nothing", "") and 'addEventListener("fetch"' not in worker
    served = client.get("/app/sw.js")                        # no session needed: it sits with the static app
    assert served.status_code == 200 and "javascript" in served.headers["content-type"]
    page = (frontend / "index.html").read_text(encoding="utf-8")
    assert 'register("sw.js",{scope:"./"})' in page
    assert "On iPhone, notifications only work from the app added to the Home Screen" in page


def test_every_target_a_notification_can_carry_is_a_view_the_app_can_open():
    page = (Path(__file__).parent.parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
    start = page.index("function goTarget(target){")
    handler = page[start:page.index("function handleGoHash", start)]
    source = "".join((Path(__file__).parent.parent / name).read_text(encoding="utf-8")
                     for name in ("notify.py", "agents/alert_courier.py", "agents/briefing_courier.py"))
    import re
    named = r'"(home|settings|applications|syllabus|agents|budget|history|business:[a-z]+)"'
    targets = {t for line in source.splitlines() if "target" in line for t in re.findall(named, line)}
    targets |= set(notify.DEFAULT_TARGETS.values())
    assert {"home", "settings", "applications", "syllabus", "agents", "business:clients", "business:pipeline"} <= targets
    for target in targets:
        page_name = target.split(":")[0]
        assert page_name == "home" or f'page==="{page_name}"' in handler, target
    assert notify.target_url("business:clients") == "/app/#go=business:clients"
    assert notify.target_url(None) == "/app/#go=home"
