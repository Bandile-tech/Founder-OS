"""
Rebuild Stage 1 — assistant reliability.

All OpenAI traffic is mocked. The suite must pass with no OPENAI_API_KEY set.

Covers:
  * the app and non-AI endpoints work with no key; AI endpoints return 503
  * a failing tool returns a structured error and the reply still completes and saves
  * tool calls and tool results persist in the conversation and are replayed
  * one server-side default thread shared across browser session ids
  * the history budget (verbatim window + rolling summary)
  * long-term memory: remember tool + recall injected as context
  * model configuration via ASSISTANT_MODEL
  * llm usage logging and GET /usage/summary
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
import llm
import conversation
import memory_service
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client


def _db():
    return TestSessionLocal()


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """Every test here starts with no API key and a fresh (unbuilt) client."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ASSISTANT_MODEL", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_MODEL", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


# ── Scripted streaming helpers ───────────────────────────────

def _scripted_stream(rounds, captured=None):
    state = {"i": 0}

    def _fake(convo, tools):
        if captured is not None:
            captured.append([dict(m) for m in convo])
        i = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        for delta in rounds[i]:
            yield delta

    return _fake


def _tool_round(tool, args, call_id="call_0"):
    return [
        {"tool_calls": [{"index": 0, "id": call_id, "name": tool, "arguments": json.dumps(args)}]},
        {"finish_reason": "tool_calls"},
    ]


def _final_round(text):
    return [{"content": text}, {"finish_reason": "stop"}]


def _post_orchestrator(text, session_id="sess-A", rounds=None, captured=None):
    rounds = rounds or [_final_round("ok")]
    with patch("openai_client._stream_completion", _scripted_stream(rounds, captured)):
        r = client.post("/orchestrator", json={"text": text, "session_id": session_id})
    assert r.status_code == 200
    events = []
    for frame in r.text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


# ════════════════════════════════════════════════════════════════
# 1. Boot and 503s with no key
# ════════════════════════════════════════════════════════════════

class TestNoKey:

    def test_non_ai_endpoints_work(self):
        assert client.get("/habits").status_code == 200
        h = client.get("/health").json()
        assert h["ok"] is True
        assert h["ai_configured"] is False

    def test_startup_handler_runs_with_no_key(self):
        """The real startup chain (migrations + schema validation) must not need a key."""
        import main
        asyncio.run(main.startup())

    def test_chat_returns_503(self):
        r = client.post("/chat", json={"message": "hello", "session_id": "s1"})
        assert r.status_code == 503
        assert "OPENAI_API_KEY" in r.json()["detail"]

    def test_parse_returns_503_and_writes_nothing(self):
        db = _db()
        before = db.query(models.Todo).count()
        db.close()
        r = client.post("/parse", json={"text": "add task: buy milk"})
        assert r.status_code == 503
        db = _db()
        assert db.query(models.Todo).count() == before
        db.close()

    def test_voice_returns_503(self):
        r = client.post("/voice/speak", json={"text": "hello"})
        assert r.status_code == 503

    def test_orchestrator_stream_degrades_gracefully(self):
        """No scripted stream here: the real wrapper raises AIUnavailable."""
        r = client.post("/orchestrator", json={"text": "hi", "session_id": "s1"})
        assert r.status_code == 200
        events = [json.loads(l[5:]) for l in r.text.split("\n") if l.startswith("data:")]
        types = [e["type"] for e in events]
        assert "error" in types and types[-1] == "final"
        db = _db()
        rows = conversation.thread_rows(db)
        db.close()
        assert [r.role for r in rows] == ["user", "assistant"]
        assert "not configured" in rows[-1].content

    def test_lazy_client_raises_cleanly(self):
        with pytest.raises(llm.AIUnavailable):
            llm.get_client()


# ════════════════════════════════════════════════════════════════
# 2. Tool failures don't kill the reply
# ════════════════════════════════════════════════════════════════

class TestToolFailure:

    def test_run_tool_returns_structured_error(self):
        def boom(db, a):
            raise RuntimeError("database exploded")
        with patch.dict(ot._TOOL_DISPATCH, {"get_dashboard_state": boom}):
            db = _db()
            result, summary = ot.run_tool("get_dashboard_state", {}, db)
            db.close()
        assert "error" in result
        assert "database exploded" in result["error"]
        assert "failed" in summary

    def test_failing_tool_reply_still_completes_and_saves(self):
        def boom(db, a):
            raise RuntimeError("database exploded")
        rounds = [_tool_round("get_dashboard_state", {}), _final_round("Recovered.")]
        with patch.dict(ot._TOOL_DISPATCH, {"get_dashboard_state": boom}):
            events = _post_orchestrator("status?", rounds=rounds)
        types = [e["type"] for e in events]
        assert types[-1] == "final"
        assert events[-1]["content"] == "Recovered."
        tr = next(e for e in events if e["type"] == "tool_result")
        assert tr["error"] is True

        db = _db()
        rows = conversation.thread_rows(db)
        db.close()
        roles = [r.role for r in rows]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert "database exploded" in rows[2].content
        assert rows[-1].content == "Recovered."

    def test_unknown_tool_is_an_error_not_a_crash(self):
        db = _db()
        result, _ = ot.run_tool("does_not_exist", {}, db)
        db.close()
        assert "unknown tool" in result["error"]


