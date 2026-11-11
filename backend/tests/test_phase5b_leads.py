"""
Phase 5B — prospecting and outreach acceptance tests.

  1. CSV import: counts, phone normalisation, dedupe, unmapped columns kept, re-import
  2. add_lead twice with the same email is one row, merged

No network and no OpenAI.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import leads
import llm
import models
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client

FIXTURE = Path(__file__).parent / "fixtures" / "leads_sample.csv"


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


def _import(**form):
    with open(FIXTURE, "rb") as fh:
        return client.post("/leads/import", files={"file": ("leads.csv", fh, "text/csv")}, data=form)


# ── 1. CSV import ─────────────────────────────────────────────

def test_import_counts_normalisation_dedupe_and_reimport():
    resp = _import()
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert (out["created"], out["updated"], out["skipped"], out["total_rows"]) == (5, 2, 1, 8)
    assert out["columns_mapped"] == {
        "Full Name": "name", "BUSINESS": "company", "Role": "role", "E-mail": "email",
        "Mobile": "phone", "WhatsApp": "whatsapp", "City": "location", "Industry": "segment",
        "Comments": "notes",
    }
    assert out["batch_id"]

    db = _db()
    assert db.query(models.Lead).count() == 5
    mercy = db.query(models.Lead).filter(models.Lead.email == "mercy@sunrisepharmacy.example").one()
    assert mercy.phone == "+260977123456" and mercy.whatsapp == "+260977123456"
    assert mercy.company == "Sunrise Pharmacy Ltd"            # the repeat row's value won
    assert mercy.location == "Lusaka"                          # a blank never overwrote data
    assert mercy.segment == "pharmacies"
    assert mercy.source == f"csv:{out['import_id']}"
    assert "Source: Referral" in mercy.notes and "Source: Directory" in mercy.notes
    assert "Met at the trade fair" in mercy.notes and "second branch" in mercy.notes

    chanda = db.query(models.Lead).filter(models.Lead.phone == "0977555010").one()   # no country code added
    assert chanda.company == "Mwila Salon" and chanda.name == "C. Mwila"
    joseph = db.query(models.Lead).filter(models.Lead.name == "Joseph Phiri").one()
    assert joseph.phone == "0966200300" and joseph.whatsapp == "0966200301"
    courier = db.query(models.Lead).filter(models.Lead.company == "Kabwe Couriers").one()
    assert courier.name == "Kabwe Couriers"                   # a company alone is enough
    record = db.query(models.LeadImport).one()
    assert (record.created, record.updated, record.skipped, record.total_rows) == (5, 2, 1, 8)
    db.close()

    changes = client.get("/changes", params={"batch_id": out["batch_id"], "limit": 50}).json()
    assert sum(1 for c in changes if c["table"] == "leads" and c["action"] == "create") == 5

    again = _import().json()
    assert (again["created"], again["updated"], again["skipped"]) == (0, 7, 1)
    db = _db()
    assert db.query(models.Lead).count() == 5
    db.close()


def test_import_segment_argument_and_mapping_override():
    out = _import(segment="Copperbelt SMEs", mapping=json.dumps({"Industry": "notes", "Source": "role"})).json()
    assert out["segment"] == "copperbelt smes"
    assert out["columns_mapped"]["Industry"] == "notes" and out["columns_mapped"]["Source"] == "role"
    db = _db()
    ruth = db.query(models.Lead).filter(models.Lead.name == "Ruth Zulu").one()
    assert ruth.segment == "copperbelt smes"         # rows without a mapped segment get the argument
    assert "Schools" in ruth.notes and ruth.role == "Head teacher"   # the first mapped column wins
    assert "Source: Referral" in ruth.notes                           # and the second is not lost
    db.close()
    assert _import(mapping="not json").status_code == 400


def test_list_filter_segments_detail_and_patch():
    _import()
    all_leads = client.get("/leads").json()
    assert len(all_leads) == 5
    assert {l["status"] for l in all_leads} == {"new"}
    assert all_leads[0]["open_follow_up"] is None and all_leads[0]["last_message"] is None

    assert [l["name"] for l in client.get("/leads", params={"segment": "Schools"}).json()] == ["Ruth Zulu"]
    assert [l["company"] for l in client.get("/leads", params={"q": "golden"}).json()] == ["Golden Crust Bakery"]

    segs = {s["segment"]: s for s in client.get("/leads/segments").json()}
    assert segs["pharmacies"] == {"segment": "pharmacies", "total": 1, "by_status": {"new": 1}}

    ruth = client.get("/leads", params={"q": "ruth"}).json()[0]
    patched = client.patch(f"/leads/{ruth['id']}", json={"status": "qualified", "phone": "0971 000 111"})
    assert patched.status_code == 200
    body = patched.json()
    assert body["status"] == "qualified" and body["phone"] == "0971000111"
    assert body["change"]["table"] == "leads"
    assert client.patch(f"/leads/{ruth['id']}", json={"status": "nonsense"}).status_code == 400

    detail = client.get(f"/leads/{ruth['id']}").json()
    assert detail["messages"] == [] and detail["follow_ups"] == []
    assert client.get("/leads/99999").status_code == 404

    undo = client.post(f"/changes/{body['change']['id']}/undo")
    assert undo.status_code == 200
    assert client.get(f"/leads/{ruth['id']}").json()["status"] == "new"


# ── 2. add_lead dedupes ───────────────────────────────────────

def test_add_lead_twice_with_the_same_email_is_one_merged_row():
    db = _db()
    first, _ = ot.run_tool("add_lead", {"name": "Mercy Banda", "email": "Mercy@Sunrise.example",
                                        "segment": "Lusaka Pharmacies"}, db)
    assert first["deduped"] is False and first["segment"] == "lusaka pharmacies"
    second, summary = ot.run_tool("add_lead", {"name": "Mercy Banda", "email": "mercy@sunrise.example",
                                               "company": "Sunrise Pharmacy", "phone": "+260 977 000 111",
                                               "notes": "Wants a stock alert bot"}, db)
    assert second["deduped"] is True and second["id"] == first["id"]
    assert "updated lead" in summary
    assert db.query(models.Lead).count() == 1
    lead = db.query(models.Lead).one()
    assert lead.company == "Sunrise Pharmacy" and lead.phone == "+260977000111"
    assert lead.whatsapp == "+260977000111" and lead.segment == "lusaka pharmacies"
    assert lead.source == "conversation" and "stock alert bot" in lead.notes
    db.close()

    resp = client.post("/leads", json={"name": "Mercy B", "email": "mercy@sunrise.example"})
    assert resp.status_code == 200 and resp.json()["deduped"] is True
    fresh = client.post("/leads", json={"name": "Peter Lungu", "company": "Lungu Motors"})
    assert fresh.status_code == 201 and fresh.json()["deduped"] is False
    assert fresh.json()["source"] == "manual"


def test_dedupe_keys_fall_back_from_email_to_number_to_name():
    db = _db()
    a, _, _ = leads.create_lead(db, {"name": "Chanda Mwila", "phone": "0977 555 010"})
    b, deduped_b, _ = leads.create_lead(db, {"name": "Someone else", "whatsapp": "0977-555-010"})
    assert deduped_b is True and b.id == a.id
    c, _, _ = leads.create_lead(db, {"name": "Ruth Zulu", "company": "Hope Academy"})
    d, deduped_d, _ = leads.create_lead(db, {"name": "ruth zulu", "company": "HOPE ACADEMY", "role": "Head"})
    assert deduped_d is True and d.id == c.id and d.role == "Head"
    e, deduped_e, _ = leads.create_lead(db, {"name": "Ruth Zulu", "company": "Another School"})
    assert deduped_e is False and e.id != c.id
    with pytest.raises(ValueError):
        leads.create_lead(db, {"email": "nobody@example.com"})
    db.commit()
    db.close()


def test_find_leads_tool_and_deletable_tables():
    import actions
    db = _db()
    ot.run_tool("add_lead", {"name": "Mercy Banda", "segment": "lusaka pharmacies"}, db)
    ot.run_tool("add_lead", {"name": "Joseph Phiri", "segment": "kitwe bakeries"}, db)
    result, summary = ot.run_tool("find_leads", {"segment": "Lusaka Pharmacies"}, db)
    assert result["count"] == 1 and result["leads"][0]["name"] == "Mercy Banda"
    assert summary == "1 lead(s) found"
    assert "leads" in actions.DELETABLE and "outreach_messages" in actions.DELETABLE
    pending, _ = ot.run_tool("propose_delete", {"table": "leads", "record_id": result["leads"][0]["id"]}, db)
    assert pending["requires_confirmation"] is True
    assert db.query(models.Lead).count() == 2
    db.close()
