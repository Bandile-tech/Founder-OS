"""
Client, deal and milestone tools (the "Bel Baby Shop is a client" bug from live use).

  1. the orchestrator exposes create_client, create_deal, record_payment, add_recurring
     and mark_milestone
  2. create_client: client + owner as primary contact, DEFAULT_CURRENCY, no duplicate, a
     matching lead is converted, unsure files a suggestion that one tap applies
  3. create_deal: value and stage, linked to the client
  4. record_payment: paid writes revenue; due with a trigger has no date; a due payment
     recorded in the same turn is never settled by the paid one
  5. add_recurring: a start date runs; a start trigger stays pending and out of MRR
  6. mark_milestone: dates the waiting payment, starts the waiting recurring, leaves
     other clients and other triggers alone; one Undo puts it all back
  7. the live message, as five tool calls in one turn: one batch, one Undo
  8. the prompt carries the client-path rules and m025 is wired into startup

No network and no OpenAI.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import crm
import llm
import models
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client

TODAY = date.today()
LIVE_MESSAGE = ("Bel Baby Shop is a client. Owner Christabel Sintema. Website deal: K2,500 set-up, "
                "K1,250 deposit paid, K1,250 due on go-live, then K450 a month from go-live.")
LIVE_CALLS = [
    ("create_client", {"name": "Bel Baby Shop", "owner": "Christabel Sintema"}),
    ("create_deal", {"client": "Bel Baby Shop", "title": "Website", "amount": 2500, "stage": "won"}),
    ("record_payment", {"client": "Bel Baby Shop", "amount": 1250, "status": "paid", "title": "Deposit"}),
    ("record_payment", {"client": "Bel Baby Shop", "amount": 1250, "status": "due",
                        "due_trigger": "on go-live", "title": "Balance"}),
    ("add_recurring", {"client": "Bel Baby Shop", "amount": 450, "frequency": "monthly",
                       "start_trigger": "from go-live"}),
]


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEFAULT_CURRENCY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


def _run(name, args):
    db = _db()
    try:
        return ot.run_tool(name, args, db)[0]
    finally:
        db.close()


def _bel():
    """The live message's facts, one tool at a time."""
    return [_run(name, args) for name, args in LIVE_CALLS]


def _counts(db):
    return {m.__tablename__: db.query(m).count()
            for m in (models.Client, models.Contact, models.Deal, models.Payment,
                      models.RecurringRevenue, models.Revenue, models.Lead)}


def _scripted_stream(rounds):
    state = {"i": 0}

    def _fake(convo, tools):
        i = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        yield from rounds[i]

    return _fake


def _orchestrate(text, rounds):
    with patch("openai_client._stream_completion", _scripted_stream(rounds)):
        r = client.post("/orchestrator", json={"text": text, "session_id": "s"})
    assert r.status_code == 200
    return [json.loads(line[5:]) for line in r.text.split("\n") if line.startswith("data:")]


# ── 1. the tools exist ────────────────────────────────────────

def test_the_orchestrator_exposes_the_client_path_tools():
    names = {t["function"]["name"] for t in ot.TOOL_SCHEMAS}
    wanted = {"create_client", "create_deal", "record_payment", "add_recurring", "mark_milestone"}
    assert wanted <= names
    assert wanted <= set(ot._TOOL_DISPATCH)
    assert len(names) == len(ot.TOOL_SCHEMAS)          # no tool registered twice
    payment = next(t for t in ot.TOOL_SCHEMAS if t["function"]["name"] == "record_payment")
    assert "due_trigger" in payment["function"]["parameters"]["properties"]


# ── 2. create_client ──────────────────────────────────────────

def test_create_client_adds_the_client_and_the_owner_as_primary_contact():
    result = _run("create_client", {"name": "Bel Baby Shop", "owner": "Christabel Sintema"})
    assert result["created"] is True and result["currency"] == "ZMW"
    db = _db()
    c = db.query(models.Client).one()
    assert (c.name, c.status, c.currency) == ("Bel Baby Shop", "active", "ZMW")
    contact = db.query(models.Contact).one()
    assert (contact.client_id, contact.name, contact.role, contact.is_primary) == (
        c.id, "Christabel Sintema", "owner", True)
    assert db.query(models.Lead).count() == 0
    db.close()


def test_create_client_currency_follows_default_currency_unless_given(monkeypatch):
    monkeypatch.setenv("DEFAULT_CURRENCY", "USD")
    assert _run("create_client", {"name": "Dollar Client"})["currency"] == "USD"
    assert _run("create_client", {"name": "Kwacha Client", "currency": "ZMW"})["currency"] == "ZMW"


