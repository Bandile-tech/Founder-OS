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
import user_clock

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
            "exam_date": exam_date.isoformat(), "days_away": (exam_date - user_clock.today()).days}


def upcoming_exams(db: Session, today: date | None = None) -> list[dict]:
    """Every known exam date (paper rows plus subject fallbacks), soonest first."""
    today = today or user_clock.today()
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
    today = today or user_clock.today()
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


# ════════════════════════════════════════════════════════════════
# Stage 4 — syllabus_points: statuses, CSV import, endpoints, tools
# ════════════════════════════════════════════════════════════════

import csv
import difflib
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from database import get_db

router = APIRouter()

STATUSES = ["not_started", "learned", "revised", "bulletproof"]
STATUS_COLOURS = {"not_started": "red", "learned": "yellow", "revised": "blue", "bulletproof": "green"}
STATUS_SCORE = {"not_started": 0.0, "learned": 1 / 3, "revised": 2 / 3, "bulletproof": 1.0}

_TRUE = {"true", "t", "yes", "y", "x", "1", "✓", "✔", "done"}
_HEADER_KEYS = {
    "topic": "topic", "learned": "learned", "revised": "revised", "bulletproof": "bulletproof",
    "past paper score %": "score", "past paper score": "score", "score": "score", "score %": "score",
    "last revised": "last_revised", "weakness notes": "notes", "notes": "notes", "weakness": "notes",
    "paper": "paper",
}
_TOPIC_CODE_RE = re.compile(r"^\s*([A-Za-z]?\d+(?:\.\d+)*[a-z]?)\s*[\.\:\-–—)]?\s+(.+)$")


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in _TRUE


def _is_header(row: list[str]) -> bool:
    cells = [c.strip().lower() for c in row if c and c.strip()]
    return "topic" in cells and any(c in ("learned", "revised", "bulletproof") for c in cells)


def split_topic(cell: str) -> tuple[str, str]:
    """'1.3 Coordinate geometry' → ('1.3', 'Coordinate geometry'). No code → slug of title."""
    cell = (cell or "").strip()
    m = _TOPIC_CODE_RE.match(cell)
    if m:
        return m.group(1), m.group(2).strip()
    slug = re.sub(r"[^a-z0-9]+", "-", cell.lower()).strip("-")[:40]
    return slug or "topic", cell


def status_from_flags(learned, revised, bulletproof) -> str:
    """Highest of Learned / Revised / Bulletproof that is TRUE."""
    if _truthy(bulletproof):
        return "bulletproof"
    if _truthy(revised):
        return "revised"
    if _truthy(learned):
        return "learned"
    return "not_started"


def _score(value) -> float | None:
    s = str(value or "").strip().replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_tracker_csv(text: str, papers: list[str] | None = None) -> list[dict]:
    """Parse the tracker sheet export into row dicts.

    * The header row can repeat; each repeat starts a new block.
    * Without a Paper column, blocks map to ``papers`` in order.
    * Blank rows are ignored; everything from a row starting 'Legend' onwards is dropped.
    """
    reader = csv.reader(io.StringIO(text))
    rows: list[dict] = []
    columns: dict[int, str] | None = None
    block = -1
    papers = [normalise_paper(p) for p in (papers or []) if p and p.strip()]
    for raw in reader:
        if not raw or not any((c or "").strip() for c in raw):
            continue
        first = (raw[0] or "").strip().lower()
        if first.startswith("legend"):
            break
        if _is_header(raw):
            columns = {}
            for i, cell in enumerate(raw):
                key = _HEADER_KEYS.get((cell or "").strip().lower())
                if key:
                    columns[i] = key
            block += 1
            continue
        if columns is None:
            continue   # junk before the first header
        rec = {"topic": "", "learned": "", "revised": "", "bulletproof": "", "score": "", "last_revised": "", "notes": "", "paper": ""}
        for i, key in columns.items():
            if i < len(raw):
                rec[key] = (raw[i] or "").strip()
        if not rec["topic"]:
            continue
        paper = normalise_paper(rec["paper"]) if rec["paper"] else None
        if paper is None and papers:
            paper = papers[min(block, len(papers) - 1)]
        code, title = split_topic(rec["topic"])
        last = None
        if rec["last_revised"]:
            from migrations.m012_notes_currency_retrieval import parse_due
            last = parse_due(rec["last_revised"])
        rows.append({
            "paper": paper, "topic_code": code, "title": title,
            "status": status_from_flags(rec["learned"], rec["revised"], rec["bulletproof"]),
            "past_paper_score": _score(rec["score"]), "last_revised": last,
            "weakness_notes": rec["notes"] or None,
        })
    return rows


