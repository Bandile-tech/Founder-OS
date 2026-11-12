"""
Review-round fixes on the Phase 5/6 branch. Each section reproduces one reported problem
before its fix; the tests stay as the regression guard.

No network and no OpenAI.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import llm
import models
from tests.conftest import TestSessionLocal, client, engine


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


# ── 2. WhatsApp links for local numbers ───────────────────────

@pytest.mark.parametrize("stored", [
    "0977123456",            # local, with the trunk zero
    "977123456",             # local, no zero and no country code
    "+260977123456",         # international
    "260977123456",          # country code, no plus
    "+260 977 123 456",      # as people type it
    "(0977) 123-456",
    "00260977123456",        # international call prefix
])
def test_whatsapp_link_carries_the_country_code(stored, monkeypatch):
    import outreach_channels as oc

    monkeypatch.delenv("DEFAULT_COUNTRY_CODE", raising=False)
    assert oc.whatsapp_digits(stored) == "260977123456"
    lead = models.Lead(name="Mercy Banda", whatsapp=stored)
    message = models.OutreachMessage(body="Hi Mercy")
    out = oc.WhatsAppLinkChannel().send(None, message, lead)
    assert out == {"ok": True, "open_url": "https://wa.me/260977123456?text=Hi%20Mercy"}


def test_whatsapp_country_code_is_configurable_and_foreign_numbers_are_left_alone(monkeypatch):
    import outreach_channels as oc

    monkeypatch.setenv("DEFAULT_COUNTRY_CODE", "+27")
    assert oc.whatsapp_digits("0821234567") == "27821234567"
    assert oc.whatsapp_digits("821234567") == "27821234567"
    assert oc.whatsapp_digits("+260977123456") == "260977123456"       # a plus is always trusted
    monkeypatch.delenv("DEFAULT_COUNTRY_CODE")
    assert oc.whatsapp_digits("+27821234567") == "27821234567"
    assert oc.whatsapp_digits("27821234567") == "27821234567"          # too long to be local
    assert oc.whatsapp_digits("+1 (202) 555-0123") == "12025550123"
    assert oc.whatsapp_digits("") == "" and oc.whatsapp_digits(None) == ""
    lead = models.Lead(name="No Number")
    assert oc.WhatsAppLinkChannel().send(None, models.OutreachMessage(body="x"), lead)["ok"] is False


# ── 3. "today" is the day in FOUNDER_OS_TZ, not the server's ──

def _half_past_midnight_in_lusaka(monkeypatch):
    """00:30 on Friday 5 March 2027 in Lusaka = 22:30 on Thursday 4 March UTC, which is
    what a server on UTC (Render) believes the time to be."""
    import user_clock

    monkeypatch.setenv("FOUNDER_OS_TZ", "Africa/Lusaka")
    monkeypatch.setattr(user_clock, "utcnow", lambda: datetime(2027, 3, 4, 22, 30))
    return date(2027, 3, 5)


def test_briefing_today_is_the_lusaka_day_at_half_past_midnight(monkeypatch):
    today = _half_past_midnight_in_lusaka(monkeypatch)
    db = _db()
    db.add(models.Todo(text="Send the Hope Clinic invoice", priority=2, done=False, category="business",
                       due_date=today, source="test"))
    db.add(models.Todo(text="Chase the bakery deposit", priority=2, done=False, category="business",
                       due_date=today - timedelta(days=1), source="test"))
    db.commit()
    db.close()

    b = client.get("/briefing/today").json()
    assert (b["date"], b["weekday"], b["greeting"]) == ("2027-03-05", "Friday", "Good morning")
    labels = {i["title"]: i["due_label"] for i in b["items"] if i["category"] not in ("non_negotiable",)}
    assert labels["Send the Hope Clinic invoice"] == "Today"
    assert b["counts"]["overdue"] == 1          # yesterday's task is overdue, today's is not


def test_everything_dated_today_follows_the_user_zone(monkeypatch):
    import orchestrator_tools as ot
    import user_clock

    today = _half_past_midnight_in_lusaka(monkeypatch)
    assert user_clock.today() == today and user_clock.now() == datetime(2027, 3, 5, 0, 30)

    db = _db()
    todo = models.Todo(text="Print the syllabus", priority=3, done=False, category="academics", source="test")
    db.add(todo)
    db.commit()
    todo_id = todo.id
    db.close()
    assert client.post(f"/todos/{todo_id}/complete").json()["completed_at"] == "2027-03-05"

    habits = client.get("/habits").json()                       # the day's ticks are the local day's
    assert habits and all(h["date"] == "2027-03-05" for h in habits)

    db = _db()
    result, _ = ot.get_deadlines(db, 7)
    db.close()
    assert result["today"] == "2027-03-05"

    monkeypatch.setenv("FOUNDER_OS_TZ", "UTC")                  # the same instant, seen from UTC
    assert user_clock.today() == date(2027, 3, 4)
    assert client.get("/briefing/today").json()["date"] == "2027-03-04"


def test_no_app_code_reads_the_server_clock_for_today():
    """date.today() and datetime.now() are the server's day. App code goes through user_clock."""
    import re

    root = Path(__file__).parent.parent
    pattern = re.compile(r"(?<![\w.])(?:_date|date)\.today\(\)|(?<![\w.])datetime\.(?:now|today)\(\)")
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] in ("tests", "migrations") or "__pycache__" in rel.parts or rel.name == "user_clock.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("#") and pattern.search(line):
                offenders.append(f"{rel}:{number}")
    assert offenders == []