def test_create_client_does_not_duplicate_a_client_on_file():
    _run("create_client", {"name": "Bel Baby Shop", "owner": "Christabel Sintema"})
    again = _run("create_client", {"name": "Bel Baby Shop", "owner": "Christabel Sintema"})
    assert again["already_on_file"] is True and again["contact_added"] is None
    db = _db()
    assert db.query(models.Client).count() == 1 and db.query(models.Contact).count() == 1
    db.close()


def test_create_client_converts_the_lead_it_used_to_be():
    """What live use left behind: the business filed as a lead."""
    _run("add_lead", {"name": "Bel Baby Shop"})
    result = _run("create_client", {"name": "Bel Baby Shop", "owner": "Christabel Sintema"})
    assert result["converted_from_lead"] == "Bel Baby Shop"
    db = _db()
    lead, c = db.query(models.Lead).one(), db.query(models.Client).one()
    assert (lead.status, lead.client_id, c.lead_id) == ("converted", c.id, lead.id)
    assert db.query(models.Contact).one().name == "Christabel Sintema"
    db.close()


def test_create_client_unsure_files_a_suggestion_and_one_tap_applies_it():
    result = _run("create_client", {"name": "Sunrise Pharmacy", "owner": "Mr Phiri", "unsure": True})
    assert result["suggested"] is True and result["written"] is False
    db = _db()
    assert db.query(models.Client).count() == 0 and db.query(models.Lead).count() == 0
    suggestion = db.query(models.Suggestion).one()
    assert suggestion.kind == "client_add" and suggestion.status == "pending"
    db.close()

    accepted = client.post(f"/suggestions/{suggestion.id}/accept").json()
    assert accepted["applied"] is True and accepted["changes"]
    db = _db()
    assert db.query(models.Client).one().name == "Sunrise Pharmacy"
    assert db.query(models.Contact).one().name == "Mr Phiri"
    db.close()


def test_create_client_needs_a_name():
    assert "error" in _run("create_client", {"name": "  "})


# ── 3. create_deal ────────────────────────────────────────────

def test_create_deal_records_value_and_stage_linked_to_the_client():
    _run("create_client", {"name": "Bel Baby Shop"})
    deal = _run("create_deal", {"client": "Bel Baby Shop", "title": "Website", "amount": 2500, "stage": "won"})
    db = _db()
    c, d = db.query(models.Client).one(), db.query(models.Deal).one()
    assert (d.client_id, d.title, d.amount, d.currency, d.stage) == (c.id, "Website", 2500, "ZMW", "won")
    assert deal["client_name"] == "Bel Baby Shop"
    db.close()


def test_create_deal_twice_is_one_deal_and_needs_a_client_on_file():
    _run("create_client", {"name": "Bel Baby Shop"})
    for _ in range(2):
        _run("create_deal", {"client": "Bel Baby Shop", "title": "Website", "amount": 2500})
    db = _db()
    assert db.query(models.Deal).count() == 1
    db.close()
    assert "error" in _run("create_deal", {"client": "Nobody Ltd", "title": "Website", "amount": 100})
    assert "error" in _run("create_deal", {"client": "", "title": "Website"})


# ── 4. record_payment ─────────────────────────────────────────

def test_record_payment_paid_writes_revenue_and_links_the_only_deal():
    _run("create_client", {"name": "Bel Baby Shop"})
    _run("create_deal", {"client": "Bel Baby Shop", "title": "Website", "amount": 2500, "stage": "won"})
    paid = _run("record_payment", {"client": "Bel Baby Shop", "amount": 1250, "status": "paid", "title": "Deposit"})
    db = _db()
    p, rev = db.query(models.Payment).one(), db.query(models.Revenue).one()
    assert (p.status, p.amount, p.currency, p.paid_at, p.revenue_id) == ("paid", 1250, "ZMW", p.paid_at, rev.id)
    assert p.paid_at is not None and p.deal_id == db.query(models.Deal).one().id
    assert (rev.amount, rev.currency) == (1250, "ZMW") and paid["status"] == "paid"
    db.close()


def test_record_payment_due_on_a_trigger_has_no_date_and_a_note():
    _run("create_client", {"name": "Bel Baby Shop"})
    due = _run("record_payment", {"client": "Bel Baby Shop", "amount": 1250, "status": "due",
                                  "due_trigger": "on go-live"})
    db = _db()
    p = db.query(models.Payment).one()
    assert (p.status, p.due_date, p.due_trigger, p.notes) == ("due", None, "go-live", "Due on go-live")
    assert db.query(models.Revenue).count() == 0
    assert due["due_trigger"] == "go-live" and due["due_date"] is None
    db.close()


