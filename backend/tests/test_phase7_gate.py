"""
Phase 7, Stage 4 — the two approval-gate items that were listed and not fixed in the
Phase 5 and 6 review, closed before anything proactive can lead to an action.

  1. Voice: a spoken turn that starts with "yes" must not approve an action proposed in
     that same turn. (The transcript of what he said reaches the server after the tool call
     it led to, so "yes, send it" used to be read as the answer to the request it caused.)
  2. The dock has a Confirm card for every pending send: GET /actions/pending carries who
     it goes to and the whole text, and POST /actions/{id}/confirm is the tap.
"""

import pytest

import llm
import models
import voice_realtime
from tests.conftest import TestSessionLocal, client
from tests.test_phase5b_leads import _draft, _lead

BODY = "Hi Mercy, I build stock alert bots for pharmacies."
SESSION = "voice-session-1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("OPENAI_API_KEY", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "FOUNDER_OS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DEFAULT_COUNTRY_CODE", raising=False)
    monkeypatch.setattr(voice_realtime, "YES_WAIT_SECONDS", 0.0)       # do not wait for a late transcript in tests
    voice_realtime._SPOKEN.clear()
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


def _a_draft(body=BODY):
    db = _db()
    lead = _lead(db)
    mid = _draft(db, lead, body=body)["id"]
    db.close()
    return mid


def _status(mid):
    db = _db()
    try:
        return db.query(models.OutreachMessage).filter(models.OutreachMessage.id == mid).one().status
    finally:
        db.close()


def _said(text, turn, session=SESSION, role="user"):
    body = {"role": role, "text": text, "voice_session_id": session}
    if turn is not None:
        body["user_turn"] = turn
    r = client.post("/voice/realtime/transcript", json=body)
    assert r.status_code == 200 and r.json()["saved"] is True


def _voice_tool(name, args, turn, session=SESSION):
    body = {"name": name, "arguments": args, "voice_session_id": session}
    if turn is not None:
        body["user_turn"] = turn
    r = client.post("/voice/realtime/tool", json=body)
    assert r.status_code == 200
    return r.json()


def _pending_id():
    db = _db()
    try:
        return db.query(models.PendingAction).filter_by(status="pending").order_by(models.PendingAction.id.desc()).first().id
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════
# 1. Voice: a yes in the turn that proposed the action approves nothing
# ════════════════════════════════════════════════════════════════

def test_a_spoken_turn_starting_with_yes_cannot_approve_the_send_it_asked_for():
    """He says "yes, send it to Mercy". In that one turn the model requests the send and
    then tries to confirm it. The transcript arrives in between, as it does in real use."""
    mid = _a_draft()
    asked = _voice_tool("request_send", {"message_id": mid}, turn=1)
    assert asked["error"] is False
    _said("Yes, send it.", turn=1)                              # the words of turn 1, landing late
    tried = _voice_tool("confirm_action", {"pending_id": _pending_id()}, turn=1)
    assert tried["result"]["executed"] is False and tried["error"] is True
    assert "same spoken turn" in tried["result"]["error"]
    assert _status(mid) == "draft"
    # The model is told why, in words, and what to do instead.
    assert "wait for him to reply" in tried["output"]
    # However often it tries within the turn, and whatever else was transcribed meanwhile.
    _said("Shall I send this to Mercy Banda?", turn=None, role="assistant")
    assert _voice_tool("confirm_action", {"pending_id": _pending_id()}, turn=1)["result"]["executed"] is False
    assert _status(mid) == "draft"


def test_a_plain_yes_in_a_later_spoken_turn_sends():
    mid = _a_draft()
    _said("Send the draft to Mercy.", turn=1)
    _voice_tool("request_send", {"message_id": mid}, turn=1)
    _said("WhatsApp to Mercy Banda. Hi Mercy, I build stock alert bots for pharmacies. Shall I send it?",
          turn=None, role="assistant")
    _said("Yes.", turn=2)
    done = _voice_tool("confirm_action", {"pending_id": _pending_id()}, turn=2)
    assert done["result"]["executed"] is True and done["error"] is False
    assert done["result"]["open_url"].startswith("https://wa.me/260977123456")
    assert _status(mid) == "sent"


