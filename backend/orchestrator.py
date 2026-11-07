"""
Phase 6 — Orchestrator request bridge.

Turns an HTTP request into a Server-Sent Events stream of orchestrator events, and runs
the off-track alert check for the JSON alerts endpoint.

Routing lives entirely in the model (see context/core/orchestrator_prompt.md) — there is
no keyword router here. This module marshals conversation state (one server-side default
thread, history budget, long-term memory), persists every message of the turn including
tool calls and tool results, and serializes events to ``data:`` lines.
"""

import json
from datetime import date

import models
import orchestrator_tools
import conversation
import memory_service
import change_log
from openai_client import get_orchestrator_response

MEMORY_RECALL_LIMIT = 5


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _enrich_user_text(request, db) -> str:
    """Append today's activity log to the user's text.

    A ``context`` object sent by an older client is ignored on purpose: it carried the
    legacy KPI store, raw habit booleans and old subject mastery. Live state reaches the
    model only through tools (get_dashboard_state), which read the current sources."""
    text = request.text.strip()
    context_parts: list[str] = []
    today_logs = db.query(models.DailyLog).filter(
        models.DailyLog.date == date.today()
    ).order_by(models.DailyLog.id.asc()).all()
    if today_logs:
        log_lines = "\n".join(f"- {l.entry}" for l in today_logs if l.entry)
        if log_lines:
            context_parts.append(f"[Today's activity log]\n{log_lines}")
    if context_parts:
        text += "\n\n" + "\n\n".join(context_parts)
    return text


def _build_messages(request, db) -> tuple[list, dict | None]:
    """History within budget + the new user turn, plus the dynamic context message."""
    thread_id = conversation.DEFAULT_THREAD_ID
    history, summary = conversation.build_history(db, thread_id)
    try:
        memories = [memory_service.format_memory(m)
                    for m in memory_service.recall(db, request.text, limit=MEMORY_RECALL_LIMIT)]
    except Exception as exc:  # noqa: BLE001 — memory must never block a reply
        print(f"[orchestrator] memory recall failed: {exc}")
        memories = []
    ctx = conversation.context_message(summary, memories)
    messages = history + [{"role": "user", "content": _enrich_user_text(request, db)}]
    return messages, ctx


def orchestrator_stream(request, db):
    """Generator of SSE frames for ``POST /orchestrator``."""
    thread_id = conversation.DEFAULT_THREAD_ID
    session_id = getattr(request, "session_id", None)
    text = request.text.strip()

    messages, ctx = _build_messages(request, db)

    # Persist the raw user turn (not the context-enriched one).
    conversation.save_message(db, "user", text, thread_id=thread_id, session_id=session_id)

    def _persist(msg: dict):
        if msg.get("role") == "assistant":
            conversation.save_message(
                db, "assistant", msg.get("content") or "", thread_id=thread_id,
                session_id=session_id, tool_calls=msg.get("tool_calls"),
            )
        elif msg.get("role") == "tool":
            conversation.save_message(
                db, "tool", msg.get("content") or "", thread_id=thread_id,
                session_id=session_id, tool_call_id=msg.get("tool_call_id"),
            )

    change_log.begin_batch(db, "assistant")
    final_text = ""
    changes: list = []
    try:
        for event in get_orchestrator_response(
            messages, db, on_message=_persist,
            context={"message": ctx} if ctx else None,
        ):
            if event.get("type") == "final":
                final_text = event.get("content", "")
                changes = event.get("changes") or []
            yield _sse(event)
    finally:
        change_log.end_batch(db)
        conversation.save_message(
            db, "assistant", final_text, thread_id=thread_id, session_id=session_id,
            changes=changes or None,
        )


def run_off_track(db) -> list:
    """Plain JSON alerts for ``GET /orchestrator/alerts`` — empty list when clear."""
    alerts, _summary = orchestrator_tools.detect_off_track(db)
    return alerts
