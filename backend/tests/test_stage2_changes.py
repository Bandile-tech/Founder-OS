"""
Rebuild Stage 2 — change log, undo, delete confirmation, intent classification.

OpenAI is mocked throughout (the parser is patched with canned payloads).
"""

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
import llm
import change_log
import actions
import notes
import orchestrator_tools as ot
import conversation
from tests.conftest import TestSessionLocal, client

TODAY = date.today()


def _db():
    return TestSessionLocal()


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _parsed(**overrides):
    base = {
        "summary": "logged", "kpi_updates": [], "todos_add": [], "todos_complete": [],
        "roadmap_complete": [], "habits_done": [], "annual_updates": [], "reading_updates": [],
        "bible_log": [], "revenue_updates": [], "log_entry": None, "advisory": None,
    }
    base.update(overrides)
    return base


def _parse(payload, text="brain dump"):
    with patch("main.get_parse_response", return_value=payload):
        r = client.post("/parse", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


# ── Scripted orchestrator helpers (same shape as Stage 1) ────

def _scripted_stream(rounds):
    state = {"i": 0}

    def _fake(convo, tools):
        i = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        yield from rounds[i]

    return _fake


def _tool_round(tool, args, call_id="call_0"):
    return [{"tool_calls": [{"index": 0, "id": call_id, "name": tool, "arguments": json.dumps(args)}]},
            {"finish_reason": "tool_calls"}]


def _final_round(text):
    return [{"content": text}, {"finish_reason": "stop"}]


def _orchestrate(text, rounds):
    with patch("openai_client._stream_completion", _scripted_stream(rounds)):
        r = client.post("/orchestrator", json={"text": text, "session_id": "s"})
    assert r.status_code == 200
    return [json.loads(l[5:]) for l in r.text.split("\n") if l.startswith("data:")]


# ════════════════════════════════════════════════════════════════
# 1. Change log + undo
# ════════════════════════════════════════════════════════════════

class TestChangeLogAndUndo:

    def test_parser_writes_are_logged_in_one_batch(self):
        db = _db()
        db.add(models.Habit(key="scripture_prayer", label="Scripture & Prayer", date=TODAY, done=False))
        db.commit()
        db.close()
        data = _parse(_parsed(
            todos_add=[{"text": "Email the CEO", "priority": 2, "category": "business"}],
            habits_done=["scripture_prayer"],
            revenue_updates=[{"amount": 150, "source": "Wamu's", "client": None}],
        ))
        changes = data["updates"]["changes"]
        assert {c["table"] for c in changes} == {"todos", "habits", "revenue"}
        assert {c["action"] for c in changes} == {"create", "update"}
        assert len({c["batch_id"] for c in changes}) == 1
        assert data["updates"]["batch_id"] == changes[0]["batch_id"]
        db = _db()
        rows = db.query(models.ChangeLog).all()
        db.close()
        assert len(rows) == 3 and all(r.source == "parser" for r in rows)

    def test_undo_create_removes_row(self):
        data = _parse(_parsed(todos_add=[{"text": "Email the CEO"}]))
        change = next(c for c in data["updates"]["changes"] if c["table"] == "todos")
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == change["record_id"]).count() == 1
        db.close()
        r = client.post(f"/changes/{change['id']}/undo")
        assert r.status_code == 200 and r.json()["already_undone"] is False
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == change["record_id"]).count() == 0
        db.close()

    def test_undo_update_restores_before_state(self):
        db = _db()
        db.add(models.Habit(key="ironing", label="Clothes ironed", date=TODAY, done=False))
        db.commit()
        db.close()
        data = _parse(_parsed(habits_done=["ironing"]))
        change = next(c for c in data["updates"]["changes"] if c["table"] == "habits")
        db = _db()
        assert db.query(models.Habit).filter(models.Habit.key == "ironing").first().done is True
        db.close()
        client.post(f"/changes/{change['id']}/undo")
        db = _db()
        assert db.query(models.Habit).filter(models.Habit.key == "ironing").first().done is False
        db.close()

    def test_undo_is_idempotent(self):
        data = _parse(_parsed(todos_add=[{"text": "Email the CEO"}]))
        change = next(c for c in data["updates"]["changes"] if c["table"] == "todos")
        first = client.post(f"/changes/{change['id']}/undo").json()
        second = client.post(f"/changes/{change['id']}/undo").json()
        assert first["already_undone"] is False and second["already_undone"] is True
        db = _db()
        assert db.query(models.Todo).count() == 0
        db.close()

    def test_batch_undo_reverts_whole_turn(self):
        db = _db()
        db.add(models.Habit(key="ironing", label="Clothes ironed", date=TODAY, done=False))
        db.commit()
        db.close()
        data = _parse(_parsed(todos_add=[{"text": "A"}, {"text": "B"}], habits_done=["ironing"]))
        batch = data["updates"]["batch_id"]
        r = client.post(f"/changes/batch/{batch}/undo")
        assert r.status_code == 200
        assert r.json()["undone"] == 3
        db = _db()
        assert db.query(models.Todo).count() == 0
        assert db.query(models.Habit).first().done is False
        db.close()
        assert client.post(f"/changes/batch/{batch}/undo").json()["already_undone"] == 3

    def test_undo_delete_reinserts_with_original_id(self):
        db = _db()
        todo = models.Todo(text="Keep me", priority=3, category="personal", due=str(TODAY), source="manual")
        db.add(todo)
        db.commit()
        tid = todo.id
        with change_log.batch_scope(db, "ui"):
            entry = change_log.record_delete(db, todo, "Deleted test todo")
        db.commit()
        assert db.query(models.Todo).filter(models.Todo.id == tid).count() == 0
        db.close()
        client.post(f"/changes/{entry['id']}/undo")
        db = _db()
        back = db.query(models.Todo).filter(models.Todo.id == tid).first()
        db.close()
        assert back is not None and back.text == "Keep me" and back.priority == 3

    def test_kpi_undo_reloads_in_memory_state(self):
        import main
        db = _db()
        main.load_kpi_state(db)   # startup does this; the test harness never fires startup
        db.close()
        before_value = main._kpi_state["maths_syllabus"]["value"]
        data = _parse(_parsed(kpi_updates=[{"key": "maths_syllabus", "value": 91}]))
        assert main._kpi_state["maths_syllabus"]["value"] == 91
        change = next(c for c in data["updates"]["changes"] if c["table"] == "kpi_snapshots")
        client.post(f"/changes/{change['id']}/undo")
        assert main._kpi_state["maths_syllabus"]["value"] == before_value

    def test_parse_is_atomic_on_failure(self):
        """A failure mid-way rolls back everything written earlier in the same parse."""
        import main
        payload = _parsed(
            todos_add=[{"text": "first"}],
            revenue_updates=[{"amount": "not-a-number", "source": "x", "client": None}],
        )
        with patch("main.get_parse_response", return_value=payload):
            with pytest.raises(Exception):
                client.post("/parse", json={"text": "x"})
        db = _db()
        assert db.query(models.Todo).count() == 0
        assert db.query(models.ChangeLog).count() == 0
        db.close()

    def test_list_changes_endpoint(self):
        _parse(_parsed(todos_add=[{"text": "A"}]))
        r = client.get("/changes")
        assert r.status_code == 200
        assert r.json()[0]["table"] == "todos" and r.json()[0]["source"] == "parser"


