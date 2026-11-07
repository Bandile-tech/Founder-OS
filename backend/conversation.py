"""
Server-side conversation state for the assistant.

* One persistent thread (``DEFAULT_THREAD_ID``) so history follows the user across
  devices. The browser's ``session_id`` is still accepted and stored for compatibility
  but never used to partition history.
* History budget: the last ``ASSISTANT_HISTORY_TURNS`` user turns (default 20) are sent
  verbatim, preceded by a rolling summary of everything older. The summary is refreshed
  only once ``ASSISTANT_SUMMARY_BATCH`` (default 10) messages have fallen out of the
  window, so the extra LLM call is amortised rather than paid every turn. With no API
  key the summary simply stays as it is.
* Tool calls and tool results are persisted so later turns can see what a tool returned.
  On replay, tool results are truncated to ``TOOL_RESULT_REPLAY_CHARS``.
"""

import json
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from database import get_db

DEFAULT_THREAD_ID = "default"
TOOL_RESULT_REPLAY_CHARS = 1500


def history_turns() -> int:
    try:
        return max(1, int(os.getenv("ASSISTANT_HISTORY_TURNS", "20")))
    except ValueError:
        return 20


def summary_batch() -> int:
    try:
        return max(1, int(os.getenv("ASSISTANT_SUMMARY_BATCH", "10")))
    except ValueError:
        return 10


# ── Persistence ──────────────────────────────────────────────

def save_message(db, role: str, content: str, *, thread_id: str = DEFAULT_THREAD_ID,
                 session_id: str | None = None, tool_calls: list | None = None,
                 tool_call_id: str | None = None, changes: list | None = None,
                 commit: bool = True) -> models.ChatMessage:
    row = models.ChatMessage(
        session_id=session_id or thread_id,
        thread_id=thread_id,
        role=role,
        content=content if content is not None else "",
        tool_calls_json=json.dumps(tool_calls, default=str) if tool_calls else None,
        tool_call_id=tool_call_id,
        changes_json=json.dumps(changes, default=str) if changes else None,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return row


def thread_rows(db, thread_id: str = DEFAULT_THREAD_ID) -> list[models.ChatMessage]:
    return (
        db.query(models.ChatMessage)
        .filter(models.ChatMessage.thread_id == thread_id)
        .order_by(models.ChatMessage.id.asc())
        .all()
    )


# ── Replay ───────────────────────────────────────────────────

def row_to_message(row: models.ChatMessage) -> dict:
    """Convert a stored row back into an OpenAI chat message."""
    if row.role == "tool":
        content = row.content or ""
        if len(content) > TOOL_RESULT_REPLAY_CHARS:
            content = content[:TOOL_RESULT_REPLAY_CHARS] + " …[truncated]"
        return {"role": "tool", "tool_call_id": row.tool_call_id or "", "content": content}
    if row.role == "assistant" and row.tool_calls_json:
        try:
            calls = json.loads(row.tool_calls_json)
        except (ValueError, TypeError):
            calls = []
        msg = {"role": "assistant", "content": row.content or None}
        if calls:
            msg["tool_calls"] = calls
        return msg
    return {"role": row.role, "content": row.content or ""}


def _window_start(rows: list[models.ChatMessage], turns: int) -> int:
    """Index of the first row inside the verbatim window: the Nth most recent user turn.
    Starting at a user row guarantees no orphaned tool result opens the window."""
    seen = 0
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].role == "user":
            seen += 1
            if seen >= turns:
                return i
    return 0


def build_history(db, thread_id: str = DEFAULT_THREAD_ID, *, refresh_summary: bool = True) -> tuple[list[dict], str | None]:
    """Return (messages, summary) for the thread within the history budget.

    ``messages`` is the verbatim window as OpenAI chat messages. ``summary`` is the
    rolling summary text (None when nothing has fallen out of the window yet)."""
    rows = thread_rows(db, thread_id)
    start = _window_start(rows, history_turns())
    window, older = rows[start:], rows[:start]

    summary_row = (
        db.query(models.ConversationSummary)
        .filter(models.ConversationSummary.thread_id == thread_id)
        .first()
    )
    if older and refresh_summary:
        summary_row = _maybe_refresh_summary(db, thread_id, older, summary_row)

    summary = summary_row.summary if (summary_row and summary_row.summary) else None
    return [row_to_message(r) for r in window], summary


# ── Rolling summary ──────────────────────────────────────────

def _transcript(rows: list[models.ChatMessage]) -> str:
    lines = []
    for r in rows:
        if r.role == "tool":
            lines.append(f"[tool result] {(r.content or '')[:300]}")
        elif r.role == "assistant" and r.tool_calls_json and not (r.content or "").strip():
            try:
                names = ", ".join(c["function"]["name"] for c in json.loads(r.tool_calls_json))
            except Exception:  # noqa: BLE001
                names = "tools"
            lines.append(f"assistant: [called {names}]")
        else:
            lines.append(f"{r.role}: {(r.content or '')[:1200]}")
    return "\n".join(lines)


