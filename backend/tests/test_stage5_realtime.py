"""
Rebuild Stage 5 — real-time voice backend.

OpenAI is mocked: the client-secret request is intercepted at httpx.post.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import models
import llm
import conversation
import voice_realtime as vr
from tests.conftest import TestSessionLocal, client


def _db():
    return TestSessionLocal()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REALTIME_MODEL", raising=False)
    llm.reset_client()
    yield
    llm.reset_client()


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class TestSession:

    def test_503_without_key(self):
        r = client.post("/voice/realtime/session")
        assert r.status_code == 503

    def test_mints_secret_without_leaking_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-SERVER-SECRET")
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update({"url": url, "headers": headers, "json": json})
            return _Resp(200, {"value": "ek_abc123", "expires_at": 1800000000, "session": {"id": "sess_1"}})

        with patch("voice_realtime.httpx.post", side_effect=fake_post):
            r = client.post("/voice/realtime/session")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["client_secret"]["value"] == "ek_abc123"
        assert body["client_secret"]["expires_at"] == 1800000000
        assert body["model"] == "gpt-realtime-2.1-mini"
        assert body["calls_url"].endswith("/realtime/calls")
        assert body["voice_session_id"] and body["silence_timeout_seconds"] == 120
        assert "sk-SERVER-SECRET" not in r.text
        # upstream request
        assert captured["url"].endswith("/realtime/client_secrets")
        assert captured["headers"]["Authorization"] == "Bearer sk-SERVER-SECRET"
        sess = captured["json"]["session"]
        assert sess["type"] == "realtime" and sess["model"] == "gpt-realtime-2.1-mini"
        assert sess["audio"]["input"]["turn_detection"]["type"] == "server_vad"
        assert sess["audio"]["input"]["turn_detection"]["interrupt_response"] is True
        assert sess["audio"]["input"]["transcription"]["model"]
        assert sess["audio"]["output"]["voice"] == "marin"
        assert "VOICE MODE" in sess["instructions"]
        names = {t["name"] for t in sess["tools"]}
        assert {"route_brain_dump", "complete_task", "propose_delete", "confirm_action", "update_syllabus"} <= names
        assert all(t["type"] == "function" and "parameters" in t for t in sess["tools"])
        assert captured["json"]["expires_after"]["seconds"] == 600

    def test_model_env_override(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("REALTIME_MODEL", "gpt-realtime-mini")
        with patch("voice_realtime.httpx.post", return_value=_Resp(200, {"value": "ek_x", "expires_at": 1})):
            r = client.post("/voice/realtime/session")
        assert r.json()["model"] == "gpt-realtime-mini"

    def test_upstream_rejection_is_502_with_friendly_message(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with patch("voice_realtime.httpx.post",
                   return_value=_Resp(400, {"error": {"message": "Unknown model: gpt-realtime-mini"}})):
            r = client.post("/voice/realtime/session")
        assert r.status_code == 502 and "Unknown model" not in r.json()["detail"]
        assert "Unknown model" in capsys.readouterr().out       # detail kept in the logs

    def test_usage_logged(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with patch("voice_realtime.httpx.post", return_value=_Resp(200, {"value": "ek_x", "expires_at": 1})):
            client.post("/voice/realtime/session")
        db = _db()
        row = db.query(models.LLMUsage).first()
        db.close()
        assert row.purpose == "realtime_session" and row.model == "gpt-realtime-2.1-mini"


class TestToolBridge:

    def test_runs_tool_persists_and_returns_changes(self):
        db = _db()
        db.add(models.Todo(text="Write the Business essay", priority=3, category="academics", due="2026-11-10", source="manual"))
        db.commit()
        db.close()
        r = client.post("/voice/realtime/tool", json={
            "name": "complete_task", "arguments": json.dumps({"query": "Business essay"}),
            "call_id": "call_9", "voice_session_id": "vs1",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["result"]["completed"] is True and body["error"] is False
        assert body["changes"][0]["table"] == "todos" and body["changes"][0]["action"] == "update"
        assert json.loads(body["output"])["completed"] is True
        db = _db()
        rows = conversation.thread_rows(db)
        db.close()
        assert [r.role for r in rows] == ["assistant", "tool"]
        assert json.loads(rows[0].tool_calls_json)[0]["function"]["name"] == "complete_task"
        assert rows[1].tool_call_id == "call_9" and rows[0].session_id == "voice:vs1"

    def test_tool_error_is_structured_not_500(self):
        r = client.post("/voice/realtime/tool", json={"name": "no_such_tool", "arguments": "{}"})
        assert r.status_code == 200 and r.json()["error"] is True

    def test_delete_still_needs_confirmation(self):
        db = _db()
        t = models.Todo(text="Old", priority=5, category="personal", due="2026-11-01", source="manual")
        db.add(t)
        db.commit()
        tid = t.id
        db.close()
        r = client.post("/voice/realtime/tool", json={"name": "propose_delete", "arguments": {"table": "todos", "record_id": tid}})
        assert r.json()["result"]["requires_confirmation"] is True
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == tid).count() == 1
        db.close()
        pid = r.json()["result"]["id"]
        r2 = client.post("/voice/realtime/tool", json={"name": "confirm_action", "arguments": {"pending_id": pid}})
        assert r2.json()["result"]["executed"] is True
        db = _db()
        assert db.query(models.Todo).filter(models.Todo.id == tid).count() == 0
        db.close()

    def test_malformed_arguments_become_empty(self):
        r = client.post("/voice/realtime/tool", json={"name": "get_deadlines", "arguments": "{not json"})
        assert r.status_code == 200 and "overdue" in r.json()["result"]


class TestTranscript:

    def test_lines_land_in_default_thread_and_replay(self):
        r = client.post("/voice/realtime/transcript", json={"role": "user", "text": "what's due today?", "voice_session_id": "vs1"})
        assert r.json()["saved"] is True and r.json()["thread_id"] == "default"
        client.post("/voice/realtime/transcript", json={"role": "assistant", "text": "Two things.", "voice_session_id": "vs1",
                                                        "changes": [{"id": 1, "summary": "x", "table": "todos", "action": "update"}]})
        db = _db()
        msgs, _ = conversation.build_history(db)
        rows = conversation.thread_rows(db)
        db.close()
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert json.loads(rows[-1].changes_json)[0]["table"] == "todos"

    def test_rejects_bad_role_and_ignores_empty(self):
        assert client.post("/voice/realtime/transcript", json={"role": "system", "text": "x"}).status_code == 400
        assert client.post("/voice/realtime/transcript", json={"role": "user", "text": "   "}).json()["saved"] is False


class TestFallbackAndFrontend:

    def test_legacy_voice_endpoints_still_present(self):
        assert client.post("/voice/speak", json={"text": "hi"}).status_code == 503   # exists; no key

    def test_frontend_wires_live_voice(self):
        html = Path(__file__).resolve().parent.parent.parent.joinpath("frontend", "index.html").read_text(encoding="utf-8")
        for needle in ('id="mic-btn"', "toggleLiveVoice", "/voice/realtime/session", "/voice/realtime/tool",
                       "/voice/realtime/transcript", 'createDataChannel("oai-events")',
                       "response.function_call_arguments.done", "conversation.item.input_audio_transcription.completed",
                       "input_audio_buffer.speech_started", "function_call_output", "toggleVoice()", "/voice/transcribe"):
            assert needle in html, needle
        js = html.split("LIVE VOICE — WebRTC realtime session")[1].split("function initialRender")[0]
        assert "OPENAI_API_KEY" not in js and "Bearer sk" not in js      # only the ephemeral secret is used
        assert "client_secret.value" in js
