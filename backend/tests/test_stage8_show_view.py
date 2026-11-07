"""Stage 8 — show_view tool and the UI directive in the orchestrator stream (OpenAI mocked)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
import llm
import notes
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client


def _db():
    return TestSessionLocal()


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield


def _scripted(rounds):
    state = {"i": 0}
    def _fake(convo, tools):
        i = min(state["i"], len(rounds) - 1); state["i"] += 1
        yield from rounds[i]
    return _fake


def _tool_round(tool, args, call_id="c0"):
    return [{"tool_calls": [{"index": 0, "id": call_id, "name": tool, "arguments": json.dumps(args)}]},
            {"finish_reason": "tool_calls"}]


def _final(text):
    return [{"content": text}, {"finish_reason": "stop"}]


def _orchestrate(text, rounds):
    with patch("openai_client._stream_completion", _scripted(rounds)):
        r = client.post("/orchestrator", json={"text": text, "session_id": "s"})
    assert r.status_code == 200
    return [json.loads(l[5:]) for l in r.text.split("\n") if l.startswith("data:")]


class TestShowViewTool:

    def test_whitelist_and_param_filtering(self):
        db = _db()
        db.add(models.Subject(name="Maths 9709", code="9709"))
        db.commit()
        result, summary = ot.run_tool("show_view", {"view": "syllabus", "params": {"paper": "s 1", "subject": "maths", "evil": "<script>"}}, db)
        db.close()
        assert result["ui_directive"] == {"view": "syllabus", "params": {"paper": "S1", "subject": "9709", "subject_name": "Maths 9709"}}
        assert "evil" not in json.dumps(result)
        assert "S1" in summary

    def test_unknown_view_is_an_error(self):
        db = _db()
        result, _ = ot.run_tool("show_view", {"view": "admin_panel"}, db)
        db.close()
        assert "error" in result and "ui_directive" not in result

    def test_aliases_and_close(self):
        db = _db()
        r1, _ = ot.run_tool("show_view", {"view": "home"}, db)
        r2, _ = ot.run_tool("show_view", {"view": "Today's Brief"}, db)
        r3, _ = ot.run_tool("show_view", {"view": "deadlines", "params": {"days": "999"}}, db)
        r4, _ = ot.run_tool("show_view", {"view": "changes", "limit": 5}, db)   # flat params accepted
        db.close()
        assert r1["ui_directive"]["view"] == "close" and r1["shown"] is False
        assert r2["ui_directive"]["view"] == "brief"
        assert r3["ui_directive"]["params"]["days"] == 90
        assert r4["ui_directive"]["params"]["limit"] == 5

    def test_note_resolves_to_id(self):
        db = _db()
        n = notes.create_note(db, "Deep Work", "book", "chapter notes")
        db.commit()
        result, _ = ot.run_tool("show_view", {"view": "note", "params": {"title": "deep work"}}, db)
        missing, _ = ot.run_tool("show_view", {"view": "note", "params": {"title": "nothing like this"}}, db)
        db.close()
        assert result["ui_directive"]["params"] == {"note_id": n.id, "title": "Deep Work"}
        assert "error" in missing

    def test_unknown_subject_is_an_error(self):
        db = _db()
        result, _ = ot.run_tool("show_view", {"view": "syllabus", "params": {"subject": "Latin"}}, db)
        db.close()
        assert "error" in result


class TestDirectiveInStream:

    def test_ui_event_emitted_after_tool_result(self):
        rounds = [_tool_round("show_view", {"view": "syllabus", "params": {"paper": "S1"}}), _final("Here are your S1 topics.")]
        events = _orchestrate("show me my S1 topics", rounds)
        types = [e["type"] for e in events]
        assert "ui" in types
        ui = events[types.index("ui")]
        assert ui["directive"] == {"view": "syllabus", "params": {"paper": "S1"}}
        assert types.index("tool_result") < types.index("ui") < types.index("final")
        # no HTML anywhere in the directive
        assert "<" not in json.dumps(ui)

    def test_no_ui_event_for_other_tools(self):
        events = _orchestrate("status", [_tool_round("get_dashboard_state", {}), _final("ok")])
        assert all(e["type"] != "ui" for e in events)

    def test_close_directive(self):
        events = _orchestrate("close", [_tool_round("show_view", {"view": "close"}), _final("Closed.")])
        assert any(e["type"] == "ui" and e["directive"]["view"] == "close" for e in events)

    def test_voice_bridge_returns_directive(self):
        r = client.post("/voice/realtime/tool", json={"name": "show_view", "arguments": {"view": "brief"}})
        assert r.status_code == 200 and r.json()["ui_directive"] == {"view": "brief", "params": {}}
        r = client.post("/voice/realtime/tool", json={"name": "get_deadlines", "arguments": {}})
        assert r.json()["ui_directive"] is None


class TestDeadlinesEndpoint:

    def test_shape(self):
        from datetime import date
        db = _db()
        db.add(models.Todo(text="Overdue", priority=1, category="x", due="2026-01-01", due_date=date(2026, 1, 1), source="m"))
        db.commit(); db.close()
        d = client.get("/deadlines", params={"days": 7}).json()
        assert d["window_days"] == 7 and [t["text"] for t in d["overdue"]] == ["Overdue"]
        for key in ("due_today", "upcoming", "exams", "undated_count"):
            assert key in d