def summarise(existing: str, transcript: str, db=None) -> str:
    """One LLM call that folds ``transcript`` into ``existing``. Tests patch this."""
    import llm
    prompt = (
        "You maintain a running summary of a conversation between Bandile and his personal "
        "operating system. Fold the new turns into the existing summary. Keep: facts stated, "
        "decisions made, commitments, preferences, open questions, and anything the "
        "assistant should remember later. Drop pleasantries. Plain text, at most 25 lines."
    )
    user = f"EXISTING SUMMARY:\n{existing or '(none)'}\n\nNEW TURNS:\n{transcript}"
    return llm.chat_text(
        [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
        db=db, purpose="conversation_summary", max_tokens=600,
    ).strip()


def _maybe_refresh_summary(db, thread_id, older, summary_row):
    import llm

    covered = summary_row.covers_through_message_id if summary_row else None
    pending = [r for r in older if covered is None or r.id > covered]
    if len(pending) < summary_batch() or not llm.is_configured():
        return summary_row
    try:
        text = summarise(summary_row.summary if summary_row else "", _transcript(pending), db=db)
    except Exception as exc:  # noqa: BLE001
        print(f"[conversation] summary refresh failed: {exc}")
        return summary_row
    if summary_row is None:
        summary_row = models.ConversationSummary(thread_id=thread_id, summary="", message_count=0)
        db.add(summary_row)
    summary_row.summary = text
    summary_row.covers_through_message_id = pending[-1].id
    summary_row.message_count = (summary_row.message_count or 0) + len(pending)
    summary_row.updated_at = datetime.utcnow()
    db.commit()
    return summary_row


# ── Context block ────────────────────────────────────────────

def context_message(summary: str | None, memories: list[str] | None) -> dict | None:
    """A second system message carrying dynamic context. The main system prompt and the
    tool definitions stay first and byte-stable so prompt caching applies."""
    parts = []
    if memories:
        parts.append("[Long-term memory]\n" + "\n".join(f"- {m}" for m in memories))
    if summary:
        parts.append("[Earlier conversation, summarised]\n" + summary)
    if not parts:
        return None
    return {"role": "system", "content": "\n\n".join(parts)}


# ── Conversations page ───────────────────────────────────────

router = APIRouter()

HISTORY_PAGE_LIMIT = 200


def _day_label(day, today) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    fmt = "%A %d %B" if day.year == today.year else "%A %d %B %Y"
    return day.strftime(fmt).replace(" 0", " ")


def history(db, *, q: str | None = None, before_id: int | None = None, limit: int = HISTORY_PAGE_LIMIT,
            tz_offset_minutes: int = 0, thread_id: str = DEFAULT_THREAD_ID, now: datetime | None = None) -> dict:
    """The thread's spoken and typed messages (user and assistant text only — no tool
    plumbing), grouped by the viewer's local day, newest day first. Within a day the
    messages read in order. ``q`` filters case-insensitively; ``before_id`` pages back."""
    query = (db.query(models.ChatMessage)
             .filter(models.ChatMessage.thread_id == thread_id,
                     models.ChatMessage.role.in_(["user", "assistant"]),
                     models.ChatMessage.content != ""))
    q = (q or "").strip()
    if q:
        query = query.filter(func.lower(models.ChatMessage.content).contains(q.lower()))
    if before_id:
        query = query.filter(models.ChatMessage.id < int(before_id))
    rows = query.order_by(models.ChatMessage.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    # tz_offset_minutes follows JavaScript's getTimezoneOffset(): minutes to ADD to local
    # time to get UTC (Lusaka, UTC+2, is -120).
    shift = timedelta(minutes=-int(tz_offset_minutes or 0))
    today = ((now or datetime.utcnow()) + shift).date()
    days: dict = {}
    for r in rows:
        local = (r.created_at or datetime.utcnow()) + shift
        day = days.setdefault(local.date(), [])
        changes = 0
        if r.changes_json:
            try:
                changes = len(json.loads(r.changes_json) or [])
            except (ValueError, TypeError):
                changes = 0
        day.append({
            "id": r.id, "role": r.role, "text": r.content,
            "time": local.strftime("%H:%M"),
            "voice": (r.session_id or "").startswith("voice"),
            "changes": changes,
        })
    out = []
    for day in sorted(days, reverse=True):
        msgs = sorted(days[day], key=lambda m: m["id"])
        out.append({"date": day.isoformat(), "label": _day_label(day, today), "messages": msgs})
    return {"query": q, "days": out, "count": len(rows), "has_more": has_more,
            "next_before_id": rows[-1].id if (has_more and rows) else None}


@router.get("/conversation/history")
def conversation_history(q: str | None = Query(None, max_length=200),
                         before_id: int | None = Query(None, ge=1),
                         limit: int = Query(HISTORY_PAGE_LIMIT, ge=1, le=500),
                         tz_offset: int = Query(0, ge=-900, le=900),
                         db: Session = Depends(get_db)):
    return history(db, q=q, before_id=before_id, limit=limit, tz_offset_minutes=tz_offset)