def test_record_payment_takes_a_milestone_given_as_the_due_date():
    """The model may put "on go-live" in due_date: still no invented date."""
    _run("create_client", {"name": "Bel Baby Shop"})
    _run("record_payment", {"client": "Bel Baby Shop", "amount": 1250, "status": "due", "due_date": "on go-live"})
    db = _db()
    p = db.query(models.Payment).one()
    assert p.due_date is None and p.due_trigger == "go-live"
    db.close()


def test_record_payment_due_with_a_date_keeps_the_date():
    _run("create_client", {"name": "Bel Baby Shop"})
    when = TODAY + timedelta(days=10)
    _run("record_payment", {"client": "Bel Baby Shop", "amount": 300, "status": "due", "due_date": when.isoformat()})
    db = _db()
    p = db.query(models.Payment).one()
    assert p.due_date == when and p.due_trigger is None and p.notes is None
    db.close()


def test_a_paid_amount_never_settles_a_due_payment_recorded_in_the_same_turn():
    """"K1,250 due on go-live, K1,250 deposit paid" in either order is two payments."""
    import change_log
    _run("create_client", {"name": "Bel Baby Shop"})
    db = _db()
    change_log.begin_batch(db, "assistant")
    ot.run_tool("record_payment", {"client": "Bel Baby Shop", "amount": 1250, "status": "due",
                                   "due_trigger": "go-live"}, db)
    ot.run_tool("record_payment", {"client": "Bel Baby Shop", "amount": 1250, "status": "paid"}, db)
    change_log.end_batch(db)
    assert sorted(p.status for p in db.query(models.Payment).all()) == ["due", "paid"]
    db.close()

    # A later turn's payment of the same amount does settle the one that was waiting.
    later = _run("record_payment", {"client": "Bel Baby Shop", "amount": 1250, "status": "paid"})
    assert later["settled_existing"] is True
    db = _db()
    assert [p.status for p in db.query(models.Payment).all()] == ["paid", "paid"]
    db.close()


# ── 5. add_recurring ──────────────────────────────────────────

def test_add_recurring_with_a_start_date_runs_from_that_date():
    _run("create_client", {"name": "Bel Baby Shop"})
    start = TODAY + timedelta(days=5)
    rec = _run("add_recurring", {"client": "Bel Baby Shop", "amount": 450, "start": start.isoformat()})
    assert rec["status"] == "active" and rec["interval"] == "monthly"
    db = _db()
    r = db.query(models.RecurringRevenue).one()
    assert (r.active, r.next_due, r.start_trigger, r.currency) == (True, start, None, "ZMW")
    db.close()


def test_add_recurring_with_a_trigger_stays_pending_and_out_of_the_totals():
    _run("create_client", {"name": "Bel Baby Shop"})
    rec = _run("add_recurring", {"client": "Bel Baby Shop", "amount": 450, "frequency": "monthly",
                                 "start_trigger": "from go-live"})
    assert rec["status"] == "pending" and rec["next_due"] is None and rec["start_trigger"] == "go-live"
    db = _db()
    r = db.query(models.RecurringRevenue).one()
    assert r.active is False and r.start_trigger == "go-live"
    assert crm.generate_due(db, TODAY + timedelta(days=400)) == 0      # nothing is ever generated
    assert db.query(models.Payment).count() == 0
    assert crm.summary(db)["mrr"]["by_currency"] == {}
    assert crm.client_detail(db, db.query(models.Client).one())["recurring"][0]["status"] == "pending"
    db.close()


def test_add_recurring_rejects_a_bad_amount_and_an_unknown_client():
    _run("create_client", {"name": "Bel Baby Shop"})
    assert "error" in _run("add_recurring", {"client": "Bel Baby Shop", "amount": 0})
    assert "error" in _run("add_recurring", {"client": "Bel Baby Shop", "amount": 450, "frequency": "weekly"})
    assert "error" in _run("add_recurring", {"client": "Nobody Ltd", "amount": 450})


# ── 6. mark_milestone ─────────────────────────────────────────

@pytest.mark.parametrize("said", ["go-live", "went live", "Go Live", "launch", "the site is live"])
def test_milestone_words_for_going_live_are_one_milestone(said):
    assert crm.milestone_key(said) == "go-live"


def test_milestone_key_keeps_other_events_apart():
    assert crm.milestone_key("on delivery") == "delivery"
    assert crm.milestone_key("after final sign-off") == "final-sign-off"
    assert crm.milestone_key("") is None


