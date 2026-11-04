"""
Notes — markdown notes of kind book / bible / journal / inbox / general.

Primitives (used by the parser, the tools and the endpoints):
  find_note, create_note, append_entry, inbox_add, book_entry, bible_entry,
  search_notes (meaning-based when a key exists, keyword otherwise)

Endpoints:
  GET   /notes?kind=&q=        list (optionally filtered / searched)
  GET   /notes/{id}
  POST  /notes                 {title, kind, body, tags}
  PATCH /notes/{id}            {title?, kind?, body?, tags?}
  POST  /notes/{id}/append     {text, date?}
"""

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
import change_log
from database import get_db

router = APIRouter()

NOTE_KINDS = ("book", "bible", "journal", "inbox", "general")
INBOX_TITLE = "Inbox"
BIBLE_TITLE = "Bible"


def _embed(db: Session, note: models.Note) -> None:
    """Keep the note's vector current. Never raises."""
    try:
        import retrieval
        retrieval.index_one(db, "note", note.id, f"{note.title}\n{note.body}")
    except Exception as exc:  # noqa: BLE001
        print(f"[notes] embedding skipped: {exc}")


def serialize(n: models.Note, *, body: bool = True) -> dict:
    out = {
        "id": n.id, "title": n.title, "kind": n.kind,
        "tags": [t for t in (n.tags or "").split(",") if t],
        "created_at": n.created_at, "updated_at": n.updated_at,
    }
    if body:
        out["body"] = n.body
    else:
        out["preview"] = (n.body or "")[:200]
    return out


def find_note(db: Session, title: str, kind: str | None = None) -> models.Note | None:
    q = db.query(models.Note).filter(func.lower(models.Note.title) == title.strip().lower())
    if kind:
        q = q.filter(models.Note.kind == kind)
    return q.first()


def find_note_fuzzy(db: Session, fragment: str, kind: str | None = None) -> models.Note | None:
    """Exact title first, then case-insensitive containment either way."""
    frag = (fragment or "").strip().lower()
    if not frag:
        return None
    exact = find_note(db, frag, kind)
    if exact:
        return exact
    q = db.query(models.Note)
    if kind:
        q = q.filter(models.Note.kind == kind)
    for n in q.all():
        t = n.title.lower()
        if frag in t or t in frag:
            return n
    return None


