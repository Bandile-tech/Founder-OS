"""
Phase 6 — Orchestrator test suite.

Three layers, all with OpenAI mocked (no real API calls):
  1. Tool-level  — each of the six tools in isolation
  2. Integration — the full streaming loop, asserting routing + gate refusal
  3. SSE         — event ordering guarantees

The OpenAI streaming layer is isolated behind ``openai_client._stream_completion`` so
tests inject canned deltas instead of constructing SDK objects.
"""

import sys
import json
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client


def _db():
    return TestSessionLocal()


TODAY = date.today()


# ════════════════════════════════════════════════════════════════
# 1. TOOL-LEVEL
# ════════════════════════════════════════════════════════════════

def _fake_parse_habits(*keys) -> dict:
    return {
        "summary": "logged",
        "kpi_updates": [],
        "todos_add": [],
        "todos_complete": [],
        "roadmap_complete": [],
        "habits_done": list(keys),
        "annual_updates": [],
        "reading_updates": [],
        "revenue_updates": [],
        "log_entry": None,
        "advisory": None,
    }


class TestRouteBrainDump:

    def test_scripture_today_toggles_habit(self):
        db = _db()
        db.add(models.Habit(key="scripture_prayer", label="Scripture & Prayer",
                            date=TODAY, done=False))
        db.commit()
        db.close()

        with patch("main.get_parse_response", return_value=_fake_parse_habits("scripture_prayer")):
            db = _db()
            result, summary = ot.route_brain_dump(db, "did scripture today")
            db.close()

        assert any(h["key"] == "scripture_prayer" for h in result["updates"]["habits_updated"])

        db = _db()
        habit = db.query(models.Habit).filter(models.Habit.key == "scripture_prayer").first()
        assert habit.done is True
        db.close()


class TestDashboardState:

    def test_returns_gate_status_field(self):
        db = _db()
        state, summary = ot.get_dashboard_state(db)
        db.close()
        assert "gate_status" in state
        assert state["gate_status"] in ("LOCKED", "CLEARED")
        assert "habits" in state and "done" in state["habits"] and "total" in state["habits"]
        assert "ten_k_progress" in state


class TestDetectOffTrack:

    def test_empty_revenue_after_start_is_critical(self):
        db = _db()
        alerts, summary = ot.detect_off_track(db)
        db.close()
        crit = [a for a in alerts if a["rule_id"] == "no_revenue_30d"]
        assert len(crit) == 1
        assert crit[0]["severity"] == "critical"

    def test_disabled_rule_not_returned(self):
        rules = ot._load_rules()
        for r in rules:
            if r["id"] == "no_revenue_30d":
                r["enabled"] = False
        with patch("orchestrator_tools._load_rules", return_value=rules):
            db = _db()
            alerts, summary = ot.detect_off_track(db)
            db.close()
        assert all(a["rule_id"] != "no_revenue_30d" for a in alerts)


class TestWeeklyReview:

    def test_has_all_four_sections(self):
        db = _db()
        result, summary = ot.synthesize_weekly_review(db, str(TODAY - timedelta(days=TODAY.weekday())))
        db.close()
        for section in ("SHIPPED", "REVENUE", "NEGOTIATED", "NEXT"):
            assert section in result


class TestWeakestSubtopic:

    def test_higher_topic_weight_surfaces_first(self):
        db = _db()
        subj = models.Subject(name="Maths 9709", code="9709-test")
        db.add(subj)
        db.flush()
        low = models.Topic(subject_id=subj.id, name="Low weight", syllabus_weight=5)
        high = models.Topic(subject_id=subj.id, name="High weight", syllabus_weight=9)
        db.add_all([low, high])
        db.flush()
        db.add(models.Subtopic(topic_id=low.id, name="Low ST", mastery_level=0))
        db.add(models.Subtopic(topic_id=high.id, name="High ST", mastery_level=0))
        db.commit()

        result, summary = ot.surface_weakest_subtopic(db, limit=2)
        db.close()
        assert result["subtopics"][0]["topic_weight"] == 9
        assert result["subtopics"][0]["name"] == "High ST"


# ════════════════════════════════════════════════════════════════
# 2/3. INTEGRATION + SSE  (orchestrator loop, OpenAI mocked)
# ════════════════════════════════════════════════════════════════

import openai_client


def _scripted_stream(rounds):
    """Build a fake _stream_completion that returns one scripted round per call."""
    state = {"i": 0}

    def _fake(convo, tools):
        i = state["i"]
        state["i"] += 1
        for delta in rounds[i]:
            yield delta

    return _fake


def _tool_round(tool, args, call_id="call_0"):
    return [
        {"tool_calls": [{
            "index": 0, "id": call_id, "name": tool, "arguments": json.dumps(args),
        }]},
        {"finish_reason": "tool_calls"},
    ]


def _final_round(text):
    return [{"content": text}, {"finish_reason": "stop"}]


def _collect(rounds, text):
    """Run the orchestrator loop with a scripted stream; return the list of events."""
    with patch("openai_client._stream_completion", _scripted_stream(rounds)):
        db = _db()
        events = list(openai_client.get_orchestrator_response(
            [{"role": "user", "content": text}], db
        ))
        db.close()
    return events


