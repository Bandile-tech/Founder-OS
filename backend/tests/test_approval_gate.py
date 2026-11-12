"""
The outreach approval gate, as an independent review tried to break it.

The rule: no message leaves (an email over SMTP, a WhatsApp link marked sent) without an
explicit approval from Bandile, given after the send was asked for. The assistant drafts and
asks; it can never send on its own, whatever a lead's notes or an email snippet tell it.

Each test reproduces one way the gate gave way before it was tightened.
No network and no OpenAI.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import actions
import conversation
import llm
import models
import orchestrator_tools as ot
import outreach
import outreach_channels
from tests.conftest import TestSessionLocal, client
from tests.test_phase5b_leads import _draft, _lead
from tests.test_stage2_changes import _final_round, _orchestrate, _tool_round


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("OPENAI_API_KEY", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "FOUNDER_OS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DEFAULT_COUNTRY_CODE", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


def _status(mid):
    s = _db()
    try:
        return s.query(models.OutreachMessage).filter(models.OutreachMessage.id == mid).one().status
    finally:
        s.close()


def _requested(body="Hi Mercy, I build stock alert bots for pharmacies."):
    """A WhatsApp draft whose send the assistant has asked for, in a turn of its own."""
    db = _db()
    lead = _lead(db)
    mid = _draft(db, lead, body=body)["id"]
    lead_id = lead.id
    db.close()
    events = _orchestrate("send Mercy the intro", [
        _tool_round("request_send", {"message_id": mid}), _final_round("Shall I send it?")])
    db = _db()
    pending_id = db.query(models.PendingAction).filter_by(kind="send_outreach", status="pending").one().id
    db.close()
    return mid, pending_id, lead_id, events


# ── 1. A later message is not a yes ───────────────────────────

def test_an_unrelated_later_message_does_not_approve_a_send():
    """Any later message from Bandile used to open the gate, whatever it said."""
    mid, pending_id, _lead_id, _ = _requested()
    events = _orchestrate("thanks. what is on today?", [
        _tool_round("confirm_action", {"pending_id": pending_id}), _final_round("Sent.")])
    refused = next(e for e in events if e["type"] == "tool_result")
    assert refused["error"] is True and "yes" in refused["summary"]
    assert _status(mid) == "draft"

    for text in ("ok what else is due?", "yes but change the subject first", "no", "send the report to Wamu instead"):
        events = _orchestrate(text, [_tool_round("confirm_action", {"pending_id": pending_id}), _final_round("…")])
        assert next(e for e in events if e["type"] == "tool_result")["error"] is True, text
        assert _status(mid) == "draft", text

    events = _orchestrate("Yes, send it.", [
        _tool_round("confirm_action", {"pending_id": pending_id}), _final_round("Tap the link.")])
    assert next(e for e in events if e["type"] == "tool_result")["error"] is False
    assert _status(mid) == "sent"


def test_a_command_or_another_row_is_not_a_yes():
    mid, pending_id, _lead_id, _ = _requested()
    db = _db()
    conversation.save_message(db, "user", "/undo", session_id="telegram:1")      # what a Telegram command leaves
    conversation.save_message(db, "assistant", "yes", session_id="s")             # the model saying yes to itself
    out, _ = ot.run_tool("confirm_action", {"pending_id": pending_id}, db)
    db.close()
    assert out["executed"] is False and _status(mid) == "draft"


# ── 2. A yes answers the newest proposal only ─────────────────

def test_a_yes_to_something_else_is_not_spent_on_an_older_send():
    mid, pending_id, _lead_id, _ = _requested()
    db = _db()
    todo = models.Todo(text="Renew the gym", done=False, priority=5)
    db.add(todo)
    db.commit()
    todo_id = todo.id
    db.close()
    _orchestrate("delete the gym task", [
        _tool_round("propose_delete", {"table": "todos", "record_id": todo_id}), _final_round("Delete it?")])
    events = _orchestrate("yes", [                                # he means the delete
        _tool_round("confirm_action", {"pending_id": pending_id}), _final_round("Sent.")])
    refused = next(e for e in events if e["type"] == "tool_result")
    assert refused["error"] is True and _status(mid) == "draft"

    # Asking again makes a fresh request, which then needs its own yes.
    db = _db()
    again, _ = ot.run_tool("request_send", {"message_id": mid}, db)
    assert again["id"] != pending_id
    assert db.query(models.PendingAction).filter_by(id=pending_id).one().status == "cancelled"
    out, _ = ot.run_tool("confirm_action", {"pending_id": again["id"]}, db)     # the old "yes" predates it
    db.close()
    assert out["executed"] is False and _status(mid) == "draft"


def test_proposing_to_delete_a_draft_never_hands_back_its_send():
    """propose_delete reused any open proposal for the row, so "delete that draft" returned
    the pending SEND, and the yes that followed sent the email."""
    mid, pending_id, _lead_id, _ = _requested()
    db = _db()
    proposal, _ = ot.run_tool("propose_delete", {"table": "outreach_messages", "record_id": mid}, db)
    assert proposal["id"] != pending_id and proposal["kind"] == "delete"
    assert proposal["summary"].startswith("Delete")
    assert db.query(models.PendingAction).filter_by(id=pending_id).one().status == "cancelled"   # he wants it gone
    done = actions.confirm_action(db, proposal["id"], human=True)
    assert done["executed"] is True
    assert db.query(models.OutreachMessage).count() == 0 and db.query(models.Communication).count() == 0
    db.close()


# ── 3. What is approved is what is sent, to whom it was shown ──

def test_the_request_shows_the_recipient_and_the_text_whatever_the_model_says():
    mid, _pending_id, _lead_id, events = _requested(body="Hi Mercy, quick question about stock alerts.")
    final = events[-1]["content"]
    assert final.startswith("Shall I send it?")
    assert "+260977123456" in final and "Mercy Banda" in final
    assert "Hi Mercy, quick question about stock alerts." in final and "Nothing has been sent" in final
    draft = client.get("/outreach", params={"status": "draft"}).json()[0]
    assert draft["recipient"] == "+260977123456"                 # the draft card shows who it goes to


def test_a_recipient_changed_after_the_request_is_not_sent_to():
    mid, pending_id, lead_id, _ = _requested()
    db = _db()
    lead = db.query(models.Lead).filter_by(id=lead_id).one()
    lead.whatsapp = "+260955000111"                              # e.g. a later add_lead that matched and overwrote it
    db.commit()
    out = actions.confirm_action(db, pending_id, human=True)
    db.close()
    assert out["executed"] is False and "recipient" in out["reason"]
    assert _status(mid) == "draft"


def test_an_address_with_a_second_address_inside_it_is_refused(monkeypatch):
    import integrations_email as mail
    monkeypatch.setenv("EMAIL_ADDRESS", "bandile@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    sent = []

    class SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def login(self, *a): pass
        def send_message(self, message): sent.append(message["To"])

    db = _db()
    with patch.object(mail.smtplib, "SMTP_SSL", SMTP):
        bad = mail.send(db, to='"mercy@sunrise.example" <evil@attacker.example>', subject="Hi", body="Hello")
        good = mail.send(db, to=" Mercy@Sunrise.example ", subject="Hi", body="Hello")
    db.close()
    assert bad["ok"] is False and "not an email address" in bad["error"]
    assert good["ok"] is True and sent == ["mercy@sunrise.example"]


# ── 4. One approval, one send ─────────────────────────────────

def test_two_approvals_at_once_send_once():
    """Nothing claimed the message before the channel was called, so a tap on two devices
    (or a tap alongside /yes) sent it twice. The overlap is made deterministic by letting the
    first send trigger the second approval."""
    db = _db()
    lead = _lead(db)
    mid = _draft(db, lead)["id"]
    db.close()
    calls, second = [], {}

    class Slow(outreach_channels.Channel):
        name = "whatsapp"

        def send(self, db, message, lead):
            calls.append(message.id)
            if len(calls) == 1:
                second["response"] = client.post(f"/outreach/{mid}/send")      # the other tap arrives now
            return {"ok": True, "open_url": "https://wa.me/260977123456?text=x"}

    with patch.object(outreach_channels, "get_channel", lambda name: Slow()):
        first = client.post(f"/outreach/{mid}/send")
    assert first.status_code == 200 and first.json()["message"]["status"] == "sent"
    assert second["response"].status_code == 409
    assert calls == [mid]                                         # the channel was called once
    db = _db()
    assert db.query(models.Communication).count() == 1 and db.query(models.Todo).count() == 1
    db.close()
