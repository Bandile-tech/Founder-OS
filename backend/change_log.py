"""
Change log + undo.

Every write made by the assistant, the parser or a tool is recorded in ``change_log``
with a before/after column snapshot. One assistant turn is one batch (``begin_batch`` /
``end_batch``); writes outside an assistant turn get their own batch per call.

Undo restores the before-state: an update is reverted column by column, a create is
removed, a delete is re-inserted with its original id. Undo is idempotent — a change
already undone reports ``already_undone`` and touches nothing.

Endpoints:
  GET  /changes                       recent changes (newest first)
  POST /changes/{id}/undo
  POST /changes/batch/{batch_id}/undo
"""

import json
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from database import get_db

router = APIRouter()

_BATCH_KEY = "change_batch"


# ── Batches ──────────────────────────────────────────────────
# The batch lives on the SQLAlchemy session (``db.info``) rather than in a context
# variable: Starlette drives SSE generators one step per worker thread, and context
# set in one step does not survive into the next. Every write path has ``db``.

def begin_batch(db: Session, source: str = "assistant", batch_id: str | None = None) -> str:
    bid = batch_id or uuid.uuid4().hex
    db.info[_BATCH_KEY] = {"id": bid, "source": source, "changes": []}
    return bid


def end_batch(db: Session) -> list:
    batch = db.info.pop(_BATCH_KEY, None)
    return list(batch["changes"]) if batch else []


def in_batch(db: Session) -> bool:
    return _BATCH_KEY in db.info


def current_batch_id(db: Session) -> str | None:
    batch = db.info.get(_BATCH_KEY)
    return batch["id"] if batch else None


def current_batch_changes(db: Session) -> list:
    batch = db.info.get(_BATCH_KEY)
    return list(batch["changes"]) if batch else []


class batch_scope:
    """``with batch_scope(db, "parser"):`` — opens a batch unless one is already active."""

    def __init__(self, db: Session, source: str):
        self.db = db
        self.source = source
        self.opened = False

    def __enter__(self):
        if not in_batch(self.db):
            begin_batch(self.db, self.source)
            self.opened = True
        return current_batch_id(self.db)

    def __exit__(self, *exc):
        if self.opened:
            end_batch(self.db)
        return False


# ── Snapshots ────────────────────────────────────────────────

def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def snapshot(obj) -> dict:
    """JSON-safe dict of every mapped column on an ORM instance."""
    table = obj.__table__
    return {c.name: _json_safe(getattr(obj, c.name)) for c in table.columns}


def _coerce(column, value):
    """Turn a JSON value back into the column's Python type."""
    if value is None:
        return None
    try:
        py = column.type.python_type
    except NotImplementedError:
        return value
    if py is datetime and isinstance(value, str):
        return datetime.fromisoformat(value)
    if py is date and isinstance(value, str):
        return date.fromisoformat(value[:10])
    if py is bool and not isinstance(value, bool):
        return bool(value)
    return value


def _model_for(table_name: str):
    for mapper in models.Base.registry.mappers:
        if mapper.local_table.name == table_name:
            return mapper.class_
    return None


# ── Recording ────────────────────────────────────────────────

def _entry(row: models.ChangeLog) -> dict:
    return {
        "id": row.id, "summary": row.summary, "table": row.table_name,
        "action": row.action, "record_id": row.record_id, "batch_id": row.batch_id,
        "undone": row.undone_at is not None,
    }


def _record(db: Session, *, table_name: str, record_id, action: str, before, after,
            summary: str, source: str | None) -> dict:
    batch = db.info.get(_BATCH_KEY)
    bid = batch["id"] if batch else uuid.uuid4().hex
    src = source or (batch["source"] if batch else None) or "ui"
    row = models.ChangeLog(
        created_at=datetime.utcnow(),
        batch_id=bid,
        source=src,
        table_name=table_name,
        record_id=record_id,
        action=action,
        before_json=json.dumps(before, default=str) if before is not None else None,
        after_json=json.dumps(after, default=str) if after is not None else None,
        summary=summary,
    )
    db.add(row)
    db.flush()
    entry = _entry(row)
    if batch is not None:
        batch["changes"].append(entry)
    return entry


def record_create(db: Session, obj, summary: str, source: str | None = None) -> dict:
    """Call after the object is flushed (so it has an id)."""
    db.flush()
    return _record(db, table_name=obj.__table__.name, record_id=obj.id, action="create",
                   before=None, after=snapshot(obj), summary=summary, source=source)


def record_update(db: Session, obj, before: dict, summary: str, source: str | None = None) -> dict | None:
    """``before`` is ``snapshot(obj)`` taken before the mutation. No-op if nothing changed."""
    db.flush()
    after = snapshot(obj)
    if after == before:
        return None
    return _record(db, table_name=obj.__table__.name, record_id=obj.id, action="update",
                   before=before, after=after, summary=summary, source=source)


