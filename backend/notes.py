"""
Notes — markdown notes of kind book / bible / journal / inbox / general.

Stage 2 provides the append/create primitives (the parser's Inbox lives here).
Stage 3 adds the endpoints, search and the assistant tools.
"""

from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
import change_log

NOTE_KINDS = ("book", "bible", "journal", "inbox", "general")
INBOX_TITLE = "Inbox"


def find_note(db: Session, title: str, kind: str | None = None) -> models.Note | None:
    q = db.query(models.Note).filter(func.lower(models.Note.title) == title.strip().lower())
    if kind:
        q = q.filter(models.Note.kind == kind)
    return q.first()


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
    return note


def append_entry(db: Session, title: str, kind: str, text: str, *, on: date | None = None,
                 create_missing: bool = True, summary: str | None = None) -> models.Note:
    """Append a dated entry to the note, creating it when missing."""
    note = find_note(db, title, kind)
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
    return note


def inbox_add(db: Session, text: str, *, on: date | None = None) -> models.Note:
    return append_entry(db, INBOX_TITLE, "inbox", text, on=on,
                        summary=f"Added idea to Inbox: {text.strip()[:60]}")