def create_note(db: Session, title: str, kind: str = "general", body: str = "",
                tags: list[str] | None = None, *, summary: str | None = None) -> models.Note:
    kind = kind if kind in NOTE_KINDS else "general"
    note = models.Note(
        title=title.strip(), kind=kind, body=body or "",
        tags=",".join(t.strip() for t in tags if t and t.strip()) if tags else None,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(note)
    db.flush()
    change_log.record_create(db, note, summary or f"Created note '{note.title}'")
    _embed(db, note)
    return note


def append_entry(db: Session, title: str, kind: str, text: str, *, on: date | None = None,
                 create_missing: bool = True, summary: str | None = None,
                 fuzzy: bool = False) -> models.Note:
    """Append a dated entry to the note, creating it when missing."""
    note = find_note_fuzzy(db, title, kind) if fuzzy else find_note(db, title, kind)
    stamp = (on or date.today()).isoformat()
    line = f"- {stamp}: {text.strip()}"
    if note is None:
        if not create_missing:
            raise ValueError(f"note '{title}' not found")
        return create_note(db, title, kind, body=line,
                           summary=summary or f"Created note '{title}' with an entry")
    before = change_log.snapshot(note)
    note.body = (note.body.rstrip() + "\n" + line) if note.body.strip() else line
    note.updated_at = datetime.utcnow()
    change_log.record_update(db, note, before, summary or f"Appended to note '{note.title}'")
    _embed(db, note)
    return note


def inbox_add(db: Session, text: str, *, on: date | None = None) -> models.Note:
    return append_entry(db, INBOX_TITLE, "inbox", text, on=on,
                        summary=f"Added idea to Inbox: {text.strip()[:60]}")


def book_entry(db: Session, title: str, text: str, *, on: date | None = None) -> models.Note:
    """Reading progress / reflection on a book. The note is titled after the book and
    matched fuzzily so 'Deep Work' finds 'Deep Work — Cal Newport'. Created if missing."""
    book = db.query(models.Book).filter(func.lower(models.Book.title).contains(title.strip().lower())).first()
    note_title = book.title if book else title.strip()
    return append_entry(db, note_title, "book", text, on=on, fuzzy=True,
                        summary=f"Reading note on '{note_title}': {text.strip()[:50]}")


def bible_entry(db: Session, book: str, chapter: int, notes_text: str | None, *, on: date | None = None) -> models.Note:
    text = f"{book} {chapter}" + (f" — {notes_text.strip()}" if notes_text else "")
    return append_entry(db, BIBLE_TITLE, "bible", text, on=on,
                        summary=f"Bible note: {book} {chapter}")


def search_notes(db: Session, query: str, kind: str | None = None, limit: int = 8) -> list[dict]:
    """Meaning-based when embeddings are available, keyword otherwise."""
    query = (query or "").strip()
    q = db.query(models.Note)
    if kind:
        q = q.filter(models.Note.kind == kind)
    if not query:
        return [serialize(n, body=False) for n in q.order_by(models.Note.updated_at.desc()).limit(limit).all()]

    try:
        import retrieval
        hits = retrieval.search(db, query, source_types=["note"], limit=limit * 2)
    except Exception:  # noqa: BLE001
        hits = None
    if hits is not None:
        by_id = {n.id: n for n in q.all()}
        out = []
        for h in hits:
            n = by_id.get(h["source_id"])
            if n is not None:
                out.append({**serialize(n, body=False), "score": h["score"]})
            if len(out) >= limit:
                break
        if out:
            return out

    ql = query.lower()
    words = [w for w in ql.split() if len(w) > 2]
    scored = []
    for n in q.all():
        hay = f"{n.title}\n{n.body}".lower()
        score = (3 if ql in n.title.lower() else 0) + (1 if ql in hay else 0) + sum(1 for w in words if w in hay)
        if score:
            scored.append((score, n))
    scored.sort(key=lambda t: (t[0], t[1].updated_at or datetime.min), reverse=True)
    return [serialize(n, body=False) for _, n in scored[:limit]]


# ── Endpoints ────────────────────────────────────────────────

class NoteCreate(BaseModel):
    title: str
    kind: str = "general"
    body: str = ""
    tags: list[str] | None = None


class NotePatch(BaseModel):
    title: Optional[str] = None
    kind: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[list[str]] = None


class NoteAppend(BaseModel):
    text: str
    date: Optional[date] = None


@router.get("/notes")
def list_notes(kind: str | None = None, q: str | None = None,
               limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    if q:
        return search_notes(db, q, kind=kind, limit=limit)
    query = db.query(models.Note)
    if kind:
        query = query.filter(models.Note.kind == kind)
    rows = query.order_by(models.Note.updated_at.desc()).limit(limit).all()
    return [serialize(n, body=False) for n in rows]


@router.get("/notes/{note_id}")
def get_note(note_id: int, db: Session = Depends(get_db)):
    n = db.query(models.Note).filter(models.Note.id == note_id).first()
    if n is None:
        raise HTTPException(404, "Note not found")
    return serialize(n)


@router.post("/notes", status_code=201)
def create_note_endpoint(payload: NoteCreate, db: Session = Depends(get_db)):
    if not payload.title.strip():
        raise HTTPException(400, "Title required")
    with change_log.batch_scope(db, "ui"):
        n = create_note(db, payload.title, payload.kind, payload.body, payload.tags)
        db.commit()
    return serialize(n)


@router.patch("/notes/{note_id}")
def patch_note(note_id: int, payload: NotePatch, db: Session = Depends(get_db)):
    n = db.query(models.Note).filter(models.Note.id == note_id).first()
    if n is None:
        raise HTTPException(404, "Note not found")
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(n)
        data = payload.model_dump(exclude_none=True)
        if "tags" in data:
            data["tags"] = ",".join(t.strip() for t in data["tags"] if t.strip()) or None
        if "kind" in data and data["kind"] not in NOTE_KINDS:
            raise HTTPException(400, f"kind must be one of {', '.join(NOTE_KINDS)}")
        for k, v in data.items():
            setattr(n, k, v)
        n.updated_at = datetime.utcnow()
        change_log.record_update(db, n, before, f"Edited note '{n.title}'")
        _embed(db, n)
        db.commit()
    return serialize(n)


@router.post("/notes/{note_id}/append")
def append_note_endpoint(note_id: int, payload: NoteAppend, db: Session = Depends(get_db)):
    n = db.query(models.Note).filter(models.Note.id == note_id).first()
    if n is None:
        raise HTTPException(404, "Note not found")
    with change_log.batch_scope(db, "ui"):
        append_entry(db, n.title, n.kind, payload.text, on=payload.date)
        db.commit()
    db.refresh(n)
    return serialize(n)
