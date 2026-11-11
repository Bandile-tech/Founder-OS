"""
Phase 6 — Orchestrator request bridge.

Turns an HTTP request into a Server-Sent Events stream of orchestrator events, and runs
the off-track alert check for the JSON alerts endpoint.

Routing lives entirely in the model (see context/core/orchestrator_prompt.md) — there is
no keyword router here. This module marshals conversation state (one server-side default
thread, history budget, long-term memory), persists every message of the turn including
tool calls and tool results, and serializes events to ``data:`` lines.

A turn has one implementation, ``_turn_events``. ``orchestrator_stream`` serialises its
events for the dock; ``run_turn`` consumes them to completion for callers with no stream
(Telegram).
"""

import json
from datetime import date
from types import SimpleNamespace

import models
import orchestrator_tools
import conversation
import memory_service
import change_log
from openai_client import get_orchestrator_response, AI_FAILED_REPLY

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


def _turn_events(request, db, outcome: dict | None = None):
    """One assistant turn as a generator of event dicts: history, memory, the change batch
    and the persistence of every message. ``outcome`` (when given) receives ``final``,
    ``changes`` and ``batch_id`` once the turn is over."""
    thread_id = conversation.DEFAULT_THREAD_ID
    session_id = getattr(request, "session_id", None)
    text = request.text.strip()

    messages, ctx = _build_messages(request, db)

    # Persist the raw user turn (not the context-enriched one).
    conversation.save_message(db, "user", text, thread_id=thread_id, session_id=session_id)

    open_calls: list[str] = []      # tool_call ids saved without a result yet

    def _persist(msg: dict):
        if msg.get("role") == "assistant":
            conversation.save_message(
                db, "assistant", msg.get("content") or "", thread_id=thread_id,
                session_id=session_id, tool_calls=msg.get("tool_calls"),
            )
            open_calls.extend(c["id"] for c in msg.get("tool_calls") or [] if c.get("id"))
        elif msg.get("role") == "tool":
            conversation.save_message(
                db, "tool", msg.get("content") or "", thread_id=thread_id,
                session_id=session_id, tool_call_id=msg.get("tool_call_id"),
            )
            if msg.get("tool_call_id") in open_calls:
                open_calls.remove(msg["tool_call_id"])

    batch_id = change_log.begin_batch(db, "assistant")
    final_text = ""
    changes: list = []
    try:
        try:
            for event in get_orchestrator_response(
                messages, db, on_message=_persist,
                context={"message": ctx} if ctx else None,
            ):
                if event.get("type") == "final":
                    final_text = event.get("content", "")
                    changes = event.get("changes") or []
                yield event
        except Exception as exc:  # noqa: BLE001 — never let a raw error reach the screen
            print(f"[orchestrator] turn failed: {type(exc).__name__}: {exc}")
            final_text = AI_FAILED_REPLY
            try:
                db.rollback()       # a failed flush leaves the session unusable until then
                changes = change_log.current_batch_changes(db)
            except Exception:  # noqa: BLE001
                changes = []
            yield {"type": "error", "content": AI_FAILED_REPLY}
            yield {"type": "final", "content": AI_FAILED_REPLY, "changes": changes}
    finally:
        # A turn that failed or was cancelled (dropped connection, restart) may have saved
        # tool calls with no results. Close them before saving so the thread stays valid.
        try:
            for call_id in open_calls:
                conversation.save_message(
                    db, "tool", conversation.INTERRUPTED_RESULT, thread_id=thread_id,
                    session_id=session_id, tool_call_id=call_id,
                )
            open_calls.clear()
            change_log.end_batch(db)
            conversation.save_message(
                db, "assistant", final_text, thread_id=thread_id, session_id=session_id,
                changes=changes or None,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[orchestrator] could not save turn: {type(exc).__name__}: {exc}")
        if outcome is not None:
            outcome.update(final=final_text, changes=changes, batch_id=batch_id)


def orchestrator_stream(request, db):
    """Generator of SSE frames for ``POST /orchestrator``."""
    events = _turn_events(request, db)
    try:
        for event in events:
            yield _sse(event)
    finally:
        events.close()      # a dropped connection must still close the turn (open tool calls, the batch)


def _max_id(db, model) -> int:
    row = db.query(model.id).order_by(model.id.desc()).first()
    return row[0] if row else 0


def run_turn(text: str, db, *, source: str = "dock", session_id: str | None = None, context=None) -> dict:
    """Run one turn to completion, for a caller with no stream.

    Same history, memory recall, change batch and persistence as the dock. ``session_id``
    is saved on every row of the turn (Telegram passes ``telegram:<chat_id>``); ``source``
    stands in for it when none is given. ``context`` is accepted and ignored, as it is on
    ``POST /orchestrator``. Returns ``{final, changes, pending, suggestions, tool_calls,
    batch_id}``, where pending and suggestions are the ones this turn created."""
    import actions
    request = SimpleNamespace(text=str(text or ""),
                              session_id=session_id or (source if source and source != "dock" else None))
    pending_before = _max_id(db, models.PendingAction)
    suggestions_before = _max_id(db, models.Suggestion)
    outcome: dict = {}
    tool_calls: list[str] = []
    for event in _turn_events(request, db, outcome):
        if event.get("type") == "tool_call":
            tool_calls.append(event.get("tool"))
    pending = db.query(models.PendingAction).filter(
        models.PendingAction.id > pending_before, models.PendingAction.status == "pending"
    ).order_by(models.PendingAction.id).all()
    suggestions = db.query(models.Suggestion).filter(
        models.Suggestion.id > suggestions_before, models.Suggestion.status == "pending"
    ).order_by(models.Suggestion.id).all()
    return {
        "final": outcome.get("final") or "", "changes": outcome.get("changes") or [],
        "pending": [actions._pending_dict(p) for p in pending],
        "suggestions": [actions._suggestion_dict(s) for s in suggestions],
        "tool_calls": tool_calls, "batch_id": outcome.get("batch_id"),
    }


def run_off_track(db) -> list:
    """Plain JSON alerts for ``GET /orchestrator/alerts`` — empty list when clear."""
    alerts, _summary = orchestrator_tools.detect_off_track(db)
    return alerts