def test_mark_milestone_dates_the_payment_and_starts_the_recurring():
    _bel()
    result = _run("mark_milestone", {"client": "Bel", "milestone": "went live", "date": "today"})
    assert len(result["payments_now_due"]) == 1 and len(result["recurring_started"]) == 1
    db = _db()
    balance = db.query(models.Payment).filter(models.Payment.title == "Balance").one()
    assert (balance.status, balance.due_date, balance.due_trigger) == ("due", TODAY, None)
    r = db.query(models.RecurringRevenue).one()
    month_on = crm._add_months(TODAY, 1)
    assert (r.active, r.next_due, r.start_trigger, r.day_of_month) == (True, month_on, None, TODAY.day)
    assert crm.summary(db)["mrr"]["by_currency"] == {"ZMW": 450.0}
    assert result["recurring_started"][0]["next_due"] == month_on
    db.close()


def test_first_recurring_payment_is_due_one_month_after_the_milestone():
    _bel()
    _run("mark_milestone", {"client": "Bel", "milestone": "go-live"})
    db = _db()
    r = db.query(models.RecurringRevenue).one()
    month_on = crm._add_months(TODAY, 1)
    # Nothing recurring is due on go-live day, or on any day before the month is up.
    assert crm.generate_due(db, TODAY) == 0
    assert crm.generate_due(db, month_on - timedelta(days=1)) == 0
    assert db.query(models.Payment).filter(models.Payment.recurring_id == r.id).count() == 0
    # One month after go-live the first K450 is due, once; the second a month after that.
    assert crm.generate_due(db, month_on) == 1 and crm.generate_due(db, month_on) == 0
    first = db.query(models.Payment).filter(models.Payment.recurring_id == r.id).one()
    assert (first.amount, first.due_date) == (450, month_on)
    assert r.next_due == crm._add_months(TODAY, 2)
    db.close()


@pytest.mark.parametrize("went_live, first, second", [
    (date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)),   # short month, then back to the 31st
    (date(2026, 12, 15), date(2027, 1, 15), date(2027, 2, 15)),  # over the year end
])
def test_first_recurring_payment_keeps_the_milestone_day_of_month(went_live, first, second):
    _bel()
    _run("mark_milestone", {"client": "Bel", "milestone": "go-live", "date": went_live.isoformat()})
    db = _db()
    r = db.query(models.RecurringRevenue).one()
    assert (r.next_due, r.day_of_month) == (first, went_live.day)
    assert crm.generate_due(db, first) == 1
    assert r.next_due == second
    db.close()


def test_a_quarterly_amount_is_first_due_one_quarter_after_the_milestone():
    _run("create_client", {"name": "Hope Clinic"})
    _run("add_recurring", {"client": "Hope Clinic", "amount": 900, "frequency": "quarterly",
                           "start_trigger": "go-live"})
    _run("mark_milestone", {"client": "Hope Clinic", "milestone": "go-live"})
    db = _db()
    assert db.query(models.RecurringRevenue).one().next_due == crm._add_months(TODAY, 3)
    db.close()


def test_mark_milestone_takes_a_past_date():
    _bel()
    day = TODAY - timedelta(days=3)
    _run("mark_milestone", {"client": "Bel Baby Shop", "milestone": "go-live", "date": day.isoformat()})
    db = _db()
    balance = db.query(models.Payment).filter(models.Payment.title == "Balance").one()
    assert balance.due_date == day and balance.status == "overdue"
    assert db.query(models.RecurringRevenue).one().next_due == crm._add_months(day, 1)
    db.close()


def test_mark_milestone_leaves_other_clients_and_other_triggers_waiting():
    _bel()
    _run("create_client", {"name": "Hope Clinic"})
    _run("record_payment", {"client": "Hope Clinic", "amount": 900, "status": "due", "due_trigger": "go-live"})
    _run("record_payment", {"client": "Bel Baby Shop", "amount": 200, "status": "due", "due_trigger": "on delivery"})
    _run("mark_milestone", {"client": "Bel Baby Shop", "milestone": "go-live"})
    db = _db()
    waiting = {(p.amount, p.due_trigger) for p in db.query(models.Payment).filter(
        models.Payment.due_trigger.isnot(None)).all()}
    assert waiting == {(900.0, "go-live"), (200.0, "delivery")}
    db.close()


def test_mark_milestone_with_nothing_waiting_writes_nothing_and_says_what_is():
    _bel()
    result = _run("mark_milestone", {"client": "Bel Baby Shop", "milestone": "delivery"})
    assert "error" in result and result["waiting_for"] == ["go-live"]
    again = _run("mark_milestone", {"client": "Bel Baby Shop", "milestone": "go-live"})
    assert "error" not in again
    twice = _run("mark_milestone", {"client": "Bel Baby Shop", "milestone": "go-live"})
    assert "error" in twice and twice["waiting_for"] == []
    assert "error" in _run("mark_milestone", {"client": "Nobody Ltd", "milestone": "go-live"})