def import_rows(db: Session, rows: list[dict], subject_code: str, subject_name: str,
                level: str | None) -> dict:
    """Upsert parsed rows. Re-importing never duplicates. Returns counts."""
    created = updated = skipped = 0
    for r in rows:
        q = db.query(models.SyllabusPoint).filter(
            models.SyllabusPoint.subject_code == subject_code,
            models.SyllabusPoint.topic_code == r["topic_code"],
        )
        q = q.filter(models.SyllabusPoint.paper == r["paper"]) if r["paper"] else q.filter(models.SyllabusPoint.paper.is_(None))
        existing = q.first()
        if existing is None:
            db.add(models.SyllabusPoint(
                subject_code=subject_code, subject_name=subject_name, level=level, paper=r["paper"],
                topic_code=r["topic_code"], title=r["title"], status=r["status"],
                past_paper_score=r["past_paper_score"], last_revised=r["last_revised"],
                weakness_notes=r["weakness_notes"], updated_at=datetime.utcnow(),
            ))
            created += 1
            continue
        changed = False
        for field in ("title", "status", "past_paper_score", "last_revised", "weakness_notes", "level"):
            new = r.get(field) if field != "level" else level
            if field == "level" and new is None:
                continue
            if getattr(existing, field) != new:
                setattr(existing, field, new)
                changed = True
        if existing.subject_name != subject_name:
            existing.subject_name = subject_name
            changed = True
        if changed:
            existing.updated_at = datetime.utcnow()
            updated += 1
        else:
            skipped += 1
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped, "total_rows": len(rows)}


def import_csv(db: Session, text: str, subject_code: str, subject_name: str, level: str | None,
               papers: list[str] | None) -> dict:
    rows = parse_tracker_csv(text, papers)
    result = import_rows(db, rows, subject_code, subject_name, level)
    result["papers"] = sorted({r["paper"] or "" for r in rows})
    return result


# ── Progress + grouping ──────────────────────────────────────

def serialize_point(p: models.SyllabusPoint) -> dict:
    return {
        "id": p.id, "subject_code": p.subject_code, "subject_name": p.subject_name, "level": p.level,
        "paper": p.paper, "topic_code": p.topic_code, "title": p.title, "status": p.status,
        "colour": STATUS_COLOURS.get(p.status, "red"), "past_paper_score": p.past_paper_score,
        "last_revised": p.last_revised.isoformat() if p.last_revised else None,
        "weakness_notes": p.weakness_notes, "updated_at": p.updated_at,
    }


def progress_of(points: list[models.SyllabusPoint]) -> dict:
    counts = {s: 0 for s in STATUSES}
    for p in points:
        counts[p.status if p.status in counts else "not_started"] += 1
    n = len(points)
    pct = round(100 * sum(STATUS_SCORE.get(p.status, 0) for p in points) / n) if n else 0
    return {"total": n, "pct": pct, "bulletproof_pct": round(100 * counts["bulletproof"] / n) if n else 0, "counts": counts}


def grouped(db: Session) -> dict:
    points = db.query(models.SyllabusPoint).order_by(
        models.SyllabusPoint.subject_code, models.SyllabusPoint.paper, models.SyllabusPoint.topic_code
    ).all()
    subjects: dict[str, dict] = {}
    for p in points:
        subj = subjects.setdefault(p.subject_code, {
            "subject_code": p.subject_code, "subject_name": p.subject_name, "level": p.level,
            "exam_date": None, "papers": {}, "_points": [],
        })
        subj["_points"].append(p)
        paper = subj["papers"].setdefault(p.paper or "", {"paper": p.paper, "exam_date": None, "points": [], "_points": []})
        paper["points"].append(serialize_point(p))
        paper["_points"].append(p)
    out = []
    for code, subj in subjects.items():
        ex = get_exam_date(db, code)
        subj["exam_date"] = ex.isoformat() if ex else None
        papers = []
        for paper in subj["papers"].values():
            pex = get_exam_date(db, code, paper["paper"])
            paper["exam_date"] = pex.isoformat() if pex else None
            paper["progress"] = progress_of(paper.pop("_points"))
            papers.append(paper)
        subj["papers"] = papers
        subj["progress"] = progress_of(subj.pop("_points"))
        out.append(subj)
    return {"subjects": out, "progress": progress_of(points)}


