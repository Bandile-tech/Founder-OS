"""
Phase 5C — CRM acceptance tests.

  1. migration backfill: one paid payment per existing revenue row with a client, on
     repeated runs; pre-existing clients become USD
  6. update_deal(stage=won) creates a project linked both ways, once
  7. log_communication resolves the client, bumps last_activity_at, writes a row; an
     ambiguous name returns candidates and writes nothing

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

def test_m016_backfills_payments_once_and_marks_old_clients_usd():
    from sqlalchemy import text
    from migrations import m016_crm as m016
    from migrations._helpers import dialect

    db = _db()
    old = _client(db, "Old Client")
    db.add(models.Revenue(amount=150, currency="USD", source="Old Client", client_id=old.id,
                          date=TODAY - timedelta(days=40)))
    db.add(models.Revenue(amount=900, currency="ZMW", source="Walk-in", client_id=None, date=TODAY))
    db.commit()
    old_id = old.id
    db.close()
    with engine.connect() as conn:
        conn.execute(text("UPDATE clients SET currency = NULL WHERE id = :i"), {"i": old_id})
        conn.commit()   # the state of a client that existed before the column

    for expected in ((1, 1), (0, 0)):           # two consecutive runs
        with engine.connect() as conn:
            with conn.begin():
                d = dialect(conn, "test")
                assert (m016._backfill_client_currency(conn, d), m016._backfill_payments(conn, d)) == expected

    db = _db()
    assert db.query(models.Client).filter(models.Client.id == old_id).one().currency == "USD"
    payments = db.query(models.Payment).all()
    assert len(payments) == 1
    p = payments[0]
    assert (p.client_id, p.title, p.status, p.amount, p.currency) == (old_id, "Imported", "paid", 150, "USD")
    assert p.paid_at == TODAY - timedelta(days=40) and p.revenue_id is not None
    fresh = _client(db, "New Client")
    assert fresh.currency == "ZMW"              # new clients are in Kwacha
    db.close()


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
