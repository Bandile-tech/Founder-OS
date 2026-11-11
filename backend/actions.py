"""
Pending actions (deletes that need confirmation) and suggestions (low-confidence
extractions that need one tap).

Pending actions
  propose_delete(db, table, record_id, reason)  — records the intent, deletes nothing
  confirm_action(db, pending_id)                — executes the delete through the change log
  cancel_action(db, pending_id)
  GET  /actions/pending
  POST /actions/{id}/confirm
  POST /actions/{id}/cancel

Suggestions
  add_suggestion(db, kind, payload, confidence, reason, source_text)
  accept_suggestion(db, id)  — writes through the same logged parser path
  GET  /suggestions
  POST /suggestions/{id}/accept
  POST /suggestions/{id}/dismiss
"""

import json
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import change_log
from database import get_db

router = APIRouter()

PENDING_TTL_HOURS = 24

# Tables the assistant may propose deleting from, with the column that names a row.
DELETABLE = {
    "todos": (models.Todo, "text"),
    "notes": (models.Note, "title"),
    "revenue": (models.Revenue, "source"),
    "ai_memory": (models.AIMemory, "context_data"),
    "books": (models.Book, "title"),
    "daily_bible_log": (models.DailyBibleLog, "book"),
    "clients": (models.Client, "name"),
    "lift_logs": (models.LiftLog, "lift_name"),
    "daily_logs": (models.DailyLog, "entry"),
    "leads": (models.Lead, "name"),
    "outreach_messages": (models.OutreachMessage, "body"),
    "contacts": (models.Contact, "name"),
    "deals": (models.Deal, "title"),
    "projects": (models.Project, "title"),
    "payments": (models.Payment, "title"),
    "recurring_revenue": (models.RecurringRevenue, "title"),
    "communications": (models.Communication, "summary"),
}


def _label(table: str, obj) -> str:
    model, col = DELETABLE[table]
    value = getattr(obj, col, None) or f"#{obj.id}"
    return f"{table.rstrip('s').replace('_', ' ')} '{str(value)[:80]}'"


def _pending_dict(p: models.PendingAction) -> dict:
    return {
        "id": p.id, "kind": p.kind, "table": p.table_name, "record_id": p.record_id,
        "summary": p.summary, "status": p.status, "created_at": p.created_at,
        "expires_at": p.expires_at, "requires_confirmation": p.status == "pending",
    }


# ── Pending deletes ──────────────────────────────────────────

def propose_delete(db: Session, table: str, record_id: int, reason: str | None = None,
                   thread_id: str | None = None) -> dict:
    if table not in DELETABLE:
        raise ValueError(f"Deleting from '{table}' is not supported. Allowed: {', '.join(DELETABLE)}")
    model, _ = DELETABLE[table]
    obj = db.query(model).filter(model.id == int(record_id)).first()
    if obj is None:
        raise ValueError(f"{table}#{record_id} not found")
    # Reuse an open proposal for the same row rather than stacking duplicates.
    existing = db.query(models.PendingAction).filter(
        models.PendingAction.table_name == table,
        models.PendingAction.record_id == int(record_id),
        models.PendingAction.status == "pending",
    ).first()
    if existing:
        return _pending_dict(existing)
    p = models.PendingAction(
        created_at=datetime.utcnow(), thread_id=thread_id, kind="delete",
        table_name=table, record_id=int(record_id),
        summary=f"Delete {_label(table, obj)}" + (f" — {reason}" if reason else ""),
        payload_json=json.dumps(change_log.snapshot(obj), default=str),
        status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=PENDING_TTL_HOURS),
        batch_id=change_log.current_batch_id(db),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _pending_dict(p)


def _get_pending(db: Session, pending_id: int) -> models.PendingAction:
    p = db.query(models.PendingAction).filter(models.PendingAction.id == pending_id).first()
    if p is None:
        raise HTTPException(404, "Pending action not found")
    return p


def confirm_action(db: Session, pending_id: int) -> dict:
    p = _get_pending(db, pending_id)
    if p.status != "pending":
        return {**_pending_dict(p), "executed": False, "reason": f"already {p.status}"}
    if p.expires_at and p.expires_at < datetime.utcnow():
        p.status = "expired"
        p.resolved_at = datetime.utcnow()
        db.commit()
        return {**_pending_dict(p), "executed": False, "reason": "expired — propose it again"}
    model, _ = DELETABLE[p.table_name]
    obj = db.query(model).filter(model.id == p.record_id).first()
    if obj is None:
        p.status = "cancelled"
        p.resolved_at = datetime.utcnow()
        db.commit()
        return {**_pending_dict(p), "executed": False, "reason": "record no longer exists"}
    with change_log.batch_scope(db, "assistant"):
        change = change_log.record_delete(db, obj, p.summary or f"Deleted {p.table_name}#{p.record_id}")
    p.status = "confirmed"
    p.resolved_at = datetime.utcnow()
    db.commit()
    return {**_pending_dict(p), "executed": True, "change": change}