class TestIntegrationRouting:

    def test_logging_calls_route_brain_dump(self):
        rounds = [
            _tool_round("route_brain_dump", {"text": "logged 50 push-ups"}),
            _final_round("Logged."),
        ]
        with patch("main.get_parse_response", return_value=_fake_parse_habits()) as parse:
            events = _collect(rounds, "logged 50 push-ups")
        assert parse.called  # the existing parse pipeline ran
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert any(e["tool"] == "route_brain_dump" for e in tool_calls)

    def test_question_does_not_call_route_brain_dump(self):
        rounds = [
            _tool_round("get_dashboard_state", {}),
            _final_round("Focus on revenue."),
        ]
        with patch("main.get_parse_response") as parse:
            events = _collect(rounds, "what should I focus on")
        assert not parse.called
        tools = [e["tool"] for e in events if e["type"] == "tool_call"]
        assert "route_brain_dump" not in tools

    def test_gate_locked_refusal_no_db_write(self):
        # Model refuses outright — no tool calls at all.
        rounds = [_final_round("The gate is LOCKED. I can't log a live trade.")]
        with patch("main.get_parse_response") as parse:
            events = _collect(rounds, "log live trade +2R")
        assert not parse.called
        assert all(e["type"] != "tool_call" for e in events)
        db = _db()
        assert db.query(models.LiveTrade).count() == 0
        assert db.query(models.BacktestTrade).count() == 0
        db.close()

    def test_study_question_dashboard_then_weakest(self):
        rounds = [
            _tool_round("get_dashboard_state", {}, call_id="c1"),
            _tool_round("surface_weakest_subtopic", {"limit": 1}, call_id="c2"),
            _final_round("Study your weakest subtopic."),
        ]
        events = _collect(rounds, "what should I study tonight")
        called = [e["tool"] for e in events if e["type"] == "tool_call"]
        assert called == ["get_dashboard_state", "surface_weakest_subtopic"]


class TestSSEOrdering:

    def test_reasoning_precedes_final(self):
        rounds = [[{"content": "Thinking..."}, {"finish_reason": "stop"}]]
        events = _collect(rounds, "hi")
        types = [e["type"] for e in events]
        last_reasoning = max(i for i, t in enumerate(types) if t == "reasoning")
        final_idx = types.index("final")
        assert last_reasoning < final_idx

    def test_each_tool_call_followed_by_tool_result(self):
        rounds = [
            _tool_round("get_dashboard_state", {}),
            _final_round("Done."),
        ]
        events = _collect(rounds, "status")
        for i, e in enumerate(events):
            if e["type"] == "tool_call":
                nxt = events[i + 1]
                assert nxt["type"] == "tool_result"
                assert nxt["tool"] == e["tool"]


class TestAlertsEndpoint:

    def test_alerts_endpoint_returns_json_array(self):
        r = client.get("/orchestrator/alerts")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ════════════════════════════════════════════════════════════════
# Tool 7 — add_todos
# ════════════════════════════════════════════════════════════════

class TestAddTodos:

    def test_add_todos_creates_rows(self):
        db = _db()
        before = db.query(models.Todo).count()
        result, summary = ot.add_todos(db, items=["Task A", "Task B", "Task C"])
        db.close()

        assert result["created"] == 3
        assert len(result["todos"]) == 3

        db = _db()
        after = db.query(models.Todo).count()
        assert after == before + 3
        rows = db.query(models.Todo).filter(
            models.Todo.source == "orchestrator"
        ).order_by(models.Todo.id.desc()).limit(3).all()
        texts = {r.text for r in rows}
        assert texts == {"Task A", "Task B", "Task C"}
        for r in rows:
            assert r.done is False
        db.close()

    def test_add_todos_respects_category_and_priority(self):
        db = _db()
        result, _ = ot.add_todos(db, items=["Study calculus"], category="academics", priority=2)
        todo_id = result["todos"][0]["id"]
        db.close()

        db = _db()
        row = db.query(models.Todo).filter(models.Todo.id == todo_id).first()
        assert row.category == "academics"
        assert row.priority == 2
        assert row.source == "orchestrator"
        assert row.done is False
        db.close()

    def test_orchestrator_refuses_add_todos_on_query(self):
        # Model answers with prose — does NOT call add_todos
        rounds = [
            _tool_round("get_dashboard_state", {}),
            _final_round("Focus on maths tonight — weakest area is calculus."),
        ]
        events = _collect(rounds, "what should I focus on today")
        tools_called = [e["tool"] for e in events if e["type"] == "tool_call"]
        assert "add_todos" not in tools_called

    def test_orchestrator_calls_add_todos_on_explicit_instruction(self):
        # Model calls add_todos with exactly the stated items
        rounds = [
            _tool_round("add_todos", {"items": ["Review calculus", "Read chapter 4", "Sprint drills"]}),
            _final_round("Done — 3 tasks added to your stack."),
        ]
        with patch("orchestrator_tools.add_todos", wraps=ot.add_todos) as mock_add:
            events = _collect(rounds, "add these to my stack: Review calculus, Read chapter 4, Sprint drills")
        tools_called = [e["tool"] for e in events if e["type"] == "tool_call"]
        assert "add_todos" in tools_called
        # Verify the items passed match exactly what the user stated
        add_event = next(e for e in events if e.get("type") == "tool_call" and e["tool"] == "add_todos")
        args = add_event.get("args", {})
        assert set(args.get("items", [])) == {"Review calculus", "Read chapter 4", "Sprint drills"}