# ════════════════════════════════════════════════════════════════
# 3. Persistence + replay of tool calls
# ════════════════════════════════════════════════════════════════

class TestToolPersistence:

    def test_tool_calls_and_results_persist(self):
        rounds = [_tool_round("get_dashboard_state", {}, call_id="c1"), _final_round("Done.")]
        _post_orchestrator("status", rounds=rounds)
        db = _db()
        rows = conversation.thread_rows(db)
        db.close()
        assert [r.role for r in rows] == ["user", "assistant", "tool", "assistant"]
        calls = json.loads(rows[1].tool_calls_json)
        assert calls[0]["function"]["name"] == "get_dashboard_state"
        assert rows[2].tool_call_id == "c1"
        assert json.loads(rows[2].content)["habits"]["total"] >= 0

    def test_next_turn_sees_previous_tool_result(self):
        rounds = [_tool_round("get_dashboard_state", {}, call_id="c1"), _final_round("Done.")]
        _post_orchestrator("status", rounds=rounds)
        captured = []
        _post_orchestrator("and now?", rounds=[_final_round("Still fine.")], captured=captured)
        convo = captured[0]
        roles = [m["role"] for m in convo]
        assert "tool" in roles
        tool_msg = next(m for m in convo if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "c1"
        assistant_with_calls = next(m for m in convo if m["role"] == "assistant" and m.get("tool_calls"))
        assert assistant_with_calls["tool_calls"][0]["function"]["name"] == "get_dashboard_state"

    def test_long_tool_results_truncated_on_replay(self):
        db = _db()
        conversation.save_message(db, "user", "q")
        conversation.save_message(db, "assistant", "", tool_calls=[{"id": "x", "type": "function",
                                  "function": {"name": "t", "arguments": "{}"}}])
        conversation.save_message(db, "tool", "y" * 5000, tool_call_id="x")
        msgs, _ = conversation.build_history(db)
        db.close()
        tool = next(m for m in msgs if m["role"] == "tool")
        assert len(tool["content"]) < 2000 and tool["content"].endswith("[truncated]")


# ════════════════════════════════════════════════════════════════
# 4. Default thread shared across devices
# ════════════════════════════════════════════════════════════════

class TestDefaultThread:

    def test_two_session_ids_share_history(self):
        _post_orchestrator("remember the phone call", session_id="phone")
        captured = []
        _post_orchestrator("what did I say?", session_id="laptop", captured=captured)
        texts = [m["content"] for m in captured[0] if m["role"] == "user"]
        assert any("remember the phone call" in t for t in texts)
        db = _db()
        rows = conversation.thread_rows(db)
        db.close()
        assert {r.thread_id for r in rows} == {conversation.DEFAULT_THREAD_ID}
        assert {r.session_id for r in rows} == {"phone", "laptop"}

    def test_final_event_carries_changes_list(self):
        events = _post_orchestrator("hi")
        assert isinstance(events[-1]["changes"], list)


# ════════════════════════════════════════════════════════════════
# 5. History budget
# ════════════════════════════════════════════════════════════════

def _seed_turns(db, n):
    for i in range(n):
        conversation.save_message(db, "user", f"user turn {i}", commit=False)
        conversation.save_message(db, "assistant", f"reply {i}", commit=False)
    db.commit()


class TestHistoryBudget:

    def test_window_holds_last_n_turns(self, monkeypatch):
        monkeypatch.setenv("ASSISTANT_HISTORY_TURNS", "5")
        db = _db()
        _seed_turns(db, 12)
        msgs, summary = conversation.build_history(db)
        db.close()
        users = [m for m in msgs if m["role"] == "user"]
        assert len(users) == 5
        assert users[0]["content"] == "user turn 7"
        assert msgs[0]["role"] == "user"           # window never opens on an orphan
        assert summary is None                      # no key → no summary yet

    def test_summary_refreshes_in_batches(self, monkeypatch):
        monkeypatch.setenv("ASSISTANT_HISTORY_TURNS", "5")
        monkeypatch.setenv("ASSISTANT_SUMMARY_BATCH", "10")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")   # configured, but summarise is mocked
        db = _db()
        _seed_turns(db, 12)                                # 14 older messages → batch reached
        with patch("conversation.summarise", return_value="SUMMARY v1") as summ:
            msgs, summary = conversation.build_history(db)
            assert summ.call_count == 1
            assert summary == "SUMMARY v1"
            # Nothing new fell out of the window → no second call
            msgs, summary = conversation.build_history(db)
            assert summ.call_count == 1
            # Three more turns: 6 new older messages < batch of 10 → still no call
            _seed_turns(db, 3)
            msgs, summary = conversation.build_history(db)
            assert summ.call_count == 1
            _seed_turns(db, 2)                            # now 10 pending → refresh
            msgs, summary = conversation.build_history(db)
            assert summ.call_count == 2
        row = db.query(models.ConversationSummary).first()
        assert row.message_count == 24
        db.close()

    def test_summary_lands_in_context_message(self, monkeypatch):
        monkeypatch.setenv("ASSISTANT_HISTORY_TURNS", "2")
        monkeypatch.setenv("ASSISTANT_SUMMARY_BATCH", "2")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        db = _db()
        _seed_turns(db, 6)
        db.close()
        captured = []
        with patch("conversation.summarise", return_value="Earlier we agreed X."):
            _post_orchestrator("next", captured=captured)
        convo = captured[0]
        assert convo[0]["role"] == "system"                       # stable prompt first
        assert "Earlier we agreed X." in convo[1]["content"]       # dynamic context second
        assert convo[1]["role"] == "system"


# ════════════════════════════════════════════════════════════════
# 6. Long-term memory
# ════════════════════════════════════════════════════════════════

class TestMemory:

    def test_remember_tool_writes_row(self):
        db = _db()
        result, summary = ot.run_tool("remember", {
            "text": "Further Maths P1 exam is on 12 May", "kind": "fact", "tags": ["exams"]
        }, db)
        row = db.query(models.AIMemory).filter(models.AIMemory.id == result["id"]).first()
        db.close()
        assert row.context_type == "memory" and row.kind == "fact"
        assert "12 May" in row.context_data and row.tags == "exams"

    def test_recall_ranks_by_overlap(self):
        db = _db()
        memory_service.remember(db, "Prefers evening study blocks after 20:00", kind="preference")
        memory_service.remember(db, "Further Maths exam is on 12 May", kind="fact")
        hits = memory_service.recall(db, "when is my further maths exam?")
        db.close()
        assert hits and "Further Maths" in hits[0].context_data

    def test_memory_injected_into_turn_context(self):
        db = _db()
        memory_service.remember(db, "Further Maths exam is on 12 May", kind="fact")
        db.close()
        captured = []
        _post_orchestrator("when is further maths?", captured=captured)
        convo = captured[0]
        ctx = convo[1]
        assert ctx["role"] == "system" and "[Long-term memory]" in ctx["content"]
        assert "12 May" in ctx["content"]
        assert "12 May" not in convo[0]["content"]   # stable prompt untouched

    def test_remember_schema_registered(self):
        names = [t["function"]["name"] for t in ot.TOOL_SCHEMAS]
        assert "remember" in names


# ════════════════════════════════════════════════════════════════
# 7. Model config + usage
# ════════════════════════════════════════════════════════════════

class TestModelConfigAndUsage:

    def test_default_model(self):
        assert llm.assistant_model() == "gpt-5.4-mini"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ASSISTANT_MODEL", "gpt-5.4")
        assert llm.assistant_model() == "gpt-5.4"
        import openai_client
        assert openai_client.orchestrator_model() == "gpt-5.4"

    def test_legacy_env_still_honoured(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATOR_MODEL", "legacy-model")
        assert llm.assistant_model() == "legacy-model"

    def test_usage_logged_and_summarised(self):
        db = _db()
        llm.log_usage(db, model="gpt-5.4-mini", purpose="assistant",
                      input_tokens=1000, cached_input_tokens=400, output_tokens=200)
        llm.log_usage(db, model="text-embedding-3-small", purpose="embedding", input_tokens=5000)
        db.close()
        r = client.get("/usage/summary")
        assert r.status_code == 200
        d = r.json()
        assert d["total"]["calls"] == 2
        assert d["total"]["input_tokens"] == 6000
        assert d["total"]["estimated_cost_usd"] > 0
        assert len(d["by_day"]) == 1 and len(d["by_month"]) == 1
        assert {m["model"] for m in d["by_model"]} == {"gpt-5.4-mini", "text-embedding-3-small"}

    def test_cost_estimate_uses_cached_rate(self):
        full = llm.estimate_cost("gpt-5.4-mini", 1000, 0, 0)
        cached = llm.estimate_cost("gpt-5.4-mini", 1000, 1000, 0)
        assert cached < full

    def test_weekly_review_no_longer_crashes(self):
        db = _db()
        db.add(models.RoadmapTask(task_id="sprint-acc", roadmap="sprint", done=True))
        db.commit()
        result, _ = ot.synthesize_weekly_review(db)
        db.close()
        assert "sprint-acc" in result["SHIPPED"]