# ════════════════════════════════════════════════════════════════
# 2. Assistant turns carry changes + undo
# ════════════════════════════════════════════════════════════════

class TestAssistantChanges:

    def test_final_event_and_saved_message_carry_changes(self):
        rounds = [_tool_round("add_todos", {"items": ["Review calculus"]}), _final_round("Added.")]
        events = _orchestrate("add Review calculus to my stack", rounds)
        final = events[-1]
        assert final["changes"] and final["changes"][0]["table"] == "todos"
        assert final["changes"][0]["action"] == "create"
        db = _db()
        last = conversation.thread_rows(db)[-1]
        rows = db.query(models.ChangeLog).all()
        db.close()
        saved = json.loads(last.changes_json)
        assert saved[0]["id"] == final["changes"][0]["id"]
        assert rows[0].source == "assistant"

    def test_one_turn_is_one_batch_and_batch_undo_works(self):
        rounds = [
            _tool_round("add_todos", {"items": ["A", "B"]}, call_id="c1"),
            _tool_round("remember", {"text": "Likes mornings", "kind": "preference"}, call_id="c2"),
            _final_round("Done."),
        ]
        events = _orchestrate("add A and B; I like mornings", rounds)
        changes = events[-1]["changes"]
        assert len(changes) == 3
        batch_ids = {c["batch_id"] for c in changes}
        assert len(batch_ids) == 1
        r = client.post(f"/changes/batch/{batch_ids.pop()}/undo")
        assert r.json()["undone"] == 3
        db = _db()
        assert db.query(models.Todo).count() == 0
        assert db.query(models.AIMemory).count() == 0
        db.close()