@pytest.mark.parametrize("words, why", [
    ("Wait, change the first line.", "not a plain yes"),
    ("Yes but make it shorter first.", "not a plain yes"),
    ("What is on today?", "not a plain yes"),
    (None, "has not come through"),                              # a noise, or words that never arrived
])
def test_a_later_turn_that_is_not_a_yes_sends_nothing(words, why):
    mid = _a_draft()
    _voice_tool("request_send", {"message_id": mid}, turn=1)
    _said("Yes, send it.", turn=1)
    if words:
        _said(words, turn=2)
    tried = _voice_tool("confirm_action", {"pending_id": _pending_id()}, turn=2)
    assert tried["result"]["executed"] is False and why in tried["result"]["error"]
    assert _status(mid) == "draft"


def test_a_voice_client_that_reports_no_turn_cannot_confirm_a_send_by_voice_but_a_tap_still_works():
    mid = _a_draft()
    _voice_tool("request_send", {"message_id": mid}, turn=None)        # an old tab, still open
    _said("Yes, send it.", turn=None)
    tried = _voice_tool("confirm_action", {"pending_id": _pending_id()}, turn=None)
    assert tried["result"]["executed"] is False and "tap Confirm" in tried["result"]["error"]
    assert _status(mid) == "draft"
    tapped = client.post(f"/actions/{_pending_id()}/confirm").json()   # the tap is itself the approval
    assert tapped["executed"] is True and _status(mid) == "sent"


def test_a_yes_from_another_voice_session_or_an_earlier_turn_is_not_borrowed():
    mid = _a_draft()
    _said("Yes.", turn=5, session="an-older-session")
    _said("Yes.", turn=1)
    _voice_tool("request_send", {"message_id": mid}, turn=2)
    for turn, session in ((2, SESSION), (1, SESSION), (5, "an-older-session")):
        tried = _voice_tool("confirm_action", {"pending_id": _pending_id()}, turn=turn, session=session)
        assert tried["result"]["executed"] is False, (turn, session)
    assert _status(mid) == "draft"


def test_a_send_proposed_in_the_dock_can_be_approved_by_a_spoken_yes():
    from tests.test_stage2_changes import _final_round, _orchestrate, _tool_round
    mid = _a_draft()
    _orchestrate("send it to Mercy", [_tool_round("request_send", {"message_id": mid}), _final_round("Shall I?")])
    _said("Yes, go ahead.", turn=1)
    done = _voice_tool("confirm_action", {"pending_id": _pending_id()}, turn=1)
    assert done["result"]["executed"] is True and _status(mid) == "sent"


def test_the_same_turn_rule_holds_for_a_delete_too():
    db = _db()
    db.add(models.Todo(text="Renew the domain", done=False))
    db.commit()
    todo_id = db.query(models.Todo).one().id
    db.close()
    proposed = _voice_tool("propose_delete", {"table": "todos", "record_id": todo_id}, turn=3)
    pending_id = proposed["result"]["id"]
    _said("Yes, delete the domain task.", turn=3)
    tried = _voice_tool("confirm_action", {"pending_id": pending_id}, turn=3)
    assert tried["result"]["executed"] is False and "same spoken turn" in tried["result"]["error"]
    db = _db()
    assert db.query(models.Todo).count() == 1
    db.close()
    _said("Yes.", turn=4)
    assert _voice_tool("confirm_action", {"pending_id": pending_id}, turn=4)["result"]["executed"] is True
    db = _db()
    assert db.query(models.Todo).count() == 0
    db.close()


