"""
Phase 7, Stage 1 — the model router.

No network and no keys. Each provider's real SDK client is handed a fake HTTP transport,
so these tests read the request the SDK would send and answer it with a canned stream.

  1. a streamed, tool-calling assistant turn on a mocked OpenAI route
  2. the same turn on a mocked Claude route (and what the request carries: cache_control,
     thinking from the route's parameters, the converted tool result)
  3. fallback: a provider error, a refusal, a missing key; and when it must NOT retry
  4. a route changed by chat, and the change undone
  5. the app with no keys
  6. routes API, the monthly cap, the usage breakdown
"""

import json
from datetime import datetime
from unittest.mock import patch

import httpx
import pytest

import llm
import models
import model_routes
from llm_adapters import anthropic_adapter, openai_adapter
from tests.conftest import TestSessionLocal, client, reload_routes

OPENAI_MINI, OPENAI_BIG = "gpt-5.4-mini", "gpt-5.4"
CLAUDE_SMALL, CLAUDE_MID = "claude-haiku-5-5", "claude-sonnet-5-5"
TOOL_ARGS = {"items": ["Call the bank"]}


def _db():
    return TestSessionLocal()


def _events(response):
    assert response.status_code == 200
    return [json.loads(line[5:]) for line in response.text.split("\n") if line.startswith("data:")]


def _send(text="add a task to call the bank"):
    return _events(client.post("/orchestrator", json={"text": text, "session_id": "s"}))


# ── Fake OpenAI ──────────────────────────────────────────────

class FakeOpenAI:
    """Answers /chat/completions. A streamed request with no tool result yet gets a tool
    call; one that carries a tool result gets the final text."""

    def __init__(self, fail_status=None, refuse=False):
        self.bodies, self.fail_status, self.refuse, self.embedding_calls = [], fail_status, refuse, 0

    def __call__(self, request):
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/embeddings"):       # memory recall embeds the question first
            self.embedding_calls += 1
            n = len(body["input"]) if isinstance(body["input"], list) else 1
            return httpx.Response(200, json={
                "object": "list", "model": body["model"],
                "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(n)],
                "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        self.bodies.append(body)
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": {"message": "upstream is down"}})
        model = body["model"]
        if not body.get("stream"):
            message = {"role": "assistant", "content": None, "refusal": "I can't help with that."} if self.refuse \
                else {"role": "assistant", "content": "plain answer"}
            return httpx.Response(200, json={
                "id": "c", "object": "chat.completion", "created": 0, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220,
                          "prompt_tokens_details": {"cached_tokens": 150}}})

        def chunk(delta, finish=None):
            return {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        if any(m.get("role") == "tool" for m in body["messages"]):
            chunks = [chunk({"role": "assistant", "content": "Added "}), chunk({"content": "it."}), chunk({}, "stop")]
        else:
            args = json.dumps(TOOL_ARGS)
            chunks = [
                chunk({"role": "assistant", "content": None, "tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function", "function": {"name": "add_todos", "arguments": ""}}]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[:9]}}]}),
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[9:]}}]}),
                chunk({}, "tool_calls"),
            ]
        chunks.append({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": model, "choices": [],
                       "usage": {"prompt_tokens": 1000, "completion_tokens": 30, "total_tokens": 1030,
                                 "prompt_tokens_details": {"cached_tokens": 800}}})
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})


