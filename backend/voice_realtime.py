"""
Real-time voice (Rebuild Stage 5).

  POST /voice/realtime/session     mint a short-lived client secret for the browser.
                                   The server's API key never leaves the server.
  POST /voice/realtime/tool        run one tool call made by the realtime model, through
                                   the same tool registry, change log and confirmation
                                   rules as the typed assistant. Persists the call and
                                   result into the default conversation thread.
  POST /voice/realtime/transcript  save a user or assistant transcript line into the
                                   default conversation thread.

Real-time voice stays on OpenAI. The model is the ``realtime`` route, the output voice is
that route's ``voice`` parameter, and the input transcription model is the
``realtime_transcribe`` route (all seeded by m026 from ``REALTIME_MODEL``, ``REALTIME_VOICE``
and ``REALTIME_TRANSCRIBE_MODEL``). The tool calls the voice model makes run on this
server, through the same registry as the typed assistant.

The upstream request shapes follow OpenAI's Realtime WebRTC guide (checked 2026-11-05):
``POST /v1/realtime/client_secrets`` with ``{"session": {"type": "realtime", "model", "audio":
{"output": {"voice"}}, ...}}``, then the browser posts its SDP offer to
``/v1/realtime/calls`` with the ephemeral key; data channel ``oai-events``. If OpenAI rejects
the session request, the 502 body carries its own error message.
"""

import json
import time
import uuid
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import llm
import change_log
import conversation
import orchestrator_tools
from database import get_db

router = APIRouter()

from llm_adapters import openai_adapter

OPENAI_REALTIME_BASE = openai_adapter.REALTIME_BASE
CLIENT_SECRET_TTL_SECONDS = 600
SILENCE_TIMEOUT_SECONDS = 120   # the browser ends the session after this much silence

VOICE_ADDENDUM = """
VOICE MODE
You are speaking aloud. Keep replies short — two or three sentences unless asked for more.
Say what you did after a tool runs ("Done — marked the Business essay complete"). Ask one
question at a time. Never read out ids or JSON. Deletes still need a spoken yes before
confirm_action. Before a send, read the message and who it goes to aloud, then wait: only a
plain spoken yes after that lets confirm_action send it.
"""


VOICE_UNAVAILABLE = "Voice isn't available right now. Please try again in a moment."


def realtime_model() -> str:
    return llm.model_for("realtime")


def realtime_voice() -> str:
    return llm.route("realtime").params.get("voice") or "alloy"


def transcribe_model() -> str:
    return llm.model_for("realtime_transcribe")


def realtime_tools() -> list[dict]:
    """The orchestrator's tool registry in the realtime API's flat tool shape."""
    out = []
    for t in orchestrator_tools.TOOL_SCHEMAS:
        fn = t.get("function", {})
        out.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def instructions() -> str:
    from openai_client import _load_orchestrator_prompt
    return _load_orchestrator_prompt().rstrip() + "\n" + VOICE_ADDENDUM


def session_config() -> dict:
    """The session object sent when minting the client secret."""
    return {
        "type": "realtime",
        "model": realtime_model(),
        "instructions": instructions(),
        "tools": realtime_tools(),
        "tool_choice": "auto",
        "audio": {
            "input": {
                "transcription": {"model": transcribe_model()},
                "turn_detection": {
                    "type": "server_vad",
                    "create_response": True,
                    "interrupt_response": True,
                    "silence_duration_ms": 600,
                },
            },
            "output": {"voice": realtime_voice()},
        },
    }


# ── Session ──────────────────────────────────────────────────