# ── Review, part 3: undoing an old batch must not overwrite what happened since ──

def _old_agent_run(db):
    """An agent run that scored a lead and created a follow-up task, in one batch."""
    import change_log
    lead = models.Lead(name="Mercy Banda", status="new")
    db.add(lead)
    db.commit()
    batch_id = change_log.begin_batch(db, "agent:lead_qualifier")
    before = change_log.snapshot(lead)
    lead.qualification_score = 72
    change_log.record_update(db, lead, before, "Scored Mercy Banda 72")
    todo = models.Todo(text="Follow up with Mercy Banda", done=False, priority=3)
    db.add(todo)
    db.flush()
    change_log.record_create(db, todo, "Added task: Follow up with Mercy Banda")
    db.commit()
    change_log.end_batch(db)
    return lead, todo, batch_id


def test_undoing_an_old_agent_run_keeps_what_was_changed_afterwards():
    """The Agents page offers Undo on runs that may be days old. Undo restored the whole
    row as it was before the run, wiping every later edit, and deleted rows the run had
    created even after Bandile had worked on them."""
    import change_log
    db = _db()
    lead, todo, batch_id = _old_agent_run(db)

    # Later: Bandile moves the lead on (logged) and completes the task (a plain tap).
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(lead)
        lead.status, lead.notes = "contacted", "Called her on Tuesday"
        change_log.record_update(db, lead, before, "Contacted Mercy Banda")
        db.commit()
    assert client.post(f"/todos/{todo.id}/toggle").json()["done"] is True

    out = client.post(f"/changes/batch/{batch_id}/undo").json()
    db.expire_all()
    lead = db.query(models.Lead).one()
    # The run's own change is reversed; what Bandile did afterwards is untouched.
    assert (lead.qualification_score, lead.status, lead.notes) == (None, "contacted", "Called her on Tuesday")
    done = db.query(models.Todo).filter_by(text="Follow up with Mercy Banda").one()      # not deleted
    assert done.done is True
    assert out["undone"] == 1 and out["kept"] == 1 and "changed afterwards" in out["note"]
    db.close()


def test_a_column_changed_since_is_left_alone_and_a_single_undo_says_so():
    import change_log
    db = _db()
    lead, todo, batch_id = _old_agent_run(db)
    score_change = db.query(models.ChangeLog).filter_by(table_name="leads").one()
    with change_log.batch_scope(db, "ui"):                       # the same column, changed again later
        before = change_log.snapshot(lead)
        lead.qualification_score = 90
        change_log.record_update(db, lead, before, "Rescored Mercy Banda 90")
        db.commit()
    refused = client.post(f"/changes/{score_change.id}/undo")
    assert refused.status_code == 409 and "changed afterwards" in refused.json()["detail"]
    db.expire_all()
    assert db.query(models.Lead).one().qualification_score == 90
    assert db.query(models.ChangeLog).filter_by(id=score_change.id).one().undone_at is None
    db.close()


def test_an_untouched_batch_still_undoes_completely():
    db = _db()
    lead, todo, batch_id = _old_agent_run(db)
    out = client.post(f"/changes/batch/{batch_id}/undo").json()
    db.expire_all()
    assert out["undone"] == 2 and "kept" not in out
    assert db.query(models.Lead).one().qualification_score is None and db.query(models.Todo).count() == 0
    db.close()