@pytest.fixture
def openai_wire(monkeypatch):
    from openai import OpenAI
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake = FakeOpenAI()
    monkeypatch.setattr(openai_adapter, "_client", OpenAI(
        api_key="sk-test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(fake))))
    yield fake
    llm.reset_client()


# ── Fake Claude ──────────────────────────────────────────────

class FakeClaude:
    """Answers /v1/messages the same way: a tool call first, the final text once the
    request carries a tool result."""

    def __init__(self, fail_status=None, refuse=False, think=False):
        self.bodies, self.fail_status, self.refuse, self.think = [], fail_status, refuse, think

    def __call__(self, request):
        import httpx2
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        if self.fail_status:
            return httpx2.Response(self.fail_status, json={"type": "error", "error": {
                "type": "api_error", "message": "upstream is down"}})
        model = body["model"]
        usage = {"input_tokens": 40, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 60,
                 "output_tokens": 1}
        if not body.get("stream"):
            if self.refuse:
                content, stop = [], "refusal"
            else:
                content, stop = [{"type": "text", "text": '```json\n{"answer": "plain"}\n```'}], "end_turn"
            return httpx2.Response(200, json={
                "id": "m", "type": "message", "role": "assistant", "model": model, "content": content,
                "stop_reason": stop, "stop_sequence": None, "usage": {**usage, "output_tokens": 12}})
        has_result = any(isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"])
                         for m in body["messages"])
        events = [{"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": usage}}]
        if self.refuse:
            events.append({"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_sequence": None},
                           "usage": {"output_tokens": 0}})
        elif has_result:
            events += [
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Added "}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "it."}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                 "usage": {"output_tokens": 9}},
            ]
        else:
            args = json.dumps(TOOL_ARGS)
            index = 0
            if self.think:
                events += [
                    {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "A task."}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig=="}},
                    {"type": "content_block_stop", "index": 0},
                ]
                index = 1
            events += [
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "tool_use", "id": "toolu_1", "name": "add_todos", "input": {}}},
                {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": args[:9]}},
                {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": args[9:]}},
                {"type": "content_block_stop", "index": index},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                 "usage": {"output_tokens": 25}},
            ]
        events.append({"type": "message_stop"})
        sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx2.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})


def _claude_client(fake):
    import anthropic
    import httpx2
    return anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                               http_client=httpx2.Client(transport=httpx2.MockTransport(fake)))