def record_delete(db: Session, obj, summary: str, source: str | None = None) -> dict:
    """Records then deletes. Caller commits."""
    before = snapshot(obj)
    entry = _record(db, table_name=obj.__table__.name, record_id=obj.id, action="delete",
                    before=before, after=None, summary=summary, source=source)
    db.delete(obj)
    db.flush()
    return entry


# ── Undo ─────────────────────────────────────────────────────

def _after_undo_hook(db: Session, table_name: str, row: models.ChangeLog | None = None):
    """Keep in-memory state, and the outside world, in step with the database after an undo."""
    if table_name == "kpi_snapshots":
        import main
        main.load_kpi_state(db)
    elif table_name == "calendar_events" and row is not None and row.action == "create":
        # The event was created on Google too: remove it there. Best effort — the cache row
        # is already gone whatever Google answers.
        try:
            external_id = (json.loads(row.after_json) if row.after_json else {}).get("external_id")
            if external_id:
                import integrations_google
                integrations_google.delete_remote(db, external_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[change_log] calendar undo: remote delete failed ({type(exc).__name__})")


def _undo_row(db: Session, row: models.ChangeLog) -> dict:
    if row.undone_at is not None:
        return {**_entry(row), "already_undone": True}
    model = _model_for(row.table_name)
    if model is None:
        raise HTTPException(400, f"Cannot undo changes to unknown table '{row.table_name}'")
    before = json.loads(row.before_json) if row.before_json else None
    columns = {c.name: c for c in model.__table__.columns}

    if row.action == "create":
        obj = db.query(model).filter(model.id == row.record_id).first()
        if obj is not None:
            db.delete(obj)
    elif row.action == "update":
        obj = db.query(model).filter(model.id == row.record_id).first()
        if obj is None:
            raise HTTPException(409, f"Record {row.table_name}#{row.record_id} no longer exists")
        for name, value in (before or {}).items():
            if name in columns and name != "id":
                setattr(obj, name, _coerce(columns[name], value))
    elif row.action == "delete":
        exists = db.query(model).filter(model.id == row.record_id).first()
        if exists is None and before:
            obj = model(**{n: _coerce(columns[n], v) for n, v in before.items() if n in columns})
            db.add(obj)
    else:
        raise HTTPException(400, f"Unknown action '{row.action}'")

    row.undone_at = datetime.utcnow()
    db.flush()
    _after_undo_hook(db, row.table_name, row)
    return {**_entry(row), "already_undone": False}


def undo_change(db: Session, change_id: int) -> dict:
    row = db.query(models.ChangeLog).filter(models.ChangeLog.id == change_id).first()
    if row is None:
        raise HTTPException(404, "Change not found")
    try:
        result = _undo_row(db, row)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        # e.g. undoing a create whose row is now referenced by later rows (Postgres enforces FKs)
        db.rollback()
        raise HTTPException(409, f"Cannot undo: the record is still referenced ({exc.orig})")
    return result


def undo_batch(db: Session, batch_id: str) -> dict:
    """All-or-nothing: if any change in the batch cannot be undone, nothing is undone."""
    rows = (
        db.query(models.ChangeLog)
        .filter(models.ChangeLog.batch_id == batch_id)
        .order_by(models.ChangeLog.id.desc())   # newest first so dependent rows unwind cleanly
        .all()
    )
    if not rows:
        raise HTTPException(404, "Batch not found")
    try:
        results = [_undo_row(db, r) for r in rows]
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, f"Cannot undo batch: a record is still referenced ({exc.orig})")
    return {
        "batch_id": batch_id,
        "undone": sum(1 for r in results if not r["already_undone"]),
        "already_undone": sum(1 for r in results if r["already_undone"]),
        "changes": results,
    }


# ── Endpoints ────────────────────────────────────────────────

@router.get("/changes")
def list_changes(limit: int = Query(50, ge=1, le=500), batch_id: str | None = None,
                 db: Session = Depends(get_db)):
    q = db.query(models.ChangeLog)
    if batch_id:
        q = q.filter(models.ChangeLog.batch_id == batch_id)
    rows = q.order_by(models.ChangeLog.id.desc()).limit(limit).all()
    return [{**_entry(r), "source": r.source, "created_at": r.created_at} for r in rows]


@router.post("/changes/batch/{batch_id}/undo")
def undo_batch_endpoint(batch_id: str, db: Session = Depends(get_db)):
    return undo_batch(db, batch_id)


@router.post("/changes/{change_id}/undo")
def undo_change_endpoint(change_id: int, db: Session = Depends(get_db)):
    return undo_change(db, change_id)
