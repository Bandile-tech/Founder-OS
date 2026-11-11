"""
Phase 5B — prospecting and outreach acceptance tests.

  1. CSV import: counts, phone normalisation, dedupe, unmapped columns kept, re-import
  2. add_lead twice with the same email is one row, merged
  3. qualification: score = pre + fit, status rules, the cap of 20, no-key behaviour
  4. drafts and revisions; nothing is ever sent without a later yes or a tap
  5. the Approve & send tap on a WhatsApp draft, its follow-up task and undo
  6. email fails cleanly until the email integration is configured
  7. log_lead_reply: inbound row, lead replied, follow-up done
  8. convert_lead: client linked both ways, follow-ups closed, idempotent
  9. briefing items for waiting drafts and quiet leads; the outreach_stalled alert
 10. GET /outreach/stats reply and conversion rates

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


# ════════════════════════════════════════════════════════════════
# Qualification, drafts, the approval gate, follow-ups
# ════════════════════════════════════════════════════════════════

from datetime import date, timedelta
from unittest.mock import patch

import outreach
from tests.test_stage2_changes import _final_round, _orchestrate, _tool_round

TODAY = date.today()


def _lead(db, name="Mercy Banda", **kw):
    fields = {"name": name, "company": "Sunrise Pharmacy", "segment": "lusaka pharmacies",
              "location": "Lusaka", "whatsapp": "+260 977 123 456", **kw}
    lead, _deduped, _change = leads.create_lead(db, fields)
    db.commit()
    return lead


def _draft(db, lead, channel="whatsapp", body="Hi Mercy, I build stock alert bots for pharmacies.", **kw):
    with patch("llm.chat_text", return_value=body):
        result = outreach.draft(db, lead, channel, "intro", **kw)
    db.commit()
    return result


# ── 3. qualification ──────────────────────────────────────────

def test_qualification_scores_and_sets_status(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    db = _db()
    strong = _lead(db)                                                         # pre 40: reachable, company, segment, Zambia
    weak = _lead(db, "Ruth Zulu", company=None, segment=None, location="Nairobi",
                 whatsapp=None, email="ruth@example.com")                      # pre 15: reachable only
    off = _lead(db, "Peter Lungu", company="Lungu Mining", location="Kitwe", whatsapp="0966 000 111")
    assert (leads.pre_score(strong), leads.pre_score(weak), leads.pre_score(off)) == (40, 15, 40)

    answers = {
        "Mercy Banda": {"fit": 32, "reasons": ["owner-operated", "manual stock", "reachable", "a fourth"],
                        "disqualifiers": [], "suggested_angle": "Offer a stock alert bot."},
        "Ruth Zulu": {"fit": 30, "reasons": ["small school"], "disqualifiers": []},
        "Peter Lungu": {"fit": 8, "reasons": [], "disqualifiers": ["needs hardware on site"]},
    }
    seen = []

    def fake_chat(messages, **kwargs):
        seen.append((messages, kwargs))
        name = json.loads(messages[1]["content"])["name"]
        return json.dumps(answers[name])

    with patch("llm.chat_text", side_effect=fake_chat):
        out = client.post("/leads/qualify", json={"ids": [strong.id, weak.id, off.id]})
    assert out.status_code == 200, out.text
    by_name = {r["name"]: r for r in out.json()["results"]}

    assert by_name["Mercy Banda"]["score"] == 40 + 32 and by_name["Mercy Banda"]["status"] == "qualified"
    assert len(by_name["Mercy Banda"]["reasons"]) == 3                         # capped at three
    assert by_name["Ruth Zulu"]["score"] == 45 and by_name["Ruth Zulu"]["status"] == "new"   # below 50
    assert by_name["Ruth Zulu"]["outcome"] == "below_threshold"
    assert by_name["Peter Lungu"]["status"] == "disqualified"                 # disqualifier and fit < 15
    assert len(out.json()["changes"]) == 3

    messages, kwargs = seen[0]
    assert kwargs["purpose"] == "lead_qualify" and kwargs["max_tokens"] == 300
    assert kwargs["response_format"] == {"type": "json_object"}
    assert messages[0]["content"].startswith(leads.QUALIFY_PROMPT)            # stable text first
    assert "Ideal customer" in messages[0]["content"]                         # then the ICP file

    db.expire_all()
    stored = json.loads(db.query(models.Lead).filter(models.Lead.id == strong.id).one().qualification_json)
    assert stored["fit"] == 32 and stored["suggested_angle"] == "Offer a stock alert bot." and stored["scored_at"]
    summary = next(c["summary"] for c in out.json()["changes"] if c["record_id"] == strong.id)
    assert summary == "Qualified Mercy Banda 72/100"
    db.close()


def test_qualification_caps_at_twenty_and_degrades(monkeypatch):
    db = _db()
    lead = _lead(db)
    too_many = client.post("/leads/qualify", json={"ids": list(range(1, 22))})
    assert too_many.status_code == 400 and "max 20" in too_many.json()["detail"]
    tool, _ = ot.run_tool("qualify_leads", {"lead_ids": list(range(1, 22))}, db)
    assert tool["error"] == "max 20 per call"

    no_key, summary = ot.run_tool("qualify_leads", {"segment": "Lusaka Pharmacies"}, db)   # no key set
    assert no_key["results"][0]["skipped_llm"] is True and no_key["results"][0]["fit"] is None
    assert no_key["results"][0]["qualification_score"] == 40 and "nothing changed" in summary
    db.refresh(lead)
    assert lead.status == "new" and lead.qualification_score is None

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with patch("llm.chat_text", return_value="not json"):
        bad = leads.qualify(db, [lead.id])
    assert bad[0]["error"] and bad[0]["fit"] is None
    with patch("llm.chat_text", side_effect=RuntimeError("boom")):
        worse = leads.qualify(db, [lead.id, 9999])
    assert worse[0]["error"] and worse[1]["error"] == "lead not found"
    db.refresh(lead)
    assert lead.status == "new"
    assert client.post("/leads/qualify", json={}).status_code == 400
    db.close()


# ── 4. drafts and the gate ────────────────────────────────────

def test_draft_revision_supersedes_and_cancels_the_previous():
    db = _db()
    lead = _lead(db)
    with patch("llm.chat_text", return_value="Hi Mercy, quick intro.") as chat:
        first, summary = ot.run_tool("draft_outreach", {"lead": "Mercy", "channel": "whatsapp",
                                                        "brief": "intro, mention the bakery site"}, db)
    assert summary == "drafted whatsapp to Mercy Banda (v1)"
    assert first["version"] == 1 and first["body"] == "Hi Mercy, quick intro." and first["subject"] is None
    kwargs = chat.call_args.kwargs
    assert kwargs["purpose"] == "outreach_draft" and kwargs["max_tokens"] == 400 and "response_format" not in kwargs
    sent_messages = chat.call_args.args[0]
    assert sent_messages[0]["content"] == outreach.DRAFT_PROMPT          # stable prompt first
    assert "IDEAL CUSTOMER" in sent_messages[1]["content"]               # dynamic context second

    with patch("llm.chat_text", return_value="Shorter intro.") as chat:
        second, _ = ot.run_tool("draft_outreach", {"lead": lead.id, "channel": "whatsapp", "brief": "shorter",
                                                   "message_id": first["message_id"]}, db)
    assert second["version"] == 2
    assert "PREVIOUS DRAFT:\nHi Mercy, quick intro." in chat.call_args.args[0][2]["content"]
    rows = {m.id: m for m in db.query(models.OutreachMessage).all()}
    assert rows[first["message_id"]].status == "cancelled"
    assert rows[second["message_id"]].status == "draft"
    assert rows[second["message_id"]].supersedes_id == first["message_id"]

    with patch("llm.chat_text", return_value=json.dumps({"subject": "Stock alerts", "body": "Dear Mercy,"})) as chat:
        email, _ = ot.run_tool("draft_outreach", {"lead": "Mercy", "channel": "email"}, db)
    assert (email["subject"], email["body"]) == ("Stock alerts", "Dear Mercy,")
    assert chat.call_args.kwargs["response_format"] == {"type": "json_object"}

    nobody, _ = ot.run_tool("draft_outreach", {"lead": "Quantum Freight", "channel": "whatsapp"}, db)
    assert "error" in nobody
    no_key, _ = ot.run_tool("draft_outreach", {"lead": "Mercy", "channel": "whatsapp"}, db)   # real call, no key
    assert "AI key" in no_key["error"]
    assert db.query(models.OutreachMessage).count() == 3
    db.close()


def test_nothing_is_sent_without_a_later_yes_or_a_tap():
    db = _db()
    lead = _lead(db)
    mid = _draft(db, lead)["id"]
    db.close()

    def status():
        s = _db()
        try:
            return s.query(models.OutreachMessage).filter(models.OutreachMessage.id == mid).one().status
        finally:
            s.close()

    # One turn: the model requests the send and tries to confirm it straight away.
    s = _db()
    next_pending = (s.query(models.PendingAction).count()) + 1
    s.close()
    events = _orchestrate("send it", [
        _tool_round("request_send", {"message_id": mid}, call_id="call_a"),
        _tool_round("confirm_action", {"pending_id": next_pending}, call_id="call_b"),
        _final_round("Shall I send it?"),
    ])
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[0]["summary"].startswith("awaiting your yes: Send whatsapp to Mercy Banda")
    assert results[1]["error"] is True
    assert "confirmation must come in a later message" in results[1]["summary"]
    assert status() == "draft"                                   # still not sent
    s = _db()
    pending = s.query(models.PendingAction).one()
    assert (pending.kind, pending.status, pending.table_name) == ("send_outreach", "pending", "outreach_messages")
    assert s.query(models.Todo).count() == 0
    s.close()

    # A request alone never sends, however many times it is made.
    s = _db()
    again, _ = ot.run_tool("request_send", {"message_id": mid}, s)
    assert again["id"] == pending.id and s.query(models.PendingAction).count() == 1
    direct, _ = ot.run_tool("confirm_action", {"pending_id": pending.id}, s)
    assert direct["error"] == "confirmation must come in a later message" and direct["executed"] is False
    s.close()
    assert status() == "draft"

    # The next turn: Bandile says yes, and only now does it go.
    events = _orchestrate("yes", [
        _tool_round("confirm_action", {"pending_id": pending.id}),
        _final_round("Tap the link to open WhatsApp."),
    ])
    assert next(e for e in events if e["type"] == "tool_result")["error"] is False
    assert status() == "sent"
    tables = {c["table"] for c in events[-1]["changes"]}
    assert {"outreach_messages", "leads", "todos"} <= tables
    s = _db()
    assert s.query(models.PendingAction).one().status == "confirmed"
    s.close()


def test_the_dock_tap_confirms_a_requested_send():
    db = _db()
    lead = _lead(db)
    message = db.query(models.OutreachMessage).filter(models.OutreachMessage.id == _draft(db, lead)["id"]).one()
    pending = outreach.request_send(db, message)
    assert pending["kind"] == "send_outreach" and pending["requires_confirmation"] is True
    db.close()

    listed = client.get("/actions/pending").json()
    assert [p["id"] for p in listed] == [pending["id"]]
    tapped = client.post(f"/actions/{pending['id']}/confirm").json()     # a human tap is the approval
    assert tapped["executed"] is True and tapped["open_url"].startswith("https://wa.me/260977123456?text=")
    assert client.post(f"/actions/{pending['id']}/confirm").json()["executed"] is False   # already confirmed


# ── 5. the Approve & send tap ─────────────────────────────────

def test_send_tap_on_whatsapp_marks_sent_creates_follow_up_and_undoes():
    db = _db()
    lead = _lead(db)
    lead_id = lead.id
    mid = _draft(db, lead, body="Hi Mercy & team, 50% fewer stockouts?")["id"]
    db.close()

    sent = client.post(f"/outreach/{mid}/send")
    assert sent.status_code == 200, sent.text
    out = sent.json()
    assert out["message"]["status"] == "sent" and out["message"]["sent_at"]
    assert out["open_url"].startswith("https://wa.me/260977")
    assert out["open_url"] == "https://wa.me/260977123456?text=Hi%20Mercy%20%26%20team%2C%2050%25%20fewer%20stockouts%3F"

    db = _db()
    todo = db.query(models.Todo).filter(models.Todo.id == out["follow_up_todo_id"]).one()
    assert (todo.text, todo.category, todo.source, todo.lead_id) == ("Follow up with Mercy Banda", "outreach", "outreach", lead_id)
    assert todo.due_date == TODAY + timedelta(days=3) and todo.done is False
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).one()
    assert lead.status == "contacted" and lead.last_contact_at is not None
    assert lead.next_follow_up_at == TODAY + timedelta(days=3)
    comm = db.query(models.Communication).one()
    assert (comm.lead_id, comm.direction, comm.source, comm.outreach_message_id) == (lead_id, "out", "outreach", mid)
    db.close()

    detail = client.get(f"/leads/{lead_id}").json()
    assert detail["open_follow_up"]["todo_id"] == todo.id and detail["last_message"]["status"] == "sent"
    assert [m["id"] for m in detail["messages"]] == [mid] and len(detail["follow_ups"]) == 1
    assert client.post(f"/outreach/{mid}/send").status_code == 409            # already sent
    assert client.post(f"/outreach/{mid}/cancel").status_code == 409
    assert client.get("/outreach", params={"status": "draft"}).json() == []
    assert client.get(f"/outreach/{mid}").json()["lead_name"] == "Mercy Banda"

    batch_id = out["changes"][0]["batch_id"]
    assert len({c["batch_id"] for c in out["changes"]}) == 1                   # one batch
    assert client.post(f"/changes/batch/{batch_id}/undo").status_code == 200
    db = _db()
    message = db.query(models.OutreachMessage).filter(models.OutreachMessage.id == mid).one()
    assert message.status == "draft" and message.sent_at is None
    assert db.query(models.Todo).count() == 0
    assert db.query(models.Lead).filter(models.Lead.id == lead_id).one().status == "new"
    db.close()


def test_manual_channel_and_dismiss():
    db = _db()
    lead = _lead(db)
    linkedin = _draft(db, lead, channel="linkedin")["id"]
    scrap = _draft(db, lead, channel="call")["id"]
    db.close()
    out = client.post(f"/outreach/{linkedin}/send").json()
    assert out["message"]["status"] == "sent" and "open_url" not in out        # the user copies the text
    dismissed = client.post(f"/outreach/{scrap}/cancel").json()
    assert dismissed["status"] == "cancelled" and dismissed["change"]["table"] == "outreach_messages"
    assert client.post("/outreach/9999/send").status_code == 404


# ── 6. email before the integration exists ────────────────────

def test_email_send_fails_cleanly_until_email_is_configured():
    db = _db()
    lead = _lead(db, email="mercy@sunrise.example")
    lead.status = "qualified"
    db.commit()
    lead_id = lead.id
    with patch("llm.chat_text", return_value=json.dumps({"subject": "Stock alerts", "body": "Dear Mercy,"})):
        mid = outreach.draft(db, lead, "email", "intro")["id"]
    db.commit()
    db.close()

    out = client.post(f"/outreach/{mid}/send").json()
    assert out["message"]["status"] == "failed"
    assert out["message"]["error"] == "email sending is not configured"
    assert "open_url" not in out and "follow_up_todo_id" not in out
    db = _db()
    assert db.query(models.Lead).filter(models.Lead.id == lead_id).one().status == "qualified"
    assert db.query(models.Todo).count() == 0
    db.close()


# ── follow-ups ────────────────────────────────────────────────

def test_set_follow_up_moves_the_open_task_instead_of_adding_one():
    db = _db()
    lead = _lead(db)
    first, summary = ot.run_tool("set_follow_up", {"lead": "Mercy", "date": "in 3 days"}, db)
    assert first["follow_up_on"] == TODAY + timedelta(days=3) and "follow-up with Mercy Banda" in summary
    moved, _ = ot.run_tool("set_follow_up", {"lead": "Mercy", "date": (TODAY + timedelta(days=10)).isoformat(),
                                             "reason": "after the demo"}, db)
    assert moved["todo_id"] == first["todo_id"]
    todos = db.query(models.Todo).all()
    assert len(todos) == 1 and todos[0].due_date == TODAY + timedelta(days=10)
    assert todos[0].text == "Follow up with Mercy Banda — after the demo"
    db.refresh(lead)
    assert lead.next_follow_up_at == TODAY + timedelta(days=10)
    bad, _ = ot.run_tool("set_follow_up", {"lead": "Mercy", "date": "whenever"}, db)
    assert "error" in bad
    db.close()


# ════════════════════════════════════════════════════════════════
# Replies, conversion, stats, briefing, alert
# ════════════════════════════════════════════════════════════════

from datetime import datetime


def _sent(db, lead, channel="whatsapp", days_ago=0):
    """A message that was approved and sent ``days_ago`` days ago, with its follow-up."""
    mid = _draft(db, lead, channel=channel)["id"]
    message = db.query(models.OutreachMessage).filter(models.OutreachMessage.id == mid).one()
    outreach.execute_send(db, message)
    when = datetime.utcnow() - timedelta(days=days_ago)
    message.sent_at = when
    lead.last_contact_at = when
    db.commit()
    return message


# ── 7. replies ────────────────────────────────────────────────

def test_log_lead_reply_records_the_reply_and_closes_the_follow_up():
    db = _db()
    lead = _lead(db)
    _sent(db, lead)
    assert db.query(models.Todo).filter(models.Todo.done == False).count() == 1     # noqa: E712

    result, summary = ot.run_tool("log_lead_reply", {"lead": "Mercy", "summary": "Wants a demo on Friday"}, db)
    assert result["status"] == "replied" and summary == "Mercy Banda replied — follow-up closed"
    inbound = db.query(models.OutreachMessage).filter(models.OutreachMessage.direction == "in").one()
    assert (inbound.status, inbound.channel, inbound.body) == ("received", "whatsapp", "Wants a demo on Friday")
    db.refresh(lead)
    assert lead.status == "replied" and lead.next_follow_up_at is None
    assert f"- {TODAY.isoformat()}: replied — Wants a demo on Friday" in lead.notes
    todo = db.query(models.Todo).one()
    assert todo.done is True and todo.completed_at == TODAY
    assert db.query(models.Communication).filter(models.Communication.direction == "in").count() == 1
    db.close()

    other = client.post("/leads", json={"name": "Ruth Zulu", "email": "ruth@example.com"}).json()
    tapped = client.post(f"/leads/{other['id']}/replied")                      # the one-tap path, no summary
    assert tapped.status_code == 200 and tapped.json()["status"] == "replied"
    assert tapped.json()["change"]["table"] == "leads"
    assert client.post("/leads/9999/replied").status_code == 404


# ── 8. conversion ─────────────────────────────────────────────

def test_convert_lead_links_the_client_both_ways_and_is_idempotent():
    db = _db()
    lead = _lead(db, email="mercy@sunrise.example", role="Owner")
    _sent(db, lead)
    lead_id = lead.id

    result, summary = ot.run_tool("convert_lead", {"lead": "Mercy Banda"}, db)
    assert summary == "Mercy Banda is now a client" and result["already_converted"] is False
    client_row = db.query(models.Client).one()
    assert (client_row.name, client_row.company, client_row.status) == ("Mercy Banda", "Sunrise Pharmacy", "active")
    assert client_row.lead_id == lead_id and client_row.service == "intro" and client_row.currency == "ZMW"
    db.refresh(lead)
    assert lead.client_id == client_row.id and lead.status == "converted"
    assert db.query(models.Todo).filter(models.Todo.done == False).count() == 0     # noqa: E712
    contact = db.query(models.Contact).one()
    assert (contact.client_id, contact.name, contact.role, contact.is_primary) == (client_row.id, "Mercy Banda", "Owner", True)
    assert contact.email == "mercy@sunrise.example" and contact.whatsapp == "+260977123456"
    assert {c["table"] for c in result["changes"]} == {"clients", "leads", "todos", "contacts"}
    db.close()

    again = client.post(f"/leads/{lead_id}/convert").json()
    assert again["already_converted"] is True and again["client"]["id"] == client_row.id
    assert again["changes"] == []
    db = _db()
    assert db.query(models.Client).count() == 1 and db.query(models.Contact).count() == 1
    db.close()


# ── 9. briefing and the alert ─────────────────────────────────

def test_briefing_shows_waiting_drafts_and_quiet_leads():
    import briefing
    db = _db()
    waiting = _lead(db)
    draft_id = _draft(db, waiting)["id"]
    quiet = _lead(db, "Ruth Zulu", whatsapp="+260 955 111 222")
    _sent(db, quiet, days_ago=10)
    for todo in db.query(models.Todo).all():                 # the follow-up was done, and still no reply
        todo.done = True
    chased = _lead(db, "Peter Lungu", whatsapp="+260 966 333 444")
    _sent(db, chased, days_ago=10)                           # still has its open follow-up
    recent = _lead(db, "Chanda Mwila", whatsapp="+260 977 555 010")
    _sent(db, recent, days_ago=3)
    for todo in db.query(models.Todo).filter(models.Todo.lead_id == recent.id).all():
        todo.done = True
    db.commit()

    b = briefing.build_briefing(_db(), with_summary=False)
    by_id = {i["id"]: i for i in b["items"]}
    draft_item = by_id[f"outreach:outreach_message:{draft_id}"]
    assert draft_item["score"] == 38 and draft_item["reason"] == "Draft waiting for your approval"
    assert draft_item["suggested_action"] == "Approve & send or dismiss" and draft_item["completable"] is False
    quiet_item = by_id[f"outreach:lead:{quiet.id}"]
    assert quiet_item["score"] == 28 and quiet_item["reason"] == "No reply in 10 days"
    assert quiet_item["suggested_action"] == "Follow up or mark lost"
    assert f"outreach:lead:{chased.id}" not in by_id         # an open follow-up already covers it
    assert f"outreach:lead:{recent.id}" not in by_id         # only three days
    assert b["counts"]["drafts_waiting"] == 1

    client.post(f"/outreach/{draft_id}/cancel")
    after = briefing.build_briefing(_db(), with_summary=False)
    assert after["counts"]["drafts_waiting"] == 0
    assert f"outreach:outreach_message:{draft_id}" not in {i["id"] for i in after["items"]}
    db.close()


def test_outreach_stalled_fires_with_three_qualified_leads_and_no_sends():
    db = _db()

    def fired():
        alerts, _ = ot.detect_off_track(db)
        return any(a["rule_id"] == "outreach_stalled" for a in alerts)

    made = [_lead(db, name, whatsapp=f"+260 977 000 00{i}") for i, name in enumerate(["A One", "B Two", "C Three"])]
    assert fired() is False                                   # leads exist but none is qualified
    for lead in made[:2]:
        lead.status = "qualified"
    db.commit()
    assert fired() is False                                   # two qualified: under the minimum
    made[2].status = "qualified"
    db.commit()
    assert fired() is True

    old = _sent(db, _lead(db, "D Four", whatsapp="+260 977 000 009"), days_ago=8)
    assert fired() is True                                    # a send eight days ago does not count
    old.sent_at = datetime.utcnow() - timedelta(days=2)
    db.commit()
    assert fired() is False                                   # something was sent this week
    alert = [r for r in json.loads((Path(__file__).parent.parent.parent / "context" / "core"
                                    / "off_track_rules.json").read_text(encoding="utf-8"))["rules"]
             if r["id"] == "outreach_stalled"][0]
    assert alert["severity"] == "warn" and alert["params"] == {"window_days": 7, "min_qualified": 3}
    db.close()


# ── 10. stats ─────────────────────────────────────────────────

def test_outreach_stats_rates_per_segment_and_channel():
    db = _db()
    pharm = [_lead(db, f"Pharmacy {i}", whatsapp=f"+260 977 100 00{i}") for i in range(4)]
    bakers = [_lead(db, f"Baker {i}", segment="kitwe bakeries", whatsapp=f"+260 966 200 00{i}") for i in range(2)]
    untouched = _lead(db, "Nobody Yet", segment="kitwe bakeries", whatsapp="+260 966 200 009")
    for lead in pharm[:3]:
        _sent(db, lead)                                       # three pharmacies contacted on WhatsApp
    _sent(db, bakers[0], channel="linkedin")                  # one baker contacted on LinkedIn
    _sent(db, bakers[1], days_ago=120)                        # outside the 90-day window
    leads.log_reply(db, pharm[0], "Interested")
    leads.log_reply(db, pharm[1], "Send a quote")
    leads.convert(db, pharm[1])
    db.commit()
    db.close()

    s = client.get("/outreach/stats").json()
    seg = {r["segment"]: r for r in s["by_segment"]}
    assert seg["lusaka pharmacies"] == {"segment": "lusaka pharmacies", "leads": 4, "contacted": 3, "replied": 2,
                                        "converted": 1, "reply_rate": 0.667, "conversion_rate": 0.333}
    assert seg["kitwe bakeries"] == {"segment": "kitwe bakeries", "leads": 3, "contacted": 1, "replied": 0,
                                     "converted": 0, "reply_rate": 0.0, "conversion_rate": 0.0}
    chan = {r["channel"]: r for r in s["by_channel"]}
    assert chan["whatsapp"]["sent"] == 3 and chan["whatsapp"]["reply_rate"] == 0.667
    assert chan["linkedin"] == {"channel": "linkedin", "sent": 1, "contacted": 1, "replied": 0, "converted": 0,
                                "reply_rate": 0.0, "conversion_rate": 0.0}
    assert s["totals"] == {"leads": 7, "sent": 4, "contacted": 4, "replied": 2, "converted": 1,
                           "reply_rate": 0.5, "conversion_rate": 0.25}

    empty = client.get("/outreach/stats", params={"days": 1}).json()
    assert empty["days"] == 1 and empty["totals"]["contacted"] == 4       # all four were sent today
    db = _db()
    for m in db.query(models.OutreachMessage).all():
        m.sent_at = datetime.utcnow() - timedelta(days=200)
    db.commit()
    db.close()
    none = client.get("/outreach/stats").json()["totals"]
    assert none["contacted"] == 0 and none["reply_rate"] == 0.0 and none["conversion_rate"] == 0.0   # zero-safe