@pytest.fixture
def claude_wire(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fake = FakeClaude()
    monkeypatch.setattr(anthropic_adapter, "_client", _claude_client(fake))
    yield fake
    llm.reset_client()


def _route(task, **kwargs):
    db = _db()
    try:
        return model_routes.set_route(db, task, source="ui", **kwargs)
    finally:
        db.close()


def _usage_rows():
    db = _db()
    try:
        return [(u.provider, u.model, u.task, u.purpose, u.fallback_from, u.input_tokens, u.cached_input_tokens,
                 u.output_tokens, u.estimated_cost_usd) for u in db.query(models.LLMUsage).order_by(models.LLMUsage.id)]
    finally:
        db.close()


def _todos():
    db = _db()
    try:
        return [t.text for t in db.query(models.Todo).all()]
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _usage_in_the_test_database():
    # A streamed turn logs its usage with no session of its own.
    with patch("database.SessionLocal", TestSessionLocal):
        yield


# ════════════════════════════════════════════════════════════════
# 1. A streamed, tool-calling turn on OpenAI
# ════════════════════════════════════════════════════════════════

def test_openai_route_completes_a_streamed_tool_calling_turn(openai_wire):
    events = _send()
    types = [e["type"] for e in events]
    assert types.count("tool_call") == 1 and types.count("tool_result") == 1 and types[-1] == "final"
    assert set(types) <= {"reasoning", "tool_call", "tool_result", "ui", "error", "final"}   # the SSE shape is unchanged
    call = next(e for e in events if e["type"] == "tool_call")
    assert call == {"type": "tool_call", "tool": "add_todos", "args": TOOL_ARGS}
    assert "".join(e["content"] for e in events if e["type"] == "reasoning") == "Added it."
    assert events[-1]["content"] == "Added it." and events[-1]["changes"]
    assert _todos() == ["Call the bank"]

    first, second = openai_wire.bodies
    assert first["model"] == OPENAI_MINI and first["stream"] is True
    assert first["messages"][0]["role"] == "system" and first["tools"]
    assert "temperature" not in first and "max_tokens" not in first
    # The stable prefix (system prompt, tool definitions) is identical on both calls of the turn.
    assert first["messages"][0] == second["messages"][0] and first["tools"] == second["tools"]
    assert second["messages"][-1]["role"] == "tool" and second["messages"][-2]["tool_calls"][0]["id"] == "call_1"

    rows = [r for r in _usage_rows() if r[2] == "assistant"]
    assert [(r[0], r[1], r[4]) for r in rows] == [("openai", OPENAI_MINI, None)] * 2
    assert rows[0][5:8] == (1000, 800, 30) and rows[0][8] > 0
    assert openai_wire.embedding_calls == 1                 # memory recall, on the embedding route


# ════════════════════════════════════════════════════════════════
# 2. The same turn on Claude
# ════════════════════════════════════════════════════════════════

def test_claude_route_completes_a_streamed_tool_calling_turn(claude_wire):
    _route("assistant", provider="claude")                 # the counterpart of the seeded model
    events = _send()
    types = [e["type"] for e in events]
    assert types.count("tool_call") == 1 and types.count("tool_result") == 1 and types[-1] == "final"
    assert set(types) <= {"reasoning", "tool_call", "tool_result", "ui", "error", "final"}
    assert next(e for e in events if e["type"] == "tool_call") == {
        "type": "tool_call", "tool": "add_todos", "args": TOOL_ARGS}
    assert events[-1]["content"] == "Added it." and events[-1]["changes"]
    assert _todos() == ["Call the bank"]

    first, second = claude_wire.bodies
    assert first["model"] == CLAUDE_SMALL and first["stream"] is True and first["max_tokens"] > 0
    # Thinking is off, because the route's parameters say so. Nothing else is sent.
    assert first["thinking"] == {"type": "disabled"}
    assert "temperature" not in first and "tool_choice" not in first
    # Caching: the system prompt and the tool list come first, unchanged, each with a breakpoint.
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in first["tools"][:-1])
    assert all("cache_control" not in block for block in first["system"][1:])
    assert first["tools"] == second["tools"] and first["system"][0] == second["system"][0]
    assert {"name", "description", "input_schema"} <= set(first["tools"][0])
    assert first["messages"][-1]["role"] == "user"
    # The second call hands the tool call and its result back in Claude's own shape.
    assistant, result = second["messages"][-2], second["messages"][-1]
    assert assistant["role"] == "assistant" and assistant["content"][-1] == {
        "type": "tool_use", "id": "toolu_1", "name": "add_todos", "input": TOOL_ARGS}
    assert result["role"] == "user" and result["content"][0]["type"] == "tool_result"
    assert result["content"][0]["tool_use_id"] == "toolu_1"

    rows = _usage_rows()
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [("anthropic", CLAUDE_SMALL, "assistant", None)] * 2
    assert rows[0][5:8] == (1000, 900, 25)                 # input counts cache reads and writes; cached = reads
    assert rows[0][8] > 0