def cancel_action(db: Session, pending_id: int) -> dict:
    p = _get_pending(db, pending_id)
    if p.status == "pending":
        p.status = "cancelled"
        p.resolved_at = datetime.utcnow()
        db.commit()
    return _pending_dict(p)


def list_pending(db: Session) -> list[dict]:
    rows = db.query(models.PendingAction).filter(
        models.PendingAction.status == "pending"
    ).order_by(models.PendingAction.id.desc()).all()
    return [_pending_dict(p) for p in rows]


@router.get("/actions/pending")
def pending_endpoint(db: Session = Depends(get_db)):
    return list_pending(db)


@router.post("/actions/{pending_id}/confirm")
def confirm_endpoint(pending_id: int, db: Session = Depends(get_db)):
    return confirm_action(db, pending_id)


@router.post("/actions/{pending_id}/cancel")
def cancel_endpoint(pending_id: int, db: Session = Depends(get_db)):
    return cancel_action(db, pending_id)


# ── Suggestions ──────────────────────────────────────────────

def _suggestion_dict(s: models.Suggestion) -> dict:
    try:
        payload = json.loads(s.payload_json)
    except (ValueError, TypeError):
        payload = {}
    return {
        "id": s.id, "kind": s.kind, "payload": payload, "confidence": s.confidence,
        "reason": s.reason, "status": s.status, "created_at": s.created_at,
        "source_text": s.source_text,
        "text": payload.get("text") or payload.get("title") or payload.get("fragment"),
    }


def add_suggestion(db: Session, kind: str, payload: dict, *, confidence: float | None,
                   reason: str, source_text: str | None = None, source: str | None = None) -> dict:
    s = models.Suggestion(
        created_at=datetime.utcnow(), kind=kind, payload_json=json.dumps(payload, default=str),
        confidence=confidence, reason=reason, source_text=source_text,
        status="pending", source=source or "parser", batch_id=change_log.current_batch_id(db),
    )
    db.add(s)
    db.flush()
    return _suggestion_dict(s)


def _get_suggestion(db: Session, sid: int) -> models.Suggestion:
    s = db.query(models.Suggestion).filter(models.Suggestion.id == sid).first()
    if s is None:
        raise HTTPException(404, "Suggestion not found")
    return s


def accept_suggestion(db: Session, sid: int) -> dict:
    """Write the suggested item through the normal (logged) parser path."""
    import main

    s = _get_suggestion(db, sid)
    if s.status != "pending":
        return {**_suggestion_dict(s), "applied": False, "reason": f"already {s.status}"}
    payload = json.loads(s.payload_json)
    parsed: dict = {}
    if s.kind == "todo_add":
        parsed["todos_add"] = [{**payload, "intent": "commitment", "confidence": 1.0}]
    elif s.kind == "inbox_add":
        parsed["ideas"] = [{**payload, "confidence": 1.0}]
    elif s.kind == "todo_complete":
        parsed["todos_complete_ids"] = [payload.get("todo_id")]
    else:
        raise HTTPException(400, f"Cannot apply suggestion kind '{s.kind}'")

    with change_log.batch_scope(db, "ui"):
        updates = main.apply_parse_updates(db, parsed, date.today())
        changes = change_log.current_batch_changes(db)
    s.status = "accepted"
    s.resolved_at = datetime.utcnow()
    db.commit()
    return {**_suggestion_dict(s), "applied": True, "updates": updates, "changes": changes}


def dismiss_suggestion(db: Session, sid: int) -> dict:
    s = _get_suggestion(db, sid)
    if s.status == "pending":
        s.status = "dismissed"
        s.resolved_at = datetime.utcnow()
        db.commit()
    return _suggestion_dict(s)


@router.get("/suggestions")
def suggestions_endpoint(status: str = "pending", db: Session = Depends(get_db)):
    q = db.query(models.Suggestion)
    if status != "all":
        q = q.filter(models.Suggestion.status == status)
    return [_suggestion_dict(s) for s in q.order_by(models.Suggestion.id.desc()).limit(200).all()]


@router.post("/suggestions/{sid}/accept")
def accept_endpoint(sid: int, db: Session = Depends(get_db)):
    return accept_suggestion(db, sid)


@router.post("/suggestions/{sid}/dismiss")
def dismiss_endpoint(sid: int, db: Session = Depends(get_db)):
    return dismiss_suggestion(db, sid)
