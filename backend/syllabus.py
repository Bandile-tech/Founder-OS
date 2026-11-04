"""
Syllabus + exam dates.

Stage 3 provides exam-date resolution and the ``set_exam_date`` primitive
(exam_dates table keyed on (subject_code, paper), falling back to Subject.exam_date).
Stage 4 adds syllabus_points, the CSV importer, the endpoints and the tools.
"""

import re
from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
import change_log

# Subject aliases → code. Codes themselves always resolve.
SUBJECT_ALIASES = {
    "9709": "9709", "maths": "9709", "math": "9709", "mathematics": "9709", "pure maths": "9709",
    "9231": "9231", "further maths": "9231", "further mathematics": "9231", "fm": "9231", "further": "9231",
    "9609": "9609", "business": "9609", "business studies": "9609",
    "9708": "9708", "economics": "9708", "econ": "9708", "econs": "9708",
}
SUBJECT_NAMES = {"9709": "Maths 9709", "9231": "Further Maths 9231", "9609": "Business 9609", "9708": "Economics 9708"}

_PAPER_RE = re.compile(r"\b(?:paper\s*)?([PpSsMm]\s?\d|\d)\b")


def normalise_paper(paper: str | None) -> str | None:
    """'P1', 'paper 1', 's 1' → 'P1' / 'S1'. None/'' → None (whole subject)."""
    if paper is None:
        return None
    p = str(paper).strip().upper().replace("PAPER", "").replace(" ", "")
    if not p:
        return None
    if p.isdigit():
        return f"P{p}"
    return p


def resolve_subject_code(db: Session, text: str) -> str | None:
    """Subject code from a code, an alias or a fragment of the subject name."""
    t = (text or "").strip().lower()
    if not t:
        return None
    # longest alias first so 'further maths' beats 'maths'
    for alias in sorted(SUBJECT_ALIASES, key=len, reverse=True):
        if alias in t:
            return SUBJECT_ALIASES[alias]
    subj = db.query(models.Subject).filter(func.lower(models.Subject.name).contains(t)).first()
    if subj:
        return subj.code
    subj = db.query(models.Subject).filter(models.Subject.code == t).first()
    return subj.code if subj else None


def subject_name(db: Session, code: str) -> str:
    subj = db.query(models.Subject).filter(models.Subject.code == code).first()
    return subj.name if subj else SUBJECT_NAMES.get(code, code)


def get_exam_date(db: Session, subject_code: str, paper: str | None = None) -> date | None:
    """Paper-level row → subject-level row → Subject.exam_date → None."""
    paper = normalise_paper(paper)
    if paper:
        row = db.query(models.ExamDate).filter(
            models.ExamDate.subject_code == subject_code, models.ExamDate.paper == paper
        ).first()
        if row:
            return row.exam_date
    row = db.query(models.ExamDate).filter(
        models.ExamDate.subject_code == subject_code, models.ExamDate.paper.is_(None)
    ).first()
    if row:
        return row.exam_date
    subj = db.query(models.Subject).filter(models.Subject.code == subject_code).first()
    return subj.exam_date if subj else None


def set_exam_date(db: Session, subject: str, exam_date: date, paper: str | None = None) -> dict:
    """Upsert an exam date. Records the change. Caller commits."""
    code = resolve_subject_code(db, subject)
    if code is None:
        raise ValueError(f"Unknown subject '{subject}'. Use a code (9709) or a name (Further Maths).")
    paper = normalise_paper(paper)
    q = db.query(models.ExamDate).filter(models.ExamDate.subject_code == code)
    q = q.filter(models.ExamDate.paper == paper) if paper else q.filter(models.ExamDate.paper.is_(None))
    row = q.first()
    label = f"{subject_name(db, code)}{' ' + paper if paper else ''}"
    if row is None:
        row = models.ExamDate(subject_code=code, paper=paper, exam_date=exam_date, updated_at=datetime.utcnow())
        db.add(row)
        db.flush()
        change_log.record_create(db, row, f"Exam date for {label}: {exam_date.isoformat()}")
    else:
        before = change_log.snapshot(row)
        row.exam_date = exam_date
        row.updated_at = datetime.utcnow()
        change_log.record_update(db, row, before, f"Exam date for {label}: {exam_date.isoformat()}")
    return {"subject_code": code, "subject": subject_name(db, code), "paper": paper,
            "exam_date": exam_date.isoformat(), "days_away": (exam_date - date.today()).days}


def upcoming_exams(db: Session, today: date | None = None) -> list[dict]:
    """Every known exam date (paper rows plus subject fallbacks), soonest first."""
    today = today or date.today()
    out: dict[tuple, dict] = {}
    for row in db.query(models.ExamDate).all():
        out[(row.subject_code, row.paper)] = {
            "subject_code": row.subject_code, "subject": subject_name(db, row.subject_code),
            "paper": row.paper, "exam_date": row.exam_date,
        }
    for subj in db.query(models.Subject).all():
        if subj.exam_date and not any(k[0] == subj.code for k in out):
            out[(subj.code, None)] = {"subject_code": subj.code, "subject": subj.name,
                                      "paper": None, "exam_date": subj.exam_date}
    items = []
    for v in out.values():
        items.append({**v, "exam_date": v["exam_date"].isoformat(), "days_away": (v["exam_date"] - today).days})
    items.sort(key=lambda x: x["exam_date"])
    return items


def parse_date_text(text: str, today: date | None = None) -> date | None:
    """'12 May', '2026-05-12', '12/05/2026', 'May 12' → date (assumes the next occurrence
    for day-month forms without a year)."""
    today = today or date.today()
    t = (text or "").strip()
    if not t:
        return None
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d %b, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    for fmt in ("%d %b", "%d %B", "%b %d", "%B %d"):
        try:
            d = datetime.strptime(t, fmt).date().replace(year=today.year)
            return d if d >= today else d.replace(year=today.year + 1)
        except ValueError:
            continue
    return None