def academics_pct(db: Session) -> int | None:
    """Overall syllabus progress %, or None when nothing is imported yet."""
    points = db.query(models.SyllabusPoint).all()
    return progress_of(points)["pct"] if points else None


# ── Mutations ────────────────────────────────────────────────

def advance(db: Session, point_id: int) -> models.SyllabusPoint:
    p = db.query(models.SyllabusPoint).filter(models.SyllabusPoint.id == point_id).first()
    if p is None:
        raise HTTPException(404, "Syllabus point not found")
    before = change_log.snapshot(p)
    idx = STATUSES.index(p.status) if p.status in STATUSES else 0
    p.status = STATUSES[(idx + 1) % len(STATUSES)]
    if p.status in ("revised", "bulletproof"):
        p.last_revised = user_clock.today()
    p.updated_at = datetime.utcnow()
    change_log.record_update(db, p, before, f"{p.subject_name} {p.paper or ''} {p.topic_code} → {p.status}".replace("  ", " "))
    return p


def match_topic(db: Session, query: str, subject: str | None = None, paper: str | None = None) -> list[tuple[float, models.SyllabusPoint]]:
    """Fuzzy topic match by code or title. Highest score first."""
    q = db.query(models.SyllabusPoint)
    code = resolve_subject_code(db, subject) if subject else None
    if code:
        q = q.filter(models.SyllabusPoint.subject_code == code)
    paper_n = normalise_paper(paper)
    if paper_n:
        q = q.filter(models.SyllabusPoint.paper == paper_n)
    text = (query or "").strip().lower()
    if not text:
        return []
    words = {w for w in re.findall(r"[a-z0-9.]+", text) if len(w) > 2}
    scored = []
    for p in q.all():
        title = p.title.lower()
        if text == p.topic_code.lower() or text == title:
            scored.append((1.0, p))
            continue
        tw = {w for w in re.findall(r"[a-z0-9.]+", title) if len(w) > 2}
        overlap = len(words & tw) / min(len(words), len(tw)) if words and tw else 0.0
        ratio = difflib.SequenceMatcher(None, text, title).ratio()
        contain = 0.95 if (text in title or title in text) else 0.0
        code_hit = 0.9 if p.topic_code.lower() in words else 0.0
        score = max(overlap * 0.9, ratio, contain, code_hit)
        if score >= 0.5:
            scored.append((round(score, 3), p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def update_point(db: Session, p: models.SyllabusPoint, *, status: str | None = None,
                 weakness_note: str | None = None, past_paper_score: float | None = None,
                 revised_today: bool = True) -> dict:
    before = change_log.snapshot(p)
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        p.status = status
    if revised_today and (status in ("revised", "bulletproof") or weakness_note or past_paper_score is not None):
        p.last_revised = user_clock.today()
    if weakness_note:
        line = f"- {user_clock.today().isoformat()}: {weakness_note.strip()}"
        p.weakness_notes = (p.weakness_notes.rstrip() + "\n" + line) if (p.weakness_notes or "").strip() else line
    if past_paper_score is not None:
        p.past_paper_score = float(past_paper_score)
    p.updated_at = datetime.utcnow()
    change = change_log.record_update(
        db, p, before, f"{p.subject_name} {p.paper or ''} {p.topic_code} {p.title}: {p.status}".replace("  ", " "))
    return {**serialize_point(p), "change": change}


def update_syllabus(db: Session, topic: str, status: str | None = None, weakness_note: str | None = None,
                    subject: str | None = None, paper: str | None = None, past_paper_score: float | None = None) -> dict:
    """Conversation → syllabus. One clear topic match is updated; otherwise candidates are returned."""
    scored = match_topic(db, topic, subject, paper)
    if not scored:
        return {"matched": False, "topic": topic, "candidates": []}
    top, second = scored[0][0], (scored[1][0] if len(scored) > 1 else 0.0)
    if top >= 0.7 and (top - second) >= 0.15 or top == 1.0:
        result = update_point(db, scored[0][1], status=status, weakness_note=weakness_note, past_paper_score=past_paper_score)
        return {"matched": True, **result}
    return {"matched": False, "ambiguous": True, "topic": topic,
            "candidates": [{"id": p.id, "topic_code": p.topic_code, "title": p.title, "paper": p.paper,
                            "subject": p.subject_name, "score": s} for s, p in scored[:5]]}


# ── Weakest topics ───────────────────────────────────────────

def weakest_topics(db: Session, limit: int = 5, today: date | None = None) -> list[dict]:
    """Rank topics by status, staleness of last_revised, weakness notes and exam proximity."""
    today = today or user_clock.today()
    out = []
    for p in db.query(models.SyllabusPoint).all():
        status_w = {"not_started": 3.0, "learned": 2.0, "revised": 1.0, "bulletproof": 0.0}.get(p.status, 3.0)
        if p.last_revised:
            stale_days = (today - p.last_revised).days
        else:
            stale_days = 60
        staleness = min(stale_days, 60) / 20          # 0 … 3
        notes_w = 0.75 if (p.weakness_notes or "").strip() else 0.0
        exam = get_exam_date(db, p.subject_code, p.paper)
        if exam:
            days = max((exam - today).days, 0)
            proximity = 1.0 + max(0.0, (90 - min(days, 90)) / 90) * 1.5   # 1.0 far … 2.5 imminent
        else:
            days, proximity = None, 1.0
        if p.status == "bulletproof" and not notes_w:
            continue
        score = round((status_w + staleness + notes_w) * proximity, 2)
        reasons = []
        reasons.append({"not_started": "not started", "learned": "learned but weak", "revised": "revised but not 100%",
                        "bulletproof": "bulletproof"}[p.status if p.status in STATUSES else "not_started"])
        if p.last_revised:
            reasons.append(f"last revised {stale_days}d ago")
        else:
            reasons.append("never revised")
        if notes_w:
            reasons.append("has weakness notes")
        if days is not None:
            reasons.append(f"exam in {days}d")
        out.append({**serialize_point(p), "score": score, "reason": ", ".join(reasons), "exam_days_away": days})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


# ── Endpoints ────────────────────────────────────────────────

class SyllabusPatch(BaseModel):
    status: str | None = None
    weakness_notes: str | None = None
    past_paper_score: float | None = None
    last_revised: date | None = None
    title: str | None = None


@router.get("/syllabus")
def get_syllabus(db: Session = Depends(get_db)):
    return grouped(db)


@router.get("/syllabus/weakest")
def get_weakest(limit: int = Query(5, ge=1, le=50), db: Session = Depends(get_db)):
    return weakest_topics(db, limit)


@router.post("/syllabus/import")
async def import_syllabus(
    file: UploadFile = File(...),
    subject_code: str = Form(...),
    subject_name: str = Form(...),
    level: str | None = Form(None),
    papers: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Multipart: file (CSV), subject_code, subject_name, level (AS/A2), optional papers 'P1,S1'."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    paper_list = [p.strip() for p in papers.split(",")] if papers else None
    with change_log.batch_scope(db, "ui"):
        result = import_csv(db, text, subject_code.strip(), subject_name.strip(), (level or "").strip() or None, paper_list)
    return result


@router.post("/syllabus/{point_id}/advance")
def advance_endpoint(point_id: int, db: Session = Depends(get_db)):
    with change_log.batch_scope(db, "ui"):
        p = advance(db, point_id)
        db.commit()
    return serialize_point(p)


@router.patch("/syllabus/{point_id}")
def patch_endpoint(point_id: int, payload: SyllabusPatch, db: Session = Depends(get_db)):
    p = db.query(models.SyllabusPoint).filter(models.SyllabusPoint.id == point_id).first()
    if p is None:
        raise HTTPException(404, "Syllabus point not found")
    data = payload.model_dump(exclude_none=True)
    if "status" in data and data["status"] not in STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(STATUSES)}")
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(p)
        for k, v in data.items():
            setattr(p, k, v)
        p.updated_at = datetime.utcnow()
        change_log.record_update(db, p, before, f"Edited {p.subject_name} {p.topic_code}")
        db.commit()
    return serialize_point(p)
