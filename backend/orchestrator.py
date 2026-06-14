"""
Phase 6 — Orchestrator request bridge.

Turns an HTTP request into a Server-Sent Events stream of orchestrator events, and runs
the off-track alert check for the JSON alerts endpoint.

Routing lives entirely in the model (see context/core/orchestrator_prompt.md) — there is
no keyword router here. This module only marshals conversation state, persists chat
history (same shape as /input and /chat), and serializes events to ``data:`` lines.
"""

import json
from datetime import date

import models
import orchestrator_tools
from openai_client import get_orchestrator_response


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _build_messages(request, db) -> list:
    """Reconstruct the conversation for this session and append the new user turn,
    enriching it with today's activity log and any live client context — mirroring the
    existing /input and /chat behaviour."""
    session = request.session_id
    history = db.query(models.ChatMessage).filter(
        models.ChatMessage.session_id == session
    ).order_by(models.ChatMessage.created_at).all()
    messages = [{"role": m.role, "content": m.content} for m in history]

    text = request.text.strip()
    context_parts: list[str] = []
    today_logs = db.query(models.DailyLog).filter(
        models.DailyLog.date == date.today()
    ).order_by(models.DailyLog.id.asc()).all()
    if today_logs:
        log_lines = "\n".join(f"- {l.entry}" for l in today_logs if l.entry)
        if log_lines:
            context_parts.append(f"[Today's activity log]\n{log_lines}")
    if getattr(request, "context", None):
        context_parts.append(f"[Live system context]\n{json.dumps(request.context)}")

    user_content = text
    if context_parts:
        user_content += "\n\n" + "\n\n".join(context_parts)
    messages.append({"role": "user", "content": user_content})
    return messages


def orchestrator_stream(request, db):
    """Generator yielding SSE ``data:`` lines for ``POST /orchestrator``."""
    session = request.session_id
    text = request.text.strip()

    messages = _build_messages(request, db)

    # Persist the raw user turn (not the context-enriched one).
    db.add(models.ChatMessage(session_id=session, role="user", content=text))
    db.commit()

    final_text = ""
    for event in get_orchestrator_response(messages, db):
        if event.get("type") == "final":
            final_text = event.get("content", "")
        yield _sse(event)

    db.add(models.ChatMessage(session_id=session, role="assistant", content=final_text))
    db.commit()


def run_off_track(db) -> list:
    """Plain JSON alerts for ``GET /orchestrator/alerts`` — empty list when clear."""
    alerts, _summary = orchestrator_tools.detect_off_track(db)
    return alerts
