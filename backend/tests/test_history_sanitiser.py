"""
Orphaned tool calls: a turn cut off between an assistant ``tool_calls`` message and its
tool results used to leave the persistent thread permanently invalid, so OpenAI answered
every later message with a 400. These tests pin the repair, the prevention, and the
rule that raw provider errors never reach the screen.

All OpenAI traffic is mocked.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import conversation
import llm
import models
import orchestrator
import orchestrator_tools as ot
from tests.conftest import TestSessionLocal, client
from tests.test_stage1_assistant import (
    _final_round, _post_orchestrator, _scripted_stream, _tool_round,
)

RAW_400 = ("Error code: 400 - {'error': {'message': \"An assistant message with 'tool_calls' "
           "must be followed by tool messages responding to each 'tool_call_id'. "
           "(messages.[35])\"}}")


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


def _db():
    return TestSessionLocal()


def _call(i, name="get_tasks"):
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}


def assert_valid(messages):
    """The invariants OpenAI enforces: every tool_call answered, no stray tool message,
    and the first non-system message is a user message."""
    body = [m for m in messages if m["role"] != "system"]
    if body:
        assert body[0]["role"] == "user", f"window starts with {body[0]['role']}"
    i = 0
    while i < len(messages):
        m = messages[i]
        if m["role"] == "tool":
            raise AssertionError(f"orphaned tool message at {i}")
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            following = messages[i + 1:i + 1 + len(ids)]
            assert [f["role"] for f in following] == ["tool"] * len(ids), f"missing results at {i}"
            assert sorted(f["tool_call_id"] for f in following) == sorted(ids)
            i += 1 + len(ids)
        else:
            i += 1


def _seed(rows):
    """rows: (role, content, tool_calls, tool_call_id)"""
    db = _db()
    for role, content, calls, cid in rows:
        conversation.save_message(db, role, content, tool_calls=calls, tool_call_id=cid)
    db.close()


# ── 1. The sanitiser itself ──────────────────────────────────

class TestSanitiser:

    def test_all_results_missing_get_synthetic_results(self):
        out = conversation.sanitize_history([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [_call(1), _call(2)]},
            {"role": "user", "content": "again"},
        ])
        assert_valid(out)
        synth = [m for m in out if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in synth] == ["call_1", "call_2"]
        assert all("interrupted" in m["content"] for m in synth)

    def test_some_results_missing_keeps_real_one(self):
        out = conversation.sanitize_history([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [_call(1), _call(2)]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
            {"role": "assistant", "content": "done"},
        ])
        assert_valid(out)
        tools = {m["tool_call_id"]: m["content"] for m in out if m["role"] == "tool"}
        assert tools["call_1"] == '{"ok": true}'
        assert "interrupted" in tools["call_2"]

    def test_leading_orphan_tool_and_assistant_are_dropped(self):
        out = conversation.sanitize_history([
            {"role": "tool", "tool_call_id": "call_9", "content": "x"},
            {"role": "assistant", "content": "late reply"},
            {"role": "user", "content": "hello"},
        ])
        assert out == [{"role": "user", "content": "hello"}]

    def test_tool_message_with_no_preceding_tool_calls_dropped(self):
        out = conversation.sanitize_history([
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "call_9", "content": "x"},
            {"role": "assistant", "content": "ok"},
        ])
        assert_valid(out)
        assert [m["role"] for m in out] == ["user", "assistant"]

    def test_valid_history_is_unchanged_and_input_not_mutated(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [_call(1)]},
            {"role": "tool", "tool_call_id": "call_1", "content": "r"},
            {"role": "assistant", "content": "done"},
        ]
        before = json.dumps(msgs)
        assert conversation.sanitize_history(msgs) == msgs
        assert json.dumps(msgs) == before

    def test_late_result_separated_from_its_call_is_repaired(self):
        """A voice tool row can land after typed rows; adjacency is what OpenAI checks."""
        out = conversation.sanitize_history([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [_call(1)]},
            {"role": "user", "content": "typed in between"},
            {"role": "tool", "tool_call_id": "call_1", "content": "late"},
        ])
        assert_valid(out)


# ── 2. The persisted thread ──────────────────────────────────

class TestBrokenThread:

    def test_thread_with_unanswered_tool_calls_builds_valid_history(self):
        _seed([
            ("user", "plan my day", None, None),
            ("assistant", "", [_call(1), _call(2)], None),       # turn cut off here
            ("user", "hello?", None, None),
            ("assistant", "hi", None, None),
        ])
        db = _db()
        history, _ = conversation.build_history(db, refresh_summary=False)
        db.close()
        assert_valid(history)

    def test_window_beginning_with_orphaned_tool_message(self):
        _seed([
            ("tool", "{}", None, "call_old"),
            ("assistant", "stray", None, None),
            ("user", "hello", None, None),
        ])
        db = _db()
        history, _ = conversation.build_history(db, refresh_summary=False)
        db.close()
        assert_valid(history)
        assert history[0]["role"] == "user"

    def test_voice_session_rows_are_repaired_too(self):
        db = _db()
        conversation.save_message(db, "user", "open my brief")
        conversation.save_message(db, "assistant", "", session_id="voice:abc", tool_calls=[_call(7)])
        conversation.save_message(db, "assistant", "Here you go", session_id="voice:abc")
        db.close()
        db = _db()
        history, _ = conversation.build_history(db, refresh_summary=False)
        db.close()
        assert_valid(history)

    def test_typed_message_after_broken_thread_reaches_model_valid(self):
        _seed([
            ("user", "plan my day", None, None),
            ("assistant", "", [_call(1)], None),
        ])
        captured = []
        events = _post_orchestrator("are you there?", captured=captured)
        assert events[-1]["type"] == "final"
        assert_valid(captured[0])

    def test_sanitiser_runs_before_every_model_call_in_the_loop(self):
        """Even if history handed in is broken, round 2 of a tool loop must be valid."""
        captured = []
        broken = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": None, "tool_calls": [_call(5)]},
            {"role": "user", "content": "now"},
        ]
        import openai_client
        rounds = [_tool_round("get_tasks", {}, call_id="call_new"), _final_round("ok")]
        with patch("openai_client._stream_completion", _scripted_stream(rounds, captured)):
            list(openai_client.get_orchestrator_response(broken, _db()))
        assert len(captured) == 2
        for convo in captured:
            assert_valid(convo)


# ── 3. Prevention: close open tool calls ─────────────────────

class _Req:
    text = "do the thing"
    session_id = "s1"
    context = None


def _roles(db):
    return [(r.role, r.tool_call_id) for r in conversation.thread_rows(db)]


class TestInterruptedTurns:

    def test_cancelled_mid_tool_loop_closes_open_calls(self):
        """Client disconnect closes the generator right after the tool call is announced."""
        db = _db()
        rounds = [_tool_round("get_tasks", {}, call_id="call_a"), _final_round("never")]
        with patch("openai_client._stream_completion", _scripted_stream(rounds)):
            gen = orchestrator.orchestrator_stream(_Req(), db)
            for frame in gen:
                if '"tool_call"' in frame:
                    break
            gen.close()
        rows = conversation.thread_rows(db)
        db.close()
        results = [r for r in rows if r.role == "tool"]
        assert [r.tool_call_id for r in results] == ["call_a"]
        assert "interrupted" in results[0].content
        history = [conversation.row_to_message(r) for r in rows]
        assert_valid(history)

    def test_tool_crash_closes_calls_and_shows_friendly_error(self):
        db = _db()
        rounds = [_tool_round("get_tasks", {}, call_id="call_b")]
        with patch("openai_client._stream_completion", _scripted_stream(rounds)), \
                patch.object(ot, "run_tool", side_effect=RuntimeError("secret internals")):
            frames = list(orchestrator.orchestrator_stream(_Req(), db))
        text = "".join(frames)
        assert "secret internals" not in text
        rows = conversation.thread_rows(db)
        db.close()
        assert_valid([conversation.row_to_message(r) for r in rows])

    def test_voice_tool_bridge_closes_call_when_tool_crashes(self):
        _seed([("user", "spoken request", None, None)])
        with patch.object(ot, "run_tool", side_effect=RuntimeError("boom")):
            try:
                client.post("/voice/realtime/tool",
                            json={"name": "get_tasks", "arguments": "{}", "call_id": "v1"})
            except RuntimeError:
                pass
        db = _db()
        rows = conversation.thread_rows(db)
        db.close()
        assert_valid([conversation.row_to_message(r) for r in rows])


# ── 4. Raw provider errors never reach the screen ────────────

class TestFriendlyErrors:

    def test_provider_400_is_logged_not_shown(self, capsys):
        def boom(convo, tools):
            raise RuntimeError(RAW_400)
            yield  # pragma: no cover

        with patch("openai_client._stream_completion", boom):
            r = client.post("/orchestrator", json={"text": "hi", "session_id": "s"})
        assert "tool_call_id" not in r.text and "messages.[35]" not in r.text
        assert "RuntimeError" not in r.text
        events = [json.loads(l[5:]) for l in r.text.split("\n") if l.startswith("data:")]
        assert events[-1]["type"] == "final" and events[-1]["content"]
        assert "tool_call_id" in capsys.readouterr().out      # detail kept in the logs
        db = _db()
        saved = [m.content for m in conversation.thread_rows(db) if m.role == "assistant"]
        db.close()
        assert all("tool_call_id" not in c for c in saved)

    def test_voice_session_error_is_friendly(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        class _R:
            status_code = 400
            text = "raw"
            def json(self):
                return {"error": {"message": "Unknown model: gpt-realtime-mini"}}

        with patch("voice_realtime.httpx.post", return_value=_R()):
            r = client.post("/voice/realtime/session")
        assert r.status_code == 502
        assert "Unknown model" not in r.json()["detail"]