# ════════════════════════════════════════════════════════════════
# 3. Deletes need confirmation
# ════════════════════════════════════════════════════════════════

class TestDeleteConfirmation:

    def _todo(self):
        db = _db()
        t = models.Todo(text="Old task", priority=5, category="personal", due=str(TODAY), source="manual")
        db.add(t)
        db.commit()
        tid = t.id
        db.close()
        return tid

    def test_propose_deletes_nothing(self):
        tid = self._todo()
        rounds = [_tool_round("propose_delete", {"table": "todos", "record_id": tid}),
                  _final_round("Delete 'Old task'? Say yes to confirm.")]
        events = _orchestrate("delete the old task", rounds)
        tr = next(e for e in events if e["type"] == "tool_result")
        assert "awaiting confirmation" in tr["summary"]
        assert events[-1]["changes"] == []
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == tid).count() == 1
        p = db.query(models.PendingAction).first()
        db.close()
        assert p.status == "pending" and p.record_id == tid

    def test_confirm_executes_and_is_undoable(self):
        tid = self._todo()
        db = _db()
        pending = actions.propose_delete(db, "todos", tid, "user asked")
        db.close()
        rounds = [_tool_round("confirm_action", {"pending_id": pending["id"]}), _final_round("Deleted.")]
        events = _orchestrate("yes", rounds)
        assert events[-1]["changes"][0]["action"] == "delete"
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == tid).count() == 0
        assert db.query(models.PendingAction).first().status == "confirmed"
        db.close()
        client.post(f"/changes/{events[-1]['changes'][0]['id']}/undo")
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == tid).first().text == "Old task"
        db.close()

    def test_confirm_twice_is_a_no_op(self):
        tid = self._todo()
        db = _db()
        pending = actions.propose_delete(db, "todos", tid)
        first = actions.confirm_action(db, pending["id"])
        second = actions.confirm_action(db, pending["id"])
        db.close()
        assert first["executed"] is True and second["executed"] is False

    def test_cancel_and_endpoints(self):
        tid = self._todo()
        db = _db()
        pending = actions.propose_delete(db, "todos", tid)
        db.close()
        assert client.get("/actions/pending").json()[0]["id"] == pending["id"]
        r = client.post(f"/actions/{pending['id']}/cancel")
        assert r.json()["status"] == "cancelled"
        assert client.get("/actions/pending").json() == []
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == tid).count() == 1
        db.close()

    def test_confirm_endpoint_one_tap(self):
        tid = self._todo()
        db = _db()
        pending = actions.propose_delete(db, "todos", tid)
        db.close()
        r = client.post(f"/actions/{pending['id']}/confirm")
        assert r.json()["executed"] is True
        db = _db()
        assert db.query(models.Todo).count() == 0
        db.close()

    def test_unsupported_table_is_a_tool_error(self):
        db = _db()
        result, summary = ot.run_tool("propose_delete", {"table": "annual_targets", "record_id": 1}, db)
        db.close()
        assert "error" in result


# ════════════════════════════════════════════════════════════════
# 4. Intent classification
# ════════════════════════════════════════════════════════════════

