"""
What actually goes over the wire to OpenAI, for every call path.

The rest of the suite patches ``llm.chat_text`` and friends, so it never sees the request
the SDK builds. That is how ``max_tokens`` reached production: the GPT-5 family rejects it
with a 400 ("Use 'max_completion_tokens' instead"). Here the real ``openai.OpenAI`` client
is used with its HTTP transport replaced, so each test reads the JSON body the SDK would
send and fails if ``max_tokens`` is in it.

``test_every_call_site_is_covered`` scans the backend for call sites; a new one fails it
until a test below exercises it.
"""

import ast
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

import llm
import models
from tests.conftest import TestSessionLocal, client

BACKEND = Path(__file__).resolve().parent.parent
FORBIDDEN = {"max_tokens"}

# A body every JSON-reading path accepts, and harmless text for the rest.
CONTENT = json.dumps({"requirements": [], "gaps": [], "subject": "Hello",
                      "body": "Hello there.", "summary": "Fine."})


class Recorder:
    """Stands in for api.openai.com: records each request body, answers with a canned one."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append((request.url.path, body))
        if request.url.path.endswith("/embeddings"):
            n = len(body["input"]) if isinstance(body["input"], list) else 1
            return httpx.Response(200, json={
                "object": "list", "model": body["model"],
                "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(n)],
                "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        if body.get("stream"):
            chunks = [
                {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi."}, "finish_reason": None}]},
                {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"], "choices": [],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": CONTENT}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    def chat_bodies(self) -> list[dict]:
        return [b for path, b in self.requests if path.endswith("/chat/completions")]


@pytest.fixture
def wire(monkeypatch):
    """A real OpenAI client whose requests never leave the process."""
    from openai import OpenAI
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    recorder = Recorder()
    fake = OpenAI(api_key="sk-test", max_retries=0,
                  http_client=httpx.Client(transport=httpx.MockTransport(recorder)))
    monkeypatch.setattr(llm, "_client", fake)
    yield recorder
    llm.reset_client()


def _check(recorder: Recorder, purpose_hint: str, expect_cap: bool = True) -> dict:
    """Exactly one chat request was sent; it has no forbidden key and (unless streaming)
    caps its output with max_completion_tokens."""
    bodies = recorder.chat_bodies()
    assert len(bodies) == 1, f"{purpose_hint}: expected one chat request, saw {len(bodies)}"
    body = bodies[0]
    bad = FORBIDDEN & set(body)
    assert not bad, f"{purpose_hint} sent {sorted(bad)} — use max_completion_tokens"
    if expect_cap:
        assert isinstance(body.get("max_completion_tokens"), int), f"{purpose_hint}: no output cap"
    return body


def _db():
    return TestSessionLocal()


# ── One test per call path ───────────────────────────────────

def test_application_parse(wire):
    import applications
    db = _db()
    app = models.Application(name="Wharton", kind="university")
    db.add(app)
    db.commit()
    applications.import_requirements(db, app, "Two references, a CV and a personal statement are required.")
    db.close()
    assert _check(wire, "application_parse")["max_completion_tokens"] == 1200


def test_application_gap_review(wire):
    import applications
    db = _db()
    app = models.Application(name="Wharton", kind="university")
    db.add(app)
    db.commit()
    applications.review_gaps(db, app, refresh=True)
    db.close()
    assert _check(wire, "application_gaps")["max_completion_tokens"] == 700


def test_brand_insight(wire, monkeypatch):
    from brand import analytics
    monkeypatch.setattr(analytics, "performance", lambda db, platform=None: {
        "connected": True, "cadence": {"posts_in_window": 3}})
    db = _db()
    out = analytics.insight(db, refresh=True)
    db.close()
    assert out["text"]
    assert _check(wire, "brand_insight")["max_completion_tokens"] == analytics.INSIGHT_MAX_TOKENS


def test_briefing_summary(wire):
    body = client.get("/briefing/today").json()
    assert body["summary_source"] == "llm"
    assert _check(wire, "briefing_summary")["max_completion_tokens"] == 300


def test_conversation_summary(wire):
    import conversation
    conversation.summarise("", "user: hello\nassistant: hi")
    assert _check(wire, "conversation_summary")["max_completion_tokens"] == 600


def test_lead_qualify(wire):
    import leads
    db = _db()
    lead = models.Lead(name="Chanda Mwila", company="Lusaka Logistics")
    db.add(lead)
    db.commit()
    leads._llm_fit(db, lead)
    db.close()
    assert _check(wire, "lead_qualify")["max_completion_tokens"] == 300


@pytest.mark.parametrize("channel", ["email", "whatsapp"])
def test_outreach_draft(wire, channel):
    import outreach
    db = _db()
    lead = models.Lead(name="Chanda Mwila", company="Lusaka Logistics", email="c@example.com",
                       whatsapp="+260970000000")
    db.add(lead)
    db.commit()
    outreach.draft(db, lead, channel)
    db.close()
    assert _check(wire, f"outreach_draft ({channel})")["max_completion_tokens"] == 400


def test_legacy_chat(wire):
    import openai_client
    openai_client.get_chat_response([{"role": "user", "content": "hi"}])
    assert _check(wire, "chat")["max_completion_tokens"] == 800


def test_legacy_chat_with_memory(wire):
    import openai_client
    db = _db()
    openai_client.get_chat_response_with_memory([{"role": "user", "content": "hi"}], db)
    db.close()
    assert _check(wire, "chat (memory)")["max_completion_tokens"] == 800


def test_parser(wire):
    import openai_client
    openai_client.get_parse_response("Finish the delivery for Acme tomorrow.",
                                     {"today": date.today().isoformat()})
    assert _check(wire, "parse")["max_completion_tokens"] == 1000


def test_proactive_brief(wire):
    import openai_client
    openai_client.get_proactive_brief({})
    assert _check(wire, "proactive_brief")["max_completion_tokens"] == 300


def test_market_intelligence(wire):
    from market_intel import agent
    out = agent._llm_json("system", "user", purpose="market_intel_test")
    assert out, "the call failed and was swallowed"
    _check(wire, "market_intel", expect_cap=False)


def test_orchestrator_stream(wire):
    import openai_client
    events = list(openai_client._stream_completion([{"role": "user", "content": "hi"}], []))
    assert {"content": "Hi."} in events
    _check(wire, "orchestrator stream", expect_cap=False)


def test_embeddings(wire):
    assert llm.embed_texts(["a", "b"]) == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    (path, body), = wire.requests
    assert path.endswith("/embeddings") and not (FORBIDDEN & set(body))


def test_gateway_renames_a_stray_max_tokens(wire):
    """A future caller that passes max_tokens still sends max_completion_tokens."""
    llm.chat_text([{"role": "user", "content": "hi"}], purpose="stray", max_tokens=50)
    assert _check(wire, "stray")["max_completion_tokens"] == 50


# ── Every call site has a test above ─────────────────────────

# (file, enclosing function) for every place that sends a request to OpenAI's chat or
# embeddings API. Audio (transcription, speech) takes no token cap and is not listed.
COVERED = {
    ("applications.py", "import_requirements"),     # test_application_parse
    ("applications.py", "review_gaps"),             # test_application_gap_review
    ("brand/analytics.py", "insight"),              # test_brand_insight
    ("briefing.py", "_summary"),                    # test_briefing_summary
    ("conversation.py", "summarise"),               # test_conversation_summary
    ("leads.py", "_llm_fit"),                       # test_lead_qualify
    ("outreach.py", "draft"),                       # test_outreach_draft
    ("openai_client.py", "get_chat_response"),      # test_legacy_chat
    ("openai_client.py", "get_chat_response_with_memory"),  # test_legacy_chat_with_memory
    ("openai_client.py", "get_parse_response"),     # test_parser
    ("openai_client.py", "get_proactive_brief"),    # test_proactive_brief
    ("openai_client.py", "_stream_completion"),     # test_orchestrator_stream
    ("market_intel/agent.py", "_llm_json"),         # test_market_intelligence
    ("llm.py", "chat_completion"),                  # every chat test goes through it
    ("llm.py", "chat_text"),
    ("llm.py", "embed_texts"),                      # test_embeddings
    ("retrieval.py", "index_items"),                # through embed_texts
    ("retrieval.py", "index_one"),
    ("retrieval.py", "search"),
}

_CALLS = {"chat_text", "chat_completion", "embed_texts"}


def _call_sites() -> set[tuple[str, str]]:
    found = set()
    skip = {"tests", "migrations", "venv", ".venv", "__pycache__"}
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if skip & set(rel.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node, enclosing):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing = node.name
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr, owner = node.func.attr, node.func.value
                owner_name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
                via_gateway = attr in _CALLS and owner_name == "llm"
                direct = attr == "create" and owner_name in {"completions", "embeddings"}
                if via_gateway or direct:
                    found.add((rel.as_posix(), enclosing or "<module>"))
            for child in ast.iter_child_nodes(node):
                visit(child, enclosing)

        visit(tree, None)
    return found


def test_every_call_site_is_covered():
    found = _call_sites()
    missing = {site for site in found if site not in COVERED}
    assert not missing, (f"new OpenAI call site(s) {sorted(missing)}: add a test above that "
                         f"checks the request it sends, then list it in COVERED")


def test_no_source_passes_max_tokens():
    """Belt and braces: no call site in the backend spells the old parameter."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if {"tests", "venv", ".venv", "__pycache__"} & set(rel.parts) or rel.as_posix() == "llm.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "max_tokens":
                offenders.append(f"{rel.as_posix()}:{node.value.lineno}")
    assert not offenders, f"max_tokens passed at {offenders}"