def test_undoing_the_milestone_puts_the_money_back_to_waiting():
    _bel()
    events = _orchestrate("Bel went live today", [
        [{"tool_calls": [{"index": 0, "id": "c0", "name": "mark_milestone",
                          "arguments": json.dumps({"client": "Bel Baby Shop", "milestone": "go-live"})}]},
         {"finish_reason": "tool_calls"}],
        [{"content": "Done."}, {"finish_reason": "stop"}],
    ])
    final = next(e for e in events if e.get("type") == "final")
    batch = {c["batch_id"] for c in final["changes"]}
    assert len(batch) == 1
    assert client.post(f"/changes/batch/{batch.pop()}/undo").status_code == 200
    db = _db()
    balance = db.query(models.Payment).filter(models.Payment.title == "Balance").one()
    assert (balance.due_date, balance.due_trigger) == (None, "go-live")
    r = db.query(models.RecurringRevenue).one()
    assert r.active is False and r.start_trigger == "go-live" and crm.recurring_pending(r)
    db.close()


# ── 7. the live message: several facts, one turn, one batch, one Undo ──

def _live_rounds():
    calls = [{"index": i, "id": f"call_{i}", "name": name, "arguments": json.dumps(args)}
             for i, (name, args) in enumerate(LIVE_CALLS)]
    return [[{"tool_calls": calls}, {"finish_reason": "tool_calls"}],
            [{"content": "Bel Baby Shop is on file."}, {"finish_reason": "stop"}]]


def test_the_live_message_writes_everything_in_one_batch():
    events = _orchestrate(LIVE_MESSAGE, _live_rounds())
    final = next(e for e in events if e.get("type") == "final")
    assert len({c["batch_id"] for c in final["changes"]}) == 1

    db = _db()
    counts = _counts(db)
    assert counts["leads"] == 0
    assert (counts["clients"], counts["contacts"], counts["deals"]) == (1, 1, 1)
    assert (counts["payments"], counts["recurring_revenue"], counts["revenue"]) == (2, 1, 1)

    c = db.query(models.Client).one()
    assert (c.name, c.status, c.currency) == ("Bel Baby Shop", "active", "ZMW")
    assert db.query(models.Contact).one().name == "Christabel Sintema"
    deal = db.query(models.Deal).one()
    assert (deal.client_id, deal.amount, deal.currency, deal.stage) == (c.id, 2500, "ZMW", "won")

    paid = db.query(models.Payment).filter(models.Payment.status == "paid").one()
    assert (paid.amount, paid.title, paid.deal_id) == (1250, "Deposit", deal.id)
    assert db.query(models.Revenue).one().amount == 1250
    due = db.query(models.Payment).filter(models.Payment.status == "due").one()
    assert (due.amount, due.due_date, due.due_trigger, due.notes) == (1250, None, "go-live", "Due on go-live")

    rec = db.query(models.RecurringRevenue).one()
    assert (rec.amount, rec.interval, rec.active, rec.start_trigger) == (450, "monthly", False, "go-live")
    assert crm.serialize_recurring(db, rec)["next_due"] is None
    db.close()


def test_one_undo_removes_everything_the_live_message_wrote():
    events = _orchestrate(LIVE_MESSAGE, _live_rounds())
    final = next(e for e in events if e.get("type") == "final")
    batch_id = final["changes"][0]["batch_id"]
    assert client.post(f"/changes/batch/{batch_id}/undo").status_code == 200
    db = _db()
    assert all(n == 0 for n in _counts(db).values()), _counts(db)
    assert db.query(models.Project).count() == 0
    db.close()


# ── 8. prompt and startup wiring ──────────────────────────────

def test_the_prompt_sends_clients_down_the_client_path():
    text = (Path(__file__).parent.parent.parent / "context" / "core" / "orchestrator_prompt.md").read_text(
        encoding="utf-8")
    for needle in ("create_client", "create_deal", "add_recurring", "mark_milestone", "due_trigger",
                   "start_trigger", "unsure true", "agreed to nothing", "Never invent a"):
        assert needle in " ".join(text.split()), needle


def test_m025_is_wired_into_startup_and_re_runnable():
    from migrations import m025_payment_triggers as m025
    m025.run()
    m025.run()
    source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    assert "m025_payment_triggers" in source
    assert source.index("m025_payment_triggers") < source.index("_validate_schema(engine)   # raises")