def test_the_typed_gate_is_unchanged():
    """The dock saves what he typed before the turn runs, so its rule stays as it was."""
    from tests.test_stage2_changes import _final_round, _orchestrate, _tool_round
    mid = _a_draft()
    events = _orchestrate("yes, send it to Mercy", [
        _tool_round("request_send", {"message_id": mid}), _tool_round("confirm_action", {"pending_id": 1}),
        _final_round("…")])
    assert [e["error"] for e in events if e["type"] == "tool_result"] == [False, True]
    assert _status(mid) == "draft"
    events = _orchestrate("Yes.", [_tool_round("confirm_action", {"pending_id": _pending_id()}), _final_round("Sent.")])
    assert next(e for e in events if e["type"] == "tool_result")["error"] is False and _status(mid) == "sent"


# ════════════════════════════════════════════════════════════════
# 2. A Confirm card for every pending send
# ════════════════════════════════════════════════════════════════

def test_every_pending_send_is_listed_with_its_recipient_and_whole_text():
    db = _db()
    mercy = _lead(db)
    chanda = _lead(db, name="Chanda Mwila", company="Lusaka Logistics", whatsapp=None, email="chanda@example.com")
    first = _draft(db, mercy, body=BODY)["id"]
    second = _draft(db, chanda, channel="email", body='{"subject": "Stock alerts", "body": "Hello Chanda,\\nTwo lines here."}')["id"]
    db.add(models.Todo(text="Renew the domain", done=False))
    db.commit()
    todo_id = db.query(models.Todo).one().id
    db.close()
    assert client.get("/actions/pending").json() == []

    _voice_tool("request_send", {"message_id": first}, turn=1)
    _voice_tool("request_send", {"message_id": second}, turn=2)
    _voice_tool("propose_delete", {"table": "todos", "record_id": todo_id}, turn=3)

    pending = client.get("/actions/pending").json()
    sends = [p for p in pending if p["kind"] == "send_outreach"]
    assert len(sends) == 2 and len(pending) == 3                 # one card per send; the delete is listed too
    by_message = {p["send"]["message_id"]: p for p in sends}
    assert by_message[first]["send"] == {
        "message_id": first, "channel": "whatsapp", "to_name": "Mercy Banda", "to": "+260977123456",
        "subject": None, "body": BODY, "recipient_changed": False}
    email = by_message[second]["send"]
    assert (email["channel"], email["to"], email["subject"]) == ("email", "chanda@example.com", "Stock alerts")
    assert email["body"] == "Hello Chanda,\nTwo lines here."     # the whole text, not a summary of it
    assert all(p["requires_confirmation"] for p in pending) and "send" not in pending[0]

    # The tap: Confirm sends that one message and takes its card away. Cancel takes one away unsent.
    tapped = client.post(f"/actions/{by_message[first]['id']}/confirm").json()
    assert tapped["executed"] is True and tapped["open_url"].startswith("https://wa.me/") and _status(first) == "sent"
    client.post(f"/actions/{by_message[second]['id']}/cancel")
    left = client.get("/actions/pending").json()
    assert [p["kind"] for p in left] == ["delete"] and _status(second) == "draft"


def test_a_card_says_when_the_address_changed_and_an_expired_request_has_no_card():
    from datetime import datetime, timedelta
    mid = _a_draft()
    _voice_tool("request_send", {"message_id": mid}, turn=1)
    db = _db()
    lead = db.query(models.Lead).one()
    lead.whatsapp = "+260 955 000 111"
    db.commit()
    db.close()
    card, = client.get("/actions/pending").json()
    assert card["send"]["recipient_changed"] is True and card["send"]["to"] == "+260955000111"
    refused = client.post(f"/actions/{card['id']}/confirm").json()        # the yes was for the address shown before
    assert refused["executed"] is False and "recipient changed" in refused["reason"] and _status(mid) == "draft"

    other = _a_draft("A second draft.")
    _voice_tool("request_send", {"message_id": other}, turn=2)
    db = _db()
    row = db.query(models.PendingAction).filter_by(status="pending").one()
    row.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    db.close()
    assert client.get("/actions/pending").json() == []