def test_claude_thinking_blocks_go_back_unchanged_within_the_turn(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fake = FakeClaude(think=True)
    monkeypatch.setattr(anthropic_adapter, "_client", _claude_client(fake))
    _route("assistant", model=CLAUDE_MID, params={"thinking": {"type": "adaptive"}})
    events = _send()
    assert events[-1]["content"] == "Added it." and _todos() == ["Call the bank"]
    first, second = fake.bodies
    assert first["thinking"] == {"type": "adaptive"}
    assert second["messages"][-2]["content"] == [
        {"type": "thinking", "thinking": "A task.", "signature": "sig=="},
        {"type": "tool_use", "id": "toolu_1", "name": "add_todos", "input": TOOL_ARGS}]
    # It was kept in memory for the turn only: nothing provider-specific is stored.
    db = _db()
    stored = [r for r in db.query(models.ChatMessage).all() if r.role == "assistant"]
    db.close()
    assert stored and all("thinking" not in (r.content or "") for r in stored)


def test_the_parser_runs_on_claude_and_json_comes_back_clean(claude_wire):
    _route("parser", model=CLAUDE_SMALL)
    result = llm.complete("parser", [{"role": "system", "content": "Parse."}, {"role": "user", "content": "x"}],
                          json_mode=True, max_output=1000)
    assert json.loads(result.text) == {"answer": "plain"}          # fences removed
    body = claude_wire.bodies[0]
    assert body["max_tokens"] == 1000 and body["system"][-1]["text"] == anthropic_adapter.JSON_INSTRUCTION
    assert result.provider == "anthropic" and result.cached_input_tokens == 900 and result.cache_write_tokens == 60


def test_history_from_one_provider_is_readable_by_the_other():
    system, messages = anthropic_adapter.convert_messages([
        {"role": "system", "content": "Stable prompt."},
        {"role": "system", "content": "Memory: prefers evenings."},
        {"role": "assistant", "content": "A stray opening line."},
        {"role": "user", "content": "add two tasks"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "add_todos", "arguments": '{"items": ["a"]}'}},
            {"id": "call:b", "type": "function", "function": {"name": "add_todos", "arguments": "not json"}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": '{"added": ["a"]}'},
        {"role": "tool", "tool_call_id": "call:b", "content": ""},
        {"role": "user", "content": "thanks"},
    ])
    assert [b["text"] for b in system] == ["Stable prompt.", "Memory: prefers evenings."]
    assert "cache_control" in system[0] and "cache_control" not in system[1]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert messages[0]["content"][0]["text"].startswith("(earlier")       # Claude needs a user turn first
    assert [b["type"] for b in messages[3]["content"]] == ["tool_use", "tool_use"]
    assert messages[3]["content"][1] == {"type": "tool_use", "id": "call_b", "name": "add_todos", "input": {}}
    # Both results and the next thing Bandile said share one user turn, results first.
    assert [b["type"] for b in messages[4]["content"]] == ["tool_result", "tool_result", "text"]
    assert messages[4]["content"][1] == {"type": "tool_result", "tool_use_id": "call_b", "content": "(empty)"}
    # And OpenAI is never sent a key it does not know.
    assert openai_adapter._messages([{"role": "assistant", "content": "x", "provider_raw": {"a": 1}}]) == [
        {"role": "assistant", "content": "x"}]


# ════════════════════════════════════════════════════════════════
# 3. Fallback
# ════════════════════════════════════════════════════════════════

def _failing_openai(monkeypatch, **kwargs):
    from openai import OpenAI
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake = FakeOpenAI(**kwargs)
    monkeypatch.setattr(openai_adapter, "_client", OpenAI(
        api_key="sk-test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(fake))))
    return fake


def test_a_provider_error_is_retried_once_on_the_other_provider(monkeypatch, claude_wire, capsys):
    broken = _failing_openai(monkeypatch, fail_status=500)
    events = _send()
    assert events[-1]["content"] == "Added it." and "error" not in [e["type"] for e in events]
    assert _todos() == ["Call the bank"]
    assert len(broken.bodies) == 2 and len(claude_wire.bodies) == 2        # each round: one try, one retry
    assert claude_wire.bodies[0]["model"] == CLAUDE_SMALL                  # the matching tier
    assert claude_wire.bodies[0]["thinking"] == {"type": "disabled"}       # with that model's own defaults
    rows = [r for r in _usage_rows() if r[2] == "assistant"]
    assert [(r[0], r[1], r[4]) for r in rows] == [("anthropic", CLAUDE_SMALL, f"openai:{OPENAI_MINI}")] * 2
    assert "[llm] fallback: assistant on openai:gpt-5.4-mini failed" in capsys.readouterr().out
    summary = client.get("/usage/summary").json()
    assert summary["total"]["fallbacks"] == 2


def test_a_refusal_is_retried_on_the_other_provider(monkeypatch, claude_wire):
    _failing_openai(monkeypatch, refuse=True)
    result = llm.complete("briefing_summary", [{"role": "user", "content": "x"}], json_mode=True)
    assert result.provider == "anthropic" and json.loads(result.text) == {"answer": "plain"}
    assert _usage_rows()[0][4] == f"openai:{OPENAI_MINI}"


def test_fallback_follows_FALLBACK_MAP(monkeypatch, claude_wire):
    _failing_openai(monkeypatch, fail_status=503)
    monkeypatch.setenv("FALLBACK_MAP", f"{OPENAI_MINI}={CLAUDE_MID}")
    result = llm.complete("parser", [{"role": "user", "content": "x"}])
    assert (result.provider, result.model) == ("anthropic", CLAUDE_MID)
    assert claude_wire.bodies[0]["thinking"] == {"type": "between_tools"}  # this model's way of switching it off
    monkeypatch.setenv("FALLBACK_MAP", json.dumps({OPENAI_MINI: CLAUDE_SMALL}))
    assert llm.counterpart(OPENAI_MINI) == CLAUDE_SMALL and llm.counterpart(CLAUDE_SMALL) == OPENAI_MINI


def test_no_fallback_without_the_other_key(monkeypatch):
    broken = _failing_openai(monkeypatch, fail_status=500)
    with pytest.raises(Exception) as caught:
        llm.complete("parser", [{"role": "user", "content": "x"}])
    assert not isinstance(caught.value, llm.AIUnavailable) and len(broken.bodies) == 1
    assert _usage_rows() == []


def test_a_route_whose_provider_has_no_key_is_served_by_its_counterpart(openai_wire):
    _route("briefing_summary", model=CLAUDE_SMALL)                         # no ANTHROPIC_API_KEY set
    assert llm.is_configured("briefing_summary")
    result = llm.complete("briefing_summary", [{"role": "user", "content": "x"}])
    assert (result.provider, result.model, result.text) == ("openai", OPENAI_MINI, "plain answer")
    assert _usage_rows()[0][4] == f"anthropic:{CLAUDE_SMALL} (no key)"


def test_a_stream_that_already_sent_text_is_not_retried(monkeypatch, claude_wire):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def dies_midway(model, messages, tools, params, max_output=None):
        yield {"content": "Half an ans"}
        raise RuntimeError("connection reset")

    monkeypatch.setattr(openai_adapter, "stream", dies_midway)
    events = _send("hello")
    assert "error" in [e["type"] for e in events]
    assert claude_wire.bodies == []                                        # the text on screen is not repeated
    assert events[-1]["type"] == "final"


# ════════════════════════════════════════════════════════════════
# 4. Changed by chat, and undone
# ════════════════════════════════════════════════════════════════

def _scripted(rounds):
    state = {"i": 0}

    def fake(convo, tools):
        i = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        yield from rounds[i]

    return fake


def _chat(text, tool, args):
    rounds = [[{"tool_calls": [{"index": 0, "id": "call_0", "name": tool, "arguments": json.dumps(args)}]},
               {"finish_reason": "tool_calls"}],
              [{"content": "Done."}, {"finish_reason": "stop"}]]
    with patch("openai_client._stream_completion", _scripted(rounds)):
        return _events(client.post("/orchestrator", json={"text": text, "session_id": "s"}))


def _assistant_route():
    return next(r for r in client.get("/models/routes").json()["routes"] if r["task"] == "assistant")


def test_use_claude_for_chat_and_undo():
    assert (_assistant_route()["provider"], _assistant_route()["model"]) == ("openai", OPENAI_MINI)
    events = _chat("use Claude for chat", "set_model_route", {"task": "chat", "provider": "claude"})
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["error"] is False and "now uses claude-haiku-5-5 on Claude" in result["summary"]

    route = _assistant_route()
    assert (route["provider"], route["model"], route["provider_label"]) == ("anthropic", CLAUDE_SMALL, "Claude")
    assert route["params"] == {"thinking": {"type": "disabled"}}           # no model named: the counterpart, thinking off
    assert llm.model_for("assistant") == CLAUDE_SMALL                      # the gateway sees it at once

    changes = events[-1]["changes"]
    assert len(changes) == 1 and changes[0]["table"] == "model_routes" and changes[0]["undoable"] is True
    undone = client.post(f"/changes/batch/{changes[0]['batch_id']}/undo").json()
    assert undone["undone"] == 1
    route = _assistant_route()
    assert (route["provider"], route["model"], route["params"]) == ("openai", OPENAI_MINI, {})
    assert llm.model_for("assistant") == OPENAI_MINI


def test_the_tool_names_a_model_switches_everything_and_refuses_nonsense():
    events = _chat("use sonnet for research", "set_model_route", {"task": "market_intel", "model": CLAUDE_MID})
    assert next(e for e in events if e["type"] == "tool_result")["error"] is False
    assert llm.route("market_intel").params == {"thinking": {"type": "between_tools"}}

    db = _db()
    out, summary = model_routes.set_routes(db, "all", provider="claude")
    assert {r["task"] for r in out["routes"]} == set(llm.CHAT_TASKS)
    assert all(llm.route(t).provider == "anthropic" for t in llm.CHAT_TASKS)
    assert llm.route("embedding").provider == "openai"                     # not a text task: untouched
    assert "ANTHROPIC_API_KEY is not set" in out["warning"]

    assert "error" in model_routes.set_routes(db, "assistant", model="gpt-9000")[0]
    assert "error" in model_routes.set_routes(db, "the weather", provider="claude")[0]
    assert "error" in model_routes.set_routes(db, "assistant")[0]
    again, text = model_routes.set_routes(db, "assistant", provider="claude")
    assert "already uses" in text and db.query(models.ChangeLog).filter(
        models.ChangeLog.table_name == "model_routes").count() == 1 + len(llm.CHAT_TASKS) - 1
    db.close()


# ════════════════════════════════════════════════════════════════
# 5. No keys
# ════════════════════════════════════════════════════════════════

def test_the_app_runs_with_no_keys():
    import asyncio
    import main
    asyncio.run(main.startup())                             # migrations, seeding and the schema check
    reload_routes()
    assert client.get("/health").json()["ai_configured"] is False
    body = client.get("/models/routes").json()
    assert [p["configured"] for p in body["providers"]] == [False, False]
    assert {r["task"] for r in body["routes"]} == set(llm.TASKS) and not any(r["key_set"] for r in body["routes"])
    assert client.get("/usage/summary").status_code == 200
    assert client.get("/briefing/today").status_code == 200                # everything that is not AI still works
    r = client.post("/parse", json={"text": "add task: buy milk"})
    assert r.status_code == 503 and "OPENAI_API_KEY is not set" in r.json()["detail"]
    events = _send("hi")
    assert events[-1]["type"] == "final" and "not configured" in events[-1]["content"]
    assert not llm.is_configured() and not llm.is_configured("assistant") and not llm.is_configured("embedding")


def test_each_provider_works_with_only_its_own_key(claude_wire):
    """Only ANTHROPIC_API_KEY is set. A Claude route works; an OpenAI-only task says which key is missing."""
    _route("assistant", provider="claude")
    assert _send()[-1]["content"] == "Added it."
    assert llm.is_configured() and llm.is_configured("assistant") and not llm.is_configured("embedding")
    # A route still on OpenAI is served by its Claude counterpart rather than failing.
    assert llm.complete("parser", [{"role": "user", "content": "x"}], json_mode=True).provider == "anthropic"
    with pytest.raises(llm.AIUnavailable, match="OPENAI_API_KEY"):
        llm.embed_texts(["x"])


# ════════════════════════════════════════════════════════════════
# 6. Routes API, the cap, the usage breakdown
# ════════════════════════════════════════════════════════════════

def test_routes_endpoint_and_patch():
    body = client.get("/models/routes").json()
    assert [r["task"] for r in body["routes"]] == list(llm.TASKS)
    known = {m["model"]: m for m in body["models"]}
    assert known[OPENAI_MINI]["fallback_model"] == CLAUDE_SMALL and known[OPENAI_BIG]["fallback_model"] == CLAUDE_MID
    assert known[CLAUDE_SMALL]["confirmed"] is True and known["whisper-1"]["confirmed"] is False
    assert body["cap"]["monthly_cap_usd"] is None and body["cap"]["over_threshold"] is False

    r = client.patch("/models/routes/parser", json={"model": OPENAI_BIG})
    assert r.status_code == 200 and r.json()["changed"] is True and r.json()["change"]["table"] == "model_routes"
    assert r.json()["route"]["model"] == OPENAI_BIG and r.json()["route"]["fallback_model"] == CLAUDE_MID
    r = client.patch("/models/routes/parser", json={"params": {"reasoning_effort": "low"}})
    assert r.json()["route"]["params"] == {"reasoning_effort": "low"} and r.json()["route"]["model"] == OPENAI_BIG

    assert client.patch("/models/routes/embedding", json={"provider": "claude"}).status_code == 400
    assert client.patch("/models/routes/realtime", json={"model": CLAUDE_SMALL}).status_code == 400
    assert client.patch("/models/routes/parser", json={"model": "text-embedding-3-small"}).status_code == 400
    assert client.patch("/models/routes/parser", json={"provider": "claude", "model": OPENAI_MINI}).status_code == 400
    assert client.patch("/models/routes/nothing", json={"provider": "claude"}).status_code == 404
    assert client.patch("/models/routes/parser", json={}).status_code == 400


def _spend(amount, task="assistant", provider="openai", model=OPENAI_MINI):
    db = _db()
    db.add(models.LLMUsage(created_at=datetime.utcnow(), model=model, purpose=task, provider=provider, task=task,
                           input_tokens=1000, output_tokens=100, estimated_cost_usd=amount))
    db.commit()
    db.close()
    llm.month_to_date(fresh=True)


def test_past_80_percent_of_the_cap_expensive_routes_drop_to_the_cheapest_model(monkeypatch, openai_wire):
    monkeypatch.setenv("MONTHLY_LLM_CAP_USD", "10")
    _route("market_intel", model=OPENAI_BIG)
    _spend(7.99)
    assert llm.cap_state()["over_threshold"] is False
    assert llm.complete("market_intel", [{"role": "user", "content": "x"}]).model == OPENAI_BIG
    assert not [a for a in client.get("/briefing/today").json()["alerts"] if a["rule_id"] == "llm_cost_cap"]

    _spend(0.02)                                             # 8.01 and a little: past 80 percent
    state = llm.cap_state(fresh=True)
    assert state["over_threshold"] and state["cheapest_model"] == OPENAI_MINI
    assert state["downgraded_tasks"] == ["market_intel"]
    assert llm.complete("market_intel", [{"role": "user", "content": "x"}]).model == OPENAI_MINI
    assert openai_wire.bodies[-1]["model"] == OPENAI_MINI
    assert llm.model_for("market_intel") == OPENAI_BIG       # the route itself is not rewritten

    alerts = [a for a in client.get("/briefing/today").json()["alerts"] if a["rule_id"] == "llm_cost_cap"]
    assert len(alerts) == 1 and "80%" in alerts[0]["message"] and OPENAI_MINI in alerts[0]["message"]
    route = next(r for r in client.get("/models/routes").json()["routes"] if r["task"] == "market_intel")
    assert route["model"] == OPENAI_BIG and route["serving_model"] == OPENAI_MINI


def test_the_cheapest_model_may_be_on_the_other_provider(monkeypatch, openai_wire, claude_wire):
    monkeypatch.setenv("MONTHLY_LLM_CAP_USD", "10")
    _spend(9.0)
    state = llm.cap_state(fresh=True)
    assert state["cheapest_model"] == CLAUDE_SMALL and "assistant" in state["downgraded_tasks"]
    result = llm.complete("parser", [{"role": "user", "content": "x"}], json_mode=True)
    assert (result.provider, result.model) == ("anthropic", CLAUDE_SMALL)
    assert claude_wire.bodies[-1]["thinking"] == {"type": "disabled"}


def test_usage_summary_breaks_spend_down_by_provider_and_task():
    _spend(0.5, task="assistant")
    _spend(0.25, task="market_intel", provider="anthropic", model=CLAUDE_MID)
    db = _db()
    llm.log_usage(db, model=OPENAI_MINI, purpose="parse", input_tokens=1_000_000, output_tokens=1_000_000)
    llm.log_usage(db, model=CLAUDE_SMALL, purpose="lead_qualify", input_tokens=1_000_000,
                  cached_input_tokens=400_000, cache_write_tokens=100_000, output_tokens=0)
    llm.log_usage(db, model="whisper-1", purpose="transcribe")
    rows = {u.purpose: u for u in db.query(models.LLMUsage).all()}
    assert (rows["parse"].provider, rows["parse"].task) == ("openai", "parser")
    assert rows["parse"].estimated_cost_usd == pytest.approx(0.75 + 4.50)
    assert (rows["lead_qualify"].provider, rows["lead_qualify"].task) == ("anthropic", "agents")
    # 500K fresh at $0.10, 400K cache reads at $0.01, 100K cache writes at $0.125
    assert rows["lead_qualify"].estimated_cost_usd == pytest.approx(0.05 + 0.004 + 0.0125)
    assert rows["transcribe"].estimated_cost_usd == 0 and rows["transcribe"].task == "transcribe"
    db.close()

    body = client.get("/usage/summary").json()
    by_provider = {p["provider"]: p for p in body["by_provider"]}
    assert set(by_provider) == {"openai", "anthropic"} and by_provider["anthropic"]["calls"] == 2
    by_task = {t["task"]: t for t in body["by_task"]}
    assert {"assistant", "market_intel", "parser", "agents", "transcribe"} <= set(by_task)
    assert by_task["market_intel"]["estimated_cost_usd"] == 0.25 and by_task["parser"]["label"] == "Brain-dump parser"
    assert body["cap"]["month_to_date_usd"] == pytest.approx(0.75 + 5.25 + 0.0665, abs=1e-3)
    route = next(r for r in client.get("/models/routes").json()["routes"] if r["task"] == "market_intel")
    assert route["month_to_date_usd"] == 0.25 and route["calls_this_month"] == 1


def test_m026_seeds_once_and_leaves_changed_rows_alone():
    from migrations import m026_model_router
    from tests.conftest import engine
    _route("parser", model=OPENAI_BIG)
    db = _db()
    row = db.query(models.ModelPrice).filter(models.ModelPrice.model == OPENAI_MINI).one()
    row.input_per_mtok = 9.99
    db.commit()
    db.close()
    with engine.begin() as conn:
        assert m026_model_router.seed(conn) == {"models": 0, "routes": 0}
    reload_routes()
    assert llm.model_for("parser") == OPENAI_BIG and llm.known_models()[OPENAI_MINI]["input"] == 9.99
    seeded = {m[0] for m in m026_model_router.SEED_MODELS}
    assert {OPENAI_MINI, OPENAI_BIG, CLAUDE_SMALL, CLAUDE_MID} <= seeded
    assert set(m026_model_router.SEED_ROUTES) == set(llm.TASKS)