class TestIntent:

    def test_idea_goes_to_inbox_not_tasks(self):
        data = _parse(_parsed(todos_add=[{
            "text": "Start a newsletter", "category": "business",
            "intent": "idea", "confidence": 0.9,
        }]), text="Maybe I should start a newsletter someday")
        assert data["updates"]["todos_added"] == []
        assert data["updates"]["ideas_inboxed"] == ["Start a newsletter"]
        db = _db()
        assert db.query(models.Todo).count() == 0
        inbox = notes.find_note(db, "Inbox", "inbox")
        db.close()
        assert inbox is not None and "Start a newsletter" in inbox.body
        assert any(c["table"] == "notes" for c in data["updates"]["changes"])

    def test_inbox_appends_not_duplicates(self):
        _parse(_parsed(todos_add=[{"text": "Idea one", "intent": "idea", "confidence": 0.95}]))
        _parse(_parsed(todos_add=[{"text": "Idea two", "intent": "idea", "confidence": 0.95}]))
        db = _db()
        inboxes = db.query(models.Note).filter(models.Note.kind == "inbox").all()
        db.close()
        assert len(inboxes) == 1
        assert "Idea one" in inboxes[0].body and "Idea two" in inboxes[0].body

    def test_discussion_writes_nothing(self):
        data = _parse(_parsed(todos_add=[{"text": "Thinking about pricing", "intent": "discussion", "confidence": 0.8}]))
        assert data["updates"]["discussion_skipped"] == ["Thinking about pricing"]
        db = _db()
        assert db.query(models.Todo).count() == 0 and db.query(models.Note).count() == 0
        assert db.query(models.ChangeLog).count() == 0
        db.close()

    def test_commitment_writes_task(self):
        data = _parse(_parsed(todos_add=[{"text": "Finish the essay", "intent": "commitment", "confidence": 0.95}]))
        assert data["updates"]["todos_added"] == ["Finish the essay"]

    def test_legacy_payload_without_intent_is_a_commitment(self):
        data = _parse(_parsed(todos_add=[{"text": "No intent field"}]))
        assert data["updates"]["todos_added"] == ["No intent field"]

    def test_low_confidence_becomes_suggestion_and_accept_writes(self):
        data = _parse(_parsed(todos_add=[{"text": "Call the printer", "intent": "commitment", "confidence": 0.4}]))
        assert data["updates"]["todos_added"] == []
        s = data["updates"]["suggestions"][0]
        assert s["kind"] == "todo_add" and s["status"] == "pending"
        db = _db()
        assert db.query(models.Todo).count() == 0
        db.close()
        listed = client.get("/suggestions").json()
        assert listed[0]["id"] == s["id"] and listed[0]["text"] == "Call the printer"
        r = client.post(f"/suggestions/{s['id']}/accept")
        assert r.status_code == 200 and r.json()["applied"] is True
        assert r.json()["changes"][0]["table"] == "todos"
        db = _db()
        assert db.query(models.Todo).first().text == "Call the printer"
        assert db.query(models.ChangeLog).first().source == "ui"
        db.close()
        assert client.get("/suggestions").json() == []
        assert client.post(f"/suggestions/{s['id']}/accept").json()["applied"] is False

    def test_low_confidence_idea_suggests_inbox(self):
        data = _parse(_parsed(todos_add=[{"text": "Maybe a podcast", "intent": "idea", "confidence": 0.3}]))
        s = data["updates"]["suggestions"][0]
        assert s["kind"] == "inbox_add"
        client.post(f"/suggestions/{s['id']}/accept")
        db = _db()
        assert "Maybe a podcast" in notes.find_note(db, "Inbox", "inbox").body
        db.close()

    def test_dismiss(self):
        data = _parse(_parsed(todos_add=[{"text": "Call", "intent": "commitment", "confidence": 0.2}]))
        sid = data["updates"]["suggestions"][0]["id"]
        assert client.post(f"/suggestions/{sid}/dismiss").json()["status"] == "dismissed"
        db = _db()
        assert db.query(models.Todo).count() == 0
        db.close()

    def test_threshold_env(self, monkeypatch):
        monkeypatch.setenv("INTENT_CONFIDENCE_MIN", "0.95")
        data = _parse(_parsed(todos_add=[{"text": "Call", "intent": "commitment", "confidence": 0.9}]))
        assert data["updates"]["suggestions"] and data["updates"]["todos_added"] == []
