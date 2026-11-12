"""
Phase 5C — CRM acceptance tests.

  1. migration backfill: one paid payment per existing revenue row with a client, on
     repeated runs; pre-existing clients get DEFAULT_CURRENCY (ZMW), never USD
  6. update_deal(stage=won) creates a project linked both ways, once
  7. log_communication resolves the client, bumps last_activity_at, writes a row; an
     ambiguous name returns candidates and writes nothing
  2. a paid payment creates its revenue row; undo removes both
  3. the Paid tap links revenue and is idempotent
  4. recurring revenue generates each period once
  5. overdue flags and client health
  8. reconciliation of stray revenue rows
  9. briefing items and the overdue alert
 10. GET /crm/summary against a hand-built fixture

No network and no OpenAI.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import crm
import llm
import models
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client, engine

TODAY = date.today()


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


def _client(db, name="Wamu's Bakes & Cakes", **kw):
    kw.setdefault("status", "active")
    c = models.Client(name=name, **kw)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ── 1. migration backfill ─────────────────────────────────────

def _null_currency(client_id):
    """The state of a client that existed before the column."""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("UPDATE clients SET currency = NULL WHERE id = :i"), {"i": client_id})
        conn.commit()


def test_m016_gives_old_clients_the_default_currency_and_payments_keep_their_own(monkeypatch):
    from migrations import m016_crm as m016
    from migrations._helpers import dialect

    monkeypatch.delenv("DEFAULT_CURRENCY", raising=False)
    db = _db()
    old = _client(db, "Old Client")
    db.add(models.Revenue(amount=150, currency="USD", source="Old Client", client_id=old.id,
                          date=TODAY - timedelta(days=40)))
    db.add(models.Revenue(amount=2500, currency="ZMW", source="Old Client", client_id=old.id,
                          date=TODAY - timedelta(days=10)))
    db.add(models.Revenue(amount=900, currency="ZMW", source="Walk-in", client_id=None, date=TODAY))
    db.commit()
    old_id = old.id
    db.close()
    _null_currency(old_id)

    for expected in ((1, 2), (0, 0)):           # two consecutive runs
        with engine.connect() as conn:
            with conn.begin():
                d = dialect(conn, "test")
                assert (m016._backfill_client_currency(conn, d), m016._backfill_payments(conn, d)) == expected

    db = _db()
    # Bandile works in Kwacha: a client that predates the column is not turned into a dollar client.
    assert db.query(models.Client).filter(models.Client.id == old_id).one().currency == "ZMW"
    payments = db.query(models.Payment).order_by(models.Payment.amount).all()
    # Each payment keeps the currency of the revenue row it came from, whatever the client's is.
    assert [(p.client_id, p.title, p.status, p.amount, p.currency) for p in payments] == [
        (old_id, "Imported", "paid", 150, "USD"), (old_id, "Imported", "paid", 2500, "ZMW")]
    assert payments[0].paid_at == TODAY - timedelta(days=40) and payments[0].revenue_id is not None
    fresh = _client(db, "New Client")
    assert fresh.currency == "ZMW"              # new clients are in Kwacha
    db.close()


def test_default_currency_env_var_sets_the_client_default(monkeypatch):
    from migrations import m016_crm as m016
    from migrations._helpers import dialect

    monkeypatch.setenv("DEFAULT_CURRENCY", "usd")
    db = _db()
    old = _client(db, "Dollar Client")
    old_id = old.id
    assert old.currency == "USD"                # the ORM default follows the variable
    db.close()
    _null_currency(old_id)
    with engine.connect() as conn:
        with conn.begin():
            assert m016._backfill_client_currency(conn, dialect(conn, "test")) == 1
    db = _db()
    assert db.query(models.Client).filter(models.Client.id == old_id).one().currency == "USD"
    db.close()
    assert client.get(f"/clients/{old_id}").json()["currency"] == "USD"

    monkeypatch.setenv("DEFAULT_CURRENCY", "bitcoin")      # not a currency the app knows
    assert models.default_currency() == "ZMW"
    monkeypatch.delenv("DEFAULT_CURRENCY")
    assert models.default_currency() == "ZMW"


def test_a_client_with_no_currency_reads_as_the_default_not_usd(monkeypatch):
    monkeypatch.delenv("DEFAULT_CURRENCY", raising=False)
    db = _db()
    c = _client(db, "No Currency Ltd")
    cid = c.id
    db.close()
    _null_currency(cid)
    assert client.get(f"/clients/{cid}").json()["currency"] == "ZMW"
    made = client.post("/deals", json={"client_id": cid, "title": "Site", "amount": 100}).json()
    assert made["currency"] == "ZMW"


def test_m016_payment_backfill_runs_once_so_it_never_preempts_reconcile():
    """Review item 7: the backfill ran on every startup. A revenue row logged after the
    migration got an 'Imported' paid payment at the next restart, so crm.reconcile never
    matched it to the invoice it paid and the invoice stayed open."""
    from migrations import m016_crm as m016
    from migrations._helpers import dialect

    def startup_backfill():
        with engine.connect() as conn:
            with conn.begin():
                return m016._backfill_payments_once(conn, dialect(conn, "test"))

    db = _db()
    c = _client(db, "Hope Clinic")
    cid = c.id
    db.add(models.Revenue(amount=150, currency="USD", source="Hope Clinic", client_id=cid,
                          date=TODAY - timedelta(days=40)))
    db.commit()
    assert startup_backfill() == 1                      # the migration: history becomes payments
    assert startup_backfill() == 0

    # Afterwards: an invoice, then the money logged in conversation as plain revenue.
    invoice = crm.record_payment(db, c, 500, "ZMW", status="due", title="November retainer")
    db.add(models.Revenue(amount=500, currency="ZMW", source="Hope Clinic", client_id=cid, date=TODAY))
    db.commit()
    db.close()

    assert startup_backfill() == 0                      # a restart adds nothing

    db = _db()
    assert crm.reconcile(db) == 1                       # reconciliation settles the invoice
    db.commit()
    payments = db.query(models.Payment).order_by(models.Payment.id).all()
    assert [(p.title, p.status, p.amount) for p in payments] == [
        ("Imported", "paid", 150), ("November retainer", "paid", 500)]
    assert payments[1].id == invoice["id"] and payments[1].revenue_id is not None
    mark = db.query(models.MigrationMark).one()
    assert mark.key == "m016_payments_backfill" and mark.applied_at is not None
    db.close()


def test_m016_run_is_safe_to_repeat_on_the_app_database():
    from migrations import m016_crm as m016

    m016.run()
    m016.run()


# ── 6. winning a deal creates its project once ────────────────

def test_won_deal_creates_one_project_linked_both_ways():
    db = _db()
    c = _client(db, "Hope Clinic")
    made = client.post("/deals", json={"client_id": c.id, "title": "Booking portal", "amount": 8000})
    assert made.status_code == 201
    deal = made.json()
    assert (deal["stage"], deal["probability"], deal["currency"]) == ("lead", 10, "ZMW")
    assert deal["client_name"] == "Hope Clinic" and deal["change"]["table"] == "deals"

    moved = client.patch(f"/deals/{deal['id']}", json={"stage": "proposal"}).json()
    assert (moved["stage"], moved["probability"]) == ("proposal", 40)
    assert "project" not in moved

    won = client.patch(f"/deals/{deal['id']}", json={"stage": "won"}).json()
    assert won["stage"] == "won" and won["probability"] == 100 and won["won_at"]
    project = won["project"]
    assert project["title"] == "Booking portal" and project["kind"] == "client"
    assert project["deal_id"] == deal["id"] and project["client_id"] == c.id
    assert project["expected_value"] == 8000 and project["status"] == "active"
    assert won["project_id"] == project["id"]
    assert {ch["table"] for ch in won["changes"]} == {"deals", "projects"}

    again = client.patch(f"/deals/{deal['id']}", json={"stage": "won", "amount": 8500}).json()
    assert "project" not in again and again["amount"] == 8500
    assert db.query(models.Project).count() == 1

    assert client.patch(f"/deals/{deal['id']}", json={"stage": "maybe"}).status_code == 400
    assert client.patch("/deals/9999", json={"stage": "won"}).status_code == 404
    assert [d["id"] for d in client.get("/deals", params={"stage": "won"}).json()] == [deal["id"]]
    db.close()


def test_update_deal_tool_updates_creates_and_never_guesses():
    db = _db()
    _client(db, "Hope Clinic")
    _client(db, "Hope Academy")

    created, summary = ot.run_tool("update_deal", {"deal": "Booking portal", "client": "Hope Clinic",
                                                   "amount": 8000, "create": True}, db)
    assert created["stage"] == "lead" and "created" in summary
    missing, _ = ot.run_tool("update_deal", {"deal": "School portal", "client": "Hope Academy"}, db)
    assert "error" in missing and db.query(models.Deal).count() == 1     # no create flag, no write

    moved, summary = ot.run_tool("update_deal", {"deal": "booking portal", "stage": "negotiation",
                                                 "expected_close": (TODAY + timedelta(days=20)).isoformat()}, db)
    assert moved["stage"] == "negotiation" and moved["probability"] == 70
    assert moved["expected_close"] == TODAY + timedelta(days=20)

    won, summary = ot.run_tool("update_deal", {"client": "Hope Clinic", "stage": "won"}, db)   # the client's one open deal
    assert won["stage"] == "won" and "project 'Booking portal' created" in summary

    vague, _ = ot.run_tool("update_deal", {"deal": "Portal", "client": "Hope", "create": True}, db)
    assert vague["ambiguous"] is True and len(vague["candidates"]) == 2    # two clients called Hope
    orphan, _ = ot.run_tool("update_deal", {"deal": "New thing", "create": True}, db)
    assert orphan["error"] == "a new deal needs a client that is already on file"
    db.close()


# ── 7. communications ─────────────────────────────────────────

def test_log_communication_resolves_the_client_and_bumps_its_project():
    db = _db()
    c = _client(db)
    stale = datetime.utcnow() - timedelta(days=30)
    active = models.Project(title="Website", client_id=c.id, status="active", last_activity_at=stale)
    paused = models.Project(title="Old site", client_id=c.id, status="paused", last_activity_at=stale)
    db.add_all([active, paused])
    db.commit()

    result, summary = ot.run_tool("log_communication", {"who": "Wamu's", "summary": "Agreed the gallery page",
                                                        "channel": "call"}, db)
    assert result["with"] == "Wamu's Bakes & Cakes" and summary == "logged call with Wamu's Bakes & Cakes"
    assert result["direction"] == "out" and result["source"] == "conversation"
    row = db.query(models.Communication).one()
    assert (row.client_id, row.channel, row.summary) == (c.id, "call", "Agreed the gallery page")
    db.refresh(active)
    db.refresh(paused)
    assert active.last_activity_at > stale + timedelta(days=29)
    assert paused.last_activity_at == stale                      # only active projects move

    dated, _ = ot.run_tool("log_communication", {"who": "Wamu's", "summary": "She asked for a quote",
                                                 "direction": "in", "when": "yesterday"}, db)
    assert dated["direction"] == "in"
    assert dated["occurred_at"].date() == TODAY - timedelta(days=1)

    changes = client.get("/changes").json()
    assert sum(1 for ch in changes if ch["table"] == "communications" and ch["action"] == "create") == 2
    listed = client.get("/communications", params={"client_id": c.id}).json()
    assert [x["summary"] for x in listed] == ["Agreed the gallery page", "She asked for a quote"]
    db.close()


def test_an_ambiguous_name_returns_candidates_and_writes_nothing():
    db = _db()
    _client(db, "Hope Clinic Lusaka")
    _client(db, "Hope Clinic Kitwe")
    result, summary = ot.run_tool("log_communication", {"who": "the clinic", "summary": "Called"}, db)
    assert result["ambiguous"] is True
    assert {cand["name"] for cand in result["candidates"]} == {"Hope Clinic Lusaka", "Hope Clinic Kitwe"}
    assert "ask which" in summary
    assert db.query(models.Communication).count() == 0

    nobody, _ = ot.run_tool("log_communication", {"who": "Quantum Freight", "summary": "Called"}, db)
    assert "error" in nobody and db.query(models.Communication).count() == 0
    db.close()


def test_a_lead_is_used_when_no_client_matches():
    db = _db()
    ot.run_tool("add_lead", {"name": "Mercy Banda", "company": "Sunrise Pharmacy"}, db)
    result, _ = ot.run_tool("log_communication", {"who": "Sunrise Pharmacy", "summary": "Left a voice note"}, db)
    row = db.query(models.Communication).one()
    assert row.lead_id is not None and row.client_id is None and result["with"] == "Mercy Banda"
    db.close()


# ── clients, contacts, health ─────────────────────────────────

def test_clients_list_keeps_its_fields_and_gains_the_crm_ones():
    db = _db()
    c = _client(db, "Hope Clinic", company="Hope Health Ltd", value=300, service="Portal")
    db.add(models.Payment(client_id=c.id, amount=2000, currency="ZMW", status="due", due_date=TODAY + timedelta(days=5)))
    db.add(models.Payment(client_id=c.id, amount=50, currency="USD", status="paid", paid_at=TODAY))
    db.add(models.RecurringRevenue(client_id=c.id, title="Hosting", amount=600, currency="ZMW",
                                   interval="quarterly", next_due=TODAY + timedelta(days=30)))
    db.add(models.Deal(client_id=c.id, title="Portal", stage="proposal"))
    db.add(models.Deal(client_id=c.id, title="Old", stage="lost"))
    db.commit()
    db.close()

    row = client.get("/clients").json()[0]
    for key in ("id", "name", "company", "status", "value", "service", "notes", "created_at"):
        assert key in row                                     # nothing removed
    assert row["currency"] == "ZMW" and row["health"] == "green" and row["tags"] == []
    assert row["open_deals"] == 1 and row["contacts_count"] == 0
    assert row["outstanding"]["by_currency"] == {"ZMW": 2000.0}
    assert row["mrr"]["by_currency"] == {"ZMW": 200.0}       # quarterly 600 → 200 a month
    assert row["last_contact_at"] is None

    assert client.get("/clients", params={"q": "health"}).json()[0]["name"] == "Hope Clinic"
    assert client.get("/clients", params={"status": "paused"}).json() == []

    patched = client.patch(f"/clients/{row['id']}", json={"status": "paused", "currency": "$", "tags": ["retainer", " health "]})
    assert patched.status_code == 200
    body = patched.json()
    assert (body["status"], body["currency"], body["tags"]) == ("paused", "USD", ["retainer", "health"])
    assert body["change"]["table"] == "clients"
    assert client.patch(f"/clients/{row['id']}", json={"status": "vip"}).status_code == 400
    client.post(f"/changes/{body['change']['id']}/undo")
    assert client.get("/clients").json()[0]["status"] == "active"


def test_contacts_and_client_detail():
    db = _db()
    c = _client(db, "Hope Clinic")
    cid = c.id
    db.close()

    first = client.post(f"/clients/{cid}/contacts", json={"name": "Dr Tembo", "phone": "0977 100 200", "is_primary": True})
    assert first.status_code == 201
    assert first.json()["phone"] == "0977100200" and first.json()["whatsapp"] == "0977100200"
    db = _db()
    added, summary = ot.run_tool("add_contact", {"client": "hope clinic", "name": "Mrs Banda", "role": "PA",
                                                 "email": "PA@HopeClinic.example", "is_primary": True}, db)
    assert added["is_primary"] is True and added["email"] == "pa@hopeclinic.example"
    assert summary == "added contact Mrs Banda at Hope Clinic"
    db.close()

    detail = client.get(f"/clients/{cid}").json()
    assert [(x["name"], x["is_primary"]) for x in detail["contacts"]] == [("Mrs Banda", True), ("Dr Tembo", False)]
    for key in ("deals", "projects", "payments", "recurring", "communications"):
        assert detail[key] == []
    assert detail["contacts_count"] == 2

    patched = client.patch(f"/contacts/{first.json()['id']}", json={"role": "Director", "phone": "+260 977 100 200"})
    assert patched.json()["role"] == "Director" and patched.json()["phone"] == "+260977100200"
    assert client.get("/clients/9999").status_code == 404
    assert client.post("/clients/9999/contacts", json={"name": "X"}).status_code == 404

    db = _db()
    looked, _ = ot.run_tool("crm_lookup", {"query": "Hope Clinic"}, db)
    assert looked["kind"] == "client" and len(looked["client"]["contacts"]) == 2
    person, _ = ot.run_tool("crm_lookup", {"query": "Mrs Banda", "kind": "contact"}, db)
    assert person["client_name"] == "Hope Clinic"
    nothing, _ = ot.run_tool("crm_lookup", {"query": "Quantum Freight"}, db)
    assert nothing["found"] is False
    db.close()


def test_client_health_bands():
    db = _db()
    c = _client(db, "Hope Clinic")
    assert crm.client_health(db, c) == "green"

    late = models.Payment(client_id=c.id, amount=500, currency="ZMW", status="due", due_date=TODAY - timedelta(days=1))
    db.add(late)
    db.commit()
    assert crm.client_health(db, c) == "amber"            # one day overdue
    late.due_date = TODAY - timedelta(days=15)
    db.commit()
    assert crm.client_health(db, c) == "red"              # more than fourteen
    late.status = "paid"
    db.commit()
    assert crm.client_health(db, c) == "green"

    blocked = models.Project(title="Portal", client_id=c.id, status="active", blocked_reason="waiting on logo")
    db.add(blocked)
    db.commit()
    assert crm.client_health(db, c) == "red"
    blocked.blocked_reason = None
    c.created_at = datetime.utcnow() - timedelta(days=60)
    db.commit()
    assert crm.client_health(db, c) == "red"              # active and silent for 45 days
    db.add(models.Communication(client_id=c.id, channel="call", direction="out", summary="Checked in",
                                occurred_at=datetime.utcnow() - timedelta(days=22)))
    db.commit()
    assert crm.client_health(db, c) == "amber"            # silent for 21 days
    db.add(models.Communication(client_id=c.id, channel="call", direction="out", summary="Called",
                                occurred_at=datetime.utcnow() - timedelta(days=2)))
    db.commit()
    assert crm.client_health(db, c) == "green"
    db.close()


# ════════════════════════════════════════════════════════════════
# Payments, recurring revenue, reconciliation, summary, briefing
# ════════════════════════════════════════════════════════════════

# ── 2. a paid payment writes its revenue row ──────────────────

def test_record_payment_paid_creates_linked_revenue_and_undo_removes_both(monkeypatch):
    monkeypatch.setenv("ZMW_PER_USD", "25")
    db = _db()
    c = _client(db)
    before_total = client.get("/revenue").json()["total_usd"]

    import change_log
    change_log.begin_batch(db, "assistant")
    result, summary = ot.run_tool("record_payment", {"client": "Wamu's", "amount": 2000, "currency": "K"}, db)
    batch_id = change_log.current_batch_id(db)
    change_log.end_batch(db)

    assert summary == "K2,000 received from Wamu's Bakes & Cakes"
    assert result["status"] == "paid" and result["paid_at"] == TODAY
    payment = db.query(models.Payment).one()
    revenue = db.query(models.Revenue).one()
    assert payment.revenue_id == revenue.id
    assert (revenue.amount, revenue.currency, revenue.client_id) == (2000, "ZMW", c.id)
    assert revenue.source == "Wamu's Bakes & Cakes" and revenue.notes == f"payment #{payment.id}"
    assert revenue.date == TODAY

    after = client.get("/revenue").json()
    assert after["total_usd"] == round(before_total + 2000 / 25, 2)        # 80 dollars at 25
    assert after["by_currency"]["ZMW"] == 2000

    undone = client.post(f"/changes/batch/{batch_id}/undo")
    assert undone.status_code == 200
    db.expire_all()
    assert db.query(models.Payment).count() == 0 and db.query(models.Revenue).count() == 0
    db.close()


def test_record_payment_due_then_paid_settles_the_same_payment():
    db = _db()
    _client(db)
    due, summary = ot.run_tool("record_payment", {"client": "Wamu's", "amount": 2000, "status": "due",
                                                  "due_date": "end of month", "title": "Website invoice"}, db)
    assert due["status"] == "due" and due["title"] == "Website invoice" and "due from" in summary
    assert db.query(models.Revenue).count() == 0

    paid, _ = ot.run_tool("record_payment", {"client": "Wamu's", "amount": 2000}, db)   # "they paid"
    assert paid["settled_existing"] is True and paid["id"] == due["id"] and paid["status"] == "paid"
    assert db.query(models.Payment).count() == 1 and db.query(models.Revenue).count() == 1

    bad, _ = ot.run_tool("record_payment", {"client": "Wamu's", "amount": 0}, db)
    assert "error" in bad
    unknown, _ = ot.run_tool("record_payment", {"client": "Quantum Freight", "amount": 10}, db)
    assert "error" in unknown and db.query(models.Payment).count() == 1
    db.close()


# ── 3. the Paid tap ───────────────────────────────────────────

def test_paid_endpoint_links_revenue_and_is_idempotent():
    db = _db()
    c = _client(db)
    cid = c.id
    db.close()
    made = client.post("/payments", json={"client_id": cid, "amount": 1500, "currency": "ZMW",
                                          "title": "Deposit", "due_date": (TODAY + timedelta(days=3)).isoformat()})
    assert made.status_code == 201
    pid = made.json()["id"]
    assert made.json()["status"] == "due" and made.json()["revenue_id"] is None

    paid = client.post(f"/payments/{pid}/paid").json()
    assert paid["status"] == "paid" and paid["revenue_id"] is not None
    assert {ch["table"] for ch in paid["changes"]} == {"payments", "revenue"}

    again = client.post(f"/payments/{pid}/paid").json()
    assert again["already_paid"] is True and again["revenue_id"] == paid["revenue_id"]
    db = _db()
    assert db.query(models.Revenue).count() == 1
    db.close()

    assert client.post(f"/payments/{pid}/cancel").status_code == 409
    other = client.post("/payments", json={"client_id": cid, "amount": 300}).json()
    cancelled = client.post(f"/payments/{other['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["change"]["table"] == "payments"
    assert client.post(f"/payments/{other['id']}/paid").status_code == 409
    assert client.post("/payments/9999/paid").status_code == 404
    assert client.post("/payments", json={"client_id": cid, "amount": -5}).status_code == 400
    assert client.get("/payments").json() == []                              # nothing open
    assert len(client.get("/payments", params={"status": "paid,cancelled"}).json()) == 2


# ── 4. recurring revenue ──────────────────────────────────────

def test_generate_due_creates_each_period_once_and_advances_next_due():
    db = _db()
    c = _client(db)
    start = TODAY - timedelta(days=45)
    made = client.post("/recurring", json={"client_id": c.id, "title": "Hosting retainer", "amount": 600,
                                           "currency": "ZMW", "interval": "monthly", "start": start.isoformat()})
    assert made.status_code == 201
    rec_id = made.json()["id"]
    periods = [crm._add_months(start, k, start.day) for k in range(4)]
    expected = [p for p in periods if p <= TODAY]
    assert len(expected) == 2                                # the fixture is two periods in arrears

    assert crm.generate_due(db, TODAY) == 2
    db.commit()
    payments = db.query(models.Payment).order_by(models.Payment.period_start).all()
    assert [p.period_start for p in payments] == expected
    assert all((p.status, p.amount, p.recurring_id, p.due_date) == ("due", 600, rec_id, p.period_start) for p in payments)
    assert payments[0].title.startswith("Hosting retainer — ")

    assert crm.generate_due(db, TODAY) == 0                  # a third call creates none
    db.commit()
    assert db.query(models.Payment).count() == 2
    rec = db.query(models.RecurringRevenue).one()
    assert rec.next_due == periods[2] and rec.next_due > TODAY

    listed = client.get("/recurring").json()
    assert listed[0]["monthly_amount"] == 600 and listed[0]["client_name"] == c.name
    stopped = client.patch(f"/recurring/{rec_id}", json={"active": False}).json()
    assert stopped["active"] is False and stopped["change"]["table"] == "recurring_revenue"
    assert crm.generate_due(db, TODAY + timedelta(days=90)) == 0     # inactive
    assert client.patch(f"/recurring/{rec_id}", json={"interval": "weekly"}).status_code == 400
    db.close()


def test_the_unique_index_blocks_a_second_instalment_for_a_period():
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_recurring_period "
            "ON payments (recurring_id, period_start) WHERE recurring_id IS NOT NULL"))
        conn.commit()
    db = _db()
    c = _client(db)
    for _ in range(2):                                       # manual payments never collide
        db.add(models.Payment(client_id=c.id, amount=10, currency="ZMW", status="due"))
    db.add(models.Payment(client_id=c.id, amount=10, currency="ZMW", recurring_id=7, period_start=TODAY))
    db.commit()
    db.add(models.Payment(client_id=c.id, amount=10, currency="ZMW", recurring_id=7, period_start=TODAY))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


# ── 5. overdue ────────────────────────────────────────────────

def test_refresh_statuses_flips_past_due_payments():
    db = _db()
    c = _client(db)
    late = models.Payment(client_id=c.id, amount=500, currency="ZMW", status="due", due_date=TODAY - timedelta(days=1))
    soon = models.Payment(client_id=c.id, amount=500, currency="ZMW", status="due", due_date=TODAY)
    undated = models.Payment(client_id=c.id, amount=500, currency="ZMW", status="due")
    db.add_all([late, soon, undated])
    db.commit()
    assert crm.refresh_statuses(db, TODAY) == 1
    db.commit()
    assert (late.status, soon.status, undated.status) == ("overdue", "due", "due")
    assert crm.client_health(db, c) == "amber"               # one day overdue
    late.due_date = TODAY - timedelta(days=15)
    db.commit()
    assert crm.client_health(db, c) == "red"                 # fifteen days overdue
    late.due_date = TODAY + timedelta(days=2)
    assert crm.refresh_statuses(db, TODAY) == 1              # re-dated: back to due
    assert late.status == "due"
    db.close()


# ── 8. reconciliation ─────────────────────────────────────────

def test_reconcile_links_a_stray_revenue_row_within_five_percent():
    db = _db()
    c = _client(db)
    other = _client(db, "Hope Clinic")
    older = models.Payment(client_id=c.id, amount=2000, currency="ZMW", status="due", due_date=TODAY - timedelta(days=3))
    newer = models.Payment(client_id=c.id, amount=2000, currency="ZMW", status="due", due_date=TODAY + timedelta(days=9))
    dollars = models.Payment(client_id=other.id, amount=100, currency="USD", status="due", due_date=TODAY)
    db.add_all([older, newer, dollars])
    db.add(models.Revenue(amount=1950, currency="ZMW", source="brain dump", client_id=c.id, date=TODAY))    # 2.5 % under
    db.add(models.Revenue(amount=100, currency="ZMW", source="brain dump", client_id=other.id, date=TODAY))  # wrong currency
    db.add(models.Revenue(amount=1700, currency="ZMW", source="brain dump", client_id=c.id, date=TODAY))    # 15 % under
    db.add(models.Revenue(amount=2000, currency="ZMW", source="walk-in", client_id=None, date=TODAY))       # no client
    db.commit()

    assert crm.reconcile(db) == 1
    db.commit()
    assert older.status == "paid" and older.paid_at == TODAY       # the oldest open payment first
    assert older.revenue_id == db.query(models.Revenue).filter(models.Revenue.amount == 1950).one().id
    assert newer.status == "due" and dollars.status == "due"
    assert db.query(models.Revenue).count() == 4                    # linked, not duplicated

    assert crm.reconcile(db) == 0                                   # idempotent
    db.close()


# ── 9. briefing and alert ─────────────────────────────────────

def test_briefing_shows_payments_deals_and_quiet_clients():
    import briefing
    db = _db()
    c = _client(db)
    c.created_at = datetime.utcnow() - timedelta(days=25)           # active and quiet for 25 days
    overdue = models.Payment(client_id=c.id, amount=2000, currency="ZMW", status="due", due_date=TODAY - timedelta(days=5))
    soon = models.Payment(client_id=c.id, amount=50, currency="USD", status="due", due_date=TODAY + timedelta(days=4))
    far = models.Payment(client_id=c.id, amount=900, currency="ZMW", status="due", due_date=TODAY + timedelta(days=30))
    stale = models.Deal(client_id=c.id, title="Loyalty app", stage="negotiation")
    fresh = models.Deal(client_id=c.id, title="Menu refresh", stage="negotiation")
    db.add_all([overdue, soon, far, stale, fresh])
    db.commit()
    stale.updated_at = datetime.utcnow() - timedelta(days=40)
    db.commit()

    base = briefing.build_briefing(_db(), with_summary=False)
    by_id = {i["id"]: i for i in base["items"]}
    item = by_id[f"payment:payment:{overdue.id}"]
    assert item["score"] == 72 + 5 and item["score"] >= 72
    assert item["due_label"] == "5d overdue" and item["suggested_action"] == "Chase it or mark paid"
    assert item["reason"] == "K2,000 from Wamu's Bakes & Cakes overdue"
    expected = by_id[f"payment:payment:{soon.id}"]
    assert expected["score"] == 42 and expected["title"].startswith("$50 from")
    assert f"payment:payment:{far.id}" not in by_id
    deal = by_id[f"deal:deal:{stale.id}"]
    assert deal["score"] == 40 and deal["suggested_action"] == "Close or mark lost"
    assert f"deal:deal:{fresh.id}" not in by_id
    quiet = by_id[f"client:client:{c.id}"]
    assert quiet["score"] == 26 and quiet["reason"] == "No contact in 25 days"

    assert base["counts"]["payments_overdue"] == 1
    assert base["business_detail"]["overdue_count"] == 1
    assert base["business_detail"]["outstanding_usd"] == pytest.approx(2000 / 27 + 50 + 900 / 27, abs=0.02)
    assert isinstance(base["progress"]["business"], (int, type(None)))
    db.refresh(overdue)
    assert overdue.status == "overdue"                              # stored by the housekeeping pass

    # attention_count counts the overdue payment
    overdue.status, overdue.paid_at = "paid", TODAY
    db.commit()
    after = briefing.build_briefing(_db(), with_summary=False)
    assert after["attention_count"] == base["attention_count"] - 1
    assert after["counts"]["payments_overdue"] == 0
    db.close()


def test_payment_overdue_alert_fires_after_fourteen_days():
    db = _db()
    c = _client(db)
    p = models.Payment(client_id=c.id, amount=2000, currency="ZMW", status="due", due_date=TODAY - timedelta(days=14))
    db.add(p)
    db.commit()

    def fired():
        alerts, _ = ot.detect_off_track(db)
        return any(a["rule_id"] == "payment_overdue_14d" for a in alerts)

    assert fired() is False                                         # exactly 14 days: not yet
    p.due_date = TODAY - timedelta(days=15)
    db.commit()
    assert fired() is True
    alert = next(a for a in ot.detect_off_track(db)[0] if a["rule_id"] == "payment_overdue_14d")
    assert alert["severity"] == "critical"
    p.status = "paid"
    db.commit()
    assert fired() is False
    db.close()


# ── 10. summary ───────────────────────────────────────────────

def test_crm_summary_matches_a_hand_built_fixture(monkeypatch):
    monkeypatch.setenv("ZMW_PER_USD", "25")
    db = _db()
    a = _client(db, "Hope Clinic")
    b = _client(db, "Golden Crust Bakery", status="paused")
    db.add_all([
        models.Payment(client_id=a.id, amount=2500, currency="ZMW", status="due", due_date=TODAY + timedelta(days=5)),   # $100 due
        models.Payment(client_id=a.id, amount=1000, currency="ZMW", status="due", due_date=TODAY - timedelta(days=3)),   # $40 overdue
        models.Payment(client_id=b.id, amount=60, currency="USD", status="paid", paid_at=TODAY - timedelta(days=2)),     # $60 paid
        models.RecurringRevenue(client_id=a.id, title="Hosting", amount=750, currency="ZMW", interval="monthly",
                                next_due=TODAY + timedelta(days=10)),                                                  # $30 a month
        models.Deal(client_id=a.id, title="Portal", stage="proposal", amount=5000, currency="ZMW", probability=40),     # $200 × 0.4
        models.Deal(client_id=b.id, title="Site", stage="won", amount=100, currency="USD", probability=100),
        models.Revenue(amount=60, currency="USD", source="Golden Crust Bakery", client_id=b.id, date=TODAY - timedelta(days=2)),
        models.Revenue(amount=500, currency="ZMW", source="old", date=TODAY - timedelta(days=60)),
    ])
    db.commit()
    db.close()

    s = client.get("/crm/summary").json()
    assert s["clients"] == {"active": 1, "paused": 1, "total": 2}
    assert s["deals"]["by_stage"] == {"proposal": {"count": 1, "amount_usd": 200.0},
                                      "won": {"count": 1, "amount_usd": 100.0}}
    assert s["deals"]["weighted_pipeline_usd"] == 80.0
    assert s["payments"] == {"due_usd": 100.0, "overdue_usd": 40.0, "overdue_count": 1, "paid_30d_usd": 60.0}
    assert s["mrr"] == {"by_currency": {"ZMW": 750.0}, "total_usd": 30.0}
    assert s["revenue_30d_usd"] == 60.0
    assert s["health"] == {"green": 1, "amber": 1, "red": 0}        # the clinic has an overdue payment

    db = _db()
    assert db.query(models.Client).filter(models.Client.name == "Hope Clinic").one().health == "amber"   # stored
    result, summary = ot.run_tool("crm_summary", {}, db)
    assert result["payments"]["overdue_count"] == 1 and "1 active client(s)" in summary
    db.close()
