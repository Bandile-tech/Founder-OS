"""
Migration 012 — Brain-dump rework (Rebuild Stage 3).

revenue:
  + currency VARCHAR DEFAULT 'USD' — existing rows backfilled to 'USD'
todos:
  + due_date DATE — backfilled in Python from the legacy free-text ``due`` column
                    (ISO and a few common formats; anything else stays NULL).
                    Never cast in SQL: non-ISO strings would fail on Postgres.
New tables:
  embeddings  — shared vector store (vault / note / memory), JSON vectors
  exam_dates  — exam date per (subject_code, paper)

One-off data copy (idempotent): each Book becomes a note of kind 'book' and the
daily Bible log becomes dated entries in a 'Bible' note. Originals are untouched.

SQLite + Postgres. Idempotent. No drops. RLS enabled on the new tables (Postgres).
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from database import engine, SessionLocal
from migrations._helpers import (
    dialect, add_column_if_missing, create_table_if_missing, create_index, enable_rls, table_exists,
)

TAG = "m012"

_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS embeddings (
    id           INTEGER PRIMARY KEY {autoincrement},
    source_type  VARCHAR NOT NULL,
    source_id    INTEGER NOT NULL,
    chunk_index  INTEGER DEFAULT 0,
    content_hash VARCHAR NOT NULL,
    model        VARCHAR NOT NULL,
    vector_json  TEXT    NOT NULL,
    updated_at   DATETIME
)
"""

_EXAM_DATES = """
CREATE TABLE IF NOT EXISTS exam_dates (
    id           INTEGER PRIMARY KEY {autoincrement},
    subject_code VARCHAR NOT NULL,
    paper        VARCHAR,
    exam_date    DATE    NOT NULL,
    updated_at   DATETIME
)
"""

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y")


def parse_due(value) -> date | None:
    """Best-effort date from a legacy free-text due value. None when unparseable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _backfill_due_dates(conn, d: str) -> int:
    rows = conn.execute(text("SELECT id, due FROM todos WHERE due_date IS NULL AND due IS NOT NULL")).fetchall()
    count = 0
    for row_id, due in rows:
        parsed = parse_due(due)
        if parsed is None:
            continue
        conn.execute(text("UPDATE todos SET due_date = :d WHERE id = :i"), {"d": parsed, "i": row_id})
        count += 1
    return count


def _copy_reading_into_notes() -> dict:
    """Books → one note each (kind 'book'); daily Bible log → entries in a 'Bible' note."""
    import models
    import notes as notes_mod

    db = SessionLocal()
    copied = {"books": 0, "bible_entries": 0}
    try:
        for book in db.query(models.Book).order_by(models.Book.id).all():
            if notes_mod.find_note(db, book.title, "book") is not None:
                continue
            body_lines = [f"Author: {book.author or 'unknown'}", f"Status: {book.status}"]
            if book.total_pages:
                body_lines.append(f"Progress: page {book.page or 0} of {book.total_pages}")
            note = models.Note(title=book.title, kind="book", body="\n".join(body_lines),
                               tags="imported", created_at=datetime.utcnow(), updated_at=datetime.utcnow())
            db.add(note)
            copied["books"] += 1

        if notes_mod.find_note(db, notes_mod.BIBLE_TITLE, "bible") is None:
            logs = db.query(models.DailyBibleLog).order_by(models.DailyBibleLog.date, models.DailyBibleLog.id).all()
            if logs:
                lines = []
                for l in logs:
                    entry = f"- {l.date.isoformat()}: {l.book} {l.chapter}"
                    if l.notes:
                        entry += f" — {l.notes}"
                    lines.append(entry)
                db.add(models.Note(title=notes_mod.BIBLE_TITLE, kind="bible", body="\n".join(lines),
                                   tags="imported", created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
                copied["bible_entries"] = len(logs)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — a copy failure must not block startup
        db.rollback()
        print(f"[{TAG}] notes copy skipped: {exc}")
    finally:
        db.close()
    return copied


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            if add_column_if_missing(conn, d, "revenue", "currency", "VARCHAR DEFAULT 'USD'", TAG):
                pass
            if table_exists(conn, d, "revenue"):
                conn.execute(text("UPDATE revenue SET currency = 'USD' WHERE currency IS NULL"))

            add_column_if_missing(conn, d, "todos", "due_date", "DATE", TAG)
            if table_exists(conn, d, "todos"):
                n = _backfill_due_dates(conn, d)
                if n:
                    print(f"[{TAG}] Backfilled due_date on {n} todos.")

            created = []
            if create_table_if_missing(conn, d, "embeddings", _EMBEDDINGS, TAG):
                created.append("embeddings")
            if create_table_if_missing(conn, d, "exam_dates", _EXAM_DATES, TAG):
                created.append("exam_dates")
            create_index(conn, "ix_embeddings_source", "embeddings", "source_type, source_id")
            create_index(conn, "ix_embeddings_hash", "embeddings", "source_type, content_hash")
            create_index(conn, "ux_exam_dates_subject_paper", "exam_dates", "subject_code, paper", unique=True)
            enable_rls(conn, d, created, TAG)
        print(f"[{TAG}] Schema migration complete on {d}.")
    finally:
        conn.close()

    if table_exists(engine.connect(), engine.dialect.name, "notes"):
        copied = _copy_reading_into_notes()
        if copied["books"] or copied["bible_entries"]:
            print(f"[{TAG}] Copied {copied['books']} books and {copied['bible_entries']} Bible entries into notes.")


if __name__ == "__main__":
    run()