@router.post("/voice/realtime/session")
def create_realtime_session(db: Session = Depends(get_db)):
    key = llm.api_key()
    if not key:
        raise HTTPException(503, "OPENAI_API_KEY is not set — real-time voice is disabled.")
    body = {
        "expires_after": {"anchor": "created_at", "seconds": CLIENT_SECRET_TTL_SECONDS},
        "session": session_config(),
    }
    started = time.monotonic()
    try:
        resp = openai_adapter.realtime_client_secret(body)
    except httpx.HTTPError as exc:
        print(f"[voice] could not reach OpenAI realtime: {exc}")
        raise HTTPException(502, VOICE_UNAVAILABLE)
    if resp.status_code >= 400:
        detail = resp.text[:600]
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:  # noqa: BLE001
            pass
        print(f"[voice] OpenAI rejected the realtime session ({resp.status_code}): {detail}")
        raise HTTPException(502, VOICE_UNAVAILABLE)
    data = resp.json()
    secret = data.get("value") or (data.get("client_secret") or {}).get("value")
    expires_at = data.get("expires_at") or (data.get("client_secret") or {}).get("expires_at")
    if not secret:
        print("[voice] OpenAI returned no client secret.")
        raise HTTPException(502, VOICE_UNAVAILABLE)
    llm.log_usage(db, model=realtime_model(), purpose="realtime_session",
                  duration_ms=int((time.monotonic() - started) * 1000))
    return {
        "client_secret": {"value": secret, "expires_at": expires_at},
        "model": realtime_model(),
        "voice": realtime_voice(),
        "voice_session_id": uuid.uuid4().hex,
        "calls_url": f"{OPENAI_REALTIME_BASE}/calls",
        "silence_timeout_seconds": SILENCE_TIMEOUT_SECONDS,
        "thread_id": conversation.DEFAULT_THREAD_ID,
    }


# ── Tool bridge ──────────────────────────────────────────────

class ToolCallRequest(BaseModel):
    name: str
    arguments: Any = None          # JSON string or object
    call_id: Optional[str] = None
    voice_session_id: Optional[str] = None


@router.post("/voice/realtime/tool")
def run_realtime_tool(payload: ToolCallRequest, db: Session = Depends(get_db)):
    """Execute one function call from the realtime model. Same registry, same change
    log, same confirmation rules as the typed assistant; never raises on a tool error."""
    args = payload.arguments
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            args = {}
    args = args or {}
    call_id = payload.call_id or f"voice_{uuid.uuid4().hex[:12]}"
    session_tag = f"voice:{payload.voice_session_id}" if payload.voice_session_id else "voice"

    change_log.begin_batch(db, "assistant")
    closed = False
    try:
        conversation.save_message(
            db, "assistant", "", session_id=session_tag,
            tool_calls=[{"id": call_id, "type": "function",
                         "function": {"name": payload.name, "arguments": json.dumps(args)}}],
        )
        try:
            result, summary = orchestrator_tools.run_tool(payload.name, args, db)
        except Exception as exc:  # noqa: BLE001 — run_tool should never raise
            print(f"[voice] tool {payload.name} crashed: {type(exc).__name__}: {exc}")
            result, summary = {"error": "The tool failed."}, "The tool failed."
        output = orchestrator_tools.for_model(payload.name, result)
        conversation.save_message(db, "tool", output, session_id=session_tag, tool_call_id=call_id)
        closed = True
        changes = change_log.current_batch_changes(db)
    finally:
        if not closed:
            # Cancelled between saving the call and saving its result: close it so the
            # shared thread never holds an unanswered tool call.
            try:
                conversation.save_message(db, "tool", conversation.INTERRUPTED_RESULT,
                                          session_id=session_tag, tool_call_id=call_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[voice] could not close tool call {call_id}: {exc}")
        change_log.end_batch(db)
    return {
        "call_id": call_id, "name": payload.name, "result": result, "summary": summary,
        "error": isinstance(result, dict) and "error" in result, "changes": changes,
        "output": output,
        "ui_directive": result.get("ui_directive") if isinstance(result, dict) else None,
    }


# ── Transcript ───────────────────────────────────────────────

class TranscriptLine(BaseModel):
    role: str                      # user | assistant
    text: str
    voice_session_id: Optional[str] = None
    changes: Optional[list] = None


@router.post("/voice/realtime/transcript")
def save_transcript(payload: TranscriptLine, db: Session = Depends(get_db)):
    role = payload.role if payload.role in ("user", "assistant") else None
    text = (payload.text or "").strip()
    if role is None:
        raise HTTPException(400, "role must be user or assistant")
    if not text:
        return {"saved": False}
    session_tag = f"voice:{payload.voice_session_id}" if payload.voice_session_id else "voice"
    row = conversation.save_message(db, role, text, session_id=session_tag, changes=payload.changes or None)
    return {"saved": True, "id": row.id, "thread_id": row.thread_id}
