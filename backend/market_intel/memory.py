"""
Lesson memory for the Market Intelligence Agent.

One lesson per note: slug (identity), summary (the one-line at the top),
content (markdown body). Notes are upserted by slug so a repeated lesson
updates the existing note (and bumps ``times_reinforced``) instead of
duplicating. Stored in the DB rather than loose files because Render's disk
is ephemeral.

Scope discipline: only research lessons live here — industries repeatedly
showing strong pain, problems already investigated, failed assumptions,
patterns discovered. Anything Founder OS already stores elsewhere (KPIs,
doctrine, todos) stays where it is.
"""

import re
from datetime import datetime

import models

_MAX_DIGEST_NOTES = 40


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:80] or "untitled-lesson"


def memory_digest(db) -> str:
    """One line per note (``slug: summary``) — injected into the Planner prompt."""
    notes = (
        db.query(models.ResearchMemoryNote)
        .order_by(models.ResearchMemoryNote.updated_at.desc())
        .limit(_MAX_DIGEST_NOTES)
        .all()
    )
    if not notes:
        return "(no research lessons recorded yet)"
    return "\n".join(f"- {n.slug}: {n.summary}" for n in notes)


def upsert_lessons(db, lessons: list) -> int:
    """Insert or update lesson notes. Each lesson: {slug?, summary, content}.

    Returns the number of notes written. Invalid entries are skipped.
    """
    written = 0
    for lesson in lessons or []:
        summary = (lesson.get("summary") or "").strip()
        content = (lesson.get("content") or "").strip()
        if not summary or not content:
            continue
        slug = _slugify(lesson.get("slug") or summary)

        note = db.query(models.ResearchMemoryNote).filter(
            models.ResearchMemoryNote.slug == slug
        ).first()
        if note:
            note.summary = summary
            note.content = content
            note.times_reinforced = (note.times_reinforced or 1) + 1
            note.updated_at = datetime.utcnow()
        else:
            db.add(models.ResearchMemoryNote(
                slug=slug, summary=summary, content=content, times_reinforced=1,
            ))
        written += 1
    if written:
        db.commit()
    return written
