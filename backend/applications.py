"""
Applications — Wharton and anything like it (Phase 6A).

Primitives: find_application, create, import_requirements, add_requirement, advance,
            set_status, update_requirement, make_todo, timeline, open_gaps, review_gaps,
            set_gap_status
Endpoints:  GET/POST /applications, GET/PATCH /applications/{id},
            POST /applications/{id}/requirements/import, POST /applications/{id}/requirements,
            PATCH /requirements/{id}, POST /requirements/{id}/advance,
            POST /requirements/{id}/todo, GET /applications/{id}/gaps, PATCH /gaps/{id}

What an application requires, what is missing, when it is due and where the profile is
weak. **Dates come only from Bandile or from text he pastes.** The model is never asked
what a deadline is: a date the import cannot tie to a phrase in the pasted text is
dropped, and a date he stated himself is never overwritten by a document.
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import business_common
import change_log
import llm
import models
from database import get_db
import user_clock

router = APIRouter()

KINDS = ("university", "scholarship", "competition", "programme", "job", "other")
STATUSES = ("researching", "preparing", "submitted", "interview", "accepted", "rejected", "withdrawn")
REQ_KINDS = ("essay", "test", "transcript", "recommendation", "form", "fee", "interview", "portfolio", "other")
REQ_CYCLE = ("missing", "in_progress", "ready", "submitted")
REQ_STATUSES = REQ_CYCLE + ("not_required",)
REQ_OPEN = ("missing", "in_progress")
GAP_STATUSES = ("open", "addressed", "accepted_risk", "superseded")
SEVERITIES = ("high", "medium", "low")

IMPORT_MAX_CHARS = 12_000
AT_RISK_DAYS = 14
TODO_LEAD_DAYS = 7

# Stable prompt (cached prefix). The pasted text goes in the user message.
IMPORT_PROMPT = (
    "You turn the pasted text of an application's requirements page into a checklist. "
    "Extract only what the text states. Respond as JSON: "
    "{\"requirements\": [{\"title\": \"short name\", \"kind\": \"essay|test|transcript|"
    "recommendation|form|fee|interview|portfolio|other\", \"details\": \"word limits, prompts, "
    "who writes it, or null\", \"due_date\": \"YYYY-MM-DD or null\", \"due_text\": \"the exact "
    "phrase from the text that the date came from, or null\"}], \"deadline\": \"YYYY-MM-DD or "
    "null\", \"deadline_text\": \"the exact phrase or null\", \"decision_date\": \"YYYY-MM-DD "
    "or null\", \"decision_text\": \"the exact phrase or null\"}.\n"
    "RULE ABOUT DATES: a date may only be copied from the pasted text. If the text gives no "
    "date for something, use null. Never supply a date from your own knowledge of the "
    "institution, the programme or a typical admissions calendar. Every date you return must "
    "come with the exact phrase it was read from."
)


# ── Lookups and serialisation ────────────────────────────────

def find_application(db: Session, fragment):
    """(application | None, candidates) — an id, or a fuzzy match on name, institution, programme."""
    if isinstance(fragment, int) or (isinstance(fragment, str) and fragment.strip().isdigit()):
        app = db.query(models.Application).filter(models.Application.id == int(fragment)).first()
        return app, ([(1.0, app)] if app else [])
    return business_common.fuzzy_pick(
        db.query(models.Application).all(),
        lambda a: [a.name, a.institution, a.programme, f"{a.institution or ''} {a.programme or ''}"],
        str(fragment or ""))


def requirements_of(db: Session, app: models.Application) -> list:
    return db.query(models.ApplicationRequirement).filter(
        models.ApplicationRequirement.application_id == app.id,
    ).order_by(models.ApplicationRequirement.sort_order, models.ApplicationRequirement.id).all()


def open_gaps(db: Session, app: models.Application) -> list:
    order = {"high": 0, "medium": 1, "low": 2}
    rows = db.query(models.ApplicationGap).filter(
        models.ApplicationGap.application_id == app.id, models.ApplicationGap.status == "open").all()
    return sorted(rows, key=lambda g: (order.get(g.severity, 3), g.id))


def serialize_requirement(r: models.ApplicationRequirement) -> dict:
    return {"id": r.id, "application_id": r.application_id, "title": r.title, "kind": r.kind,
            "status": r.status, "due_date": r.due_date, "due_source": r.due_source,
            "details": r.details, "todo_id": r.todo_id, "note_id": r.note_id,
            "sort_order": r.sort_order, "created_at": r.created_at, "updated_at": r.updated_at}


def serialize_gap(g: models.ApplicationGap) -> dict:
    return {"id": g.id, "application_id": g.application_id, "gap": g.gap, "severity": g.severity,
            "evidence": g.evidence, "suggested_action": g.suggested_action, "status": g.status,
            "review_batch": g.review_batch, "created_at": g.created_at, "updated_at": g.updated_at}


def timeline(db: Session, app: models.Application, today: date | None = None) -> dict:
    """Days to the deadline, requirement counts, the next thing due, and whether it is at
    risk: a missing or in-progress requirement whose due date (or, without one, the
    application's deadline) is within 14 days."""
    today = today or user_clock.today()
    reqs = requirements_of(db, app)
    counts = {status: sum(1 for r in reqs if r.status == status) for status in REQ_CYCLE}
    pending = [r for r in reqs if r.status in REQ_OPEN]
    dated = sorted(((r.due_date or app.deadline, r) for r in pending if (r.due_date or app.deadline)),
                   key=lambda pair: (pair[0], pair[1].id))
    next_due = None
    if dated:
        due, r = dated[0]
        next_due = {"requirement_id": r.id, "title": r.title, "due_date": due,
                    "days": (due - today).days, "from_deadline": r.due_date is None}
    return {
        "days_to_deadline": (app.deadline - today).days if app.deadline else None,
        "requirements": counts, "requirements_total": len(reqs),
        "next_due": next_due,
        "at_risk": any((due - today).days <= AT_RISK_DAYS for due, _r in dated),
    }


def serialize(db: Session, app: models.Application, today: date | None = None) -> dict:
    return {"id": app.id, "name": app.name, "institution": app.institution, "programme": app.programme,
            "kind": app.kind, "deadline": app.deadline, "deadline_source": app.deadline_source or "unknown",
            "decision_date": app.decision_date, "status": app.status, "priority": app.priority,
            "portal_url": app.portal_url, "notes": app.notes,
            "requirements_updated_at": app.requirements_updated_at, "gaps_reviewed_at": app.gaps_reviewed_at,
            "created_at": app.created_at, "updated_at": app.updated_at,
            "timeline": timeline(db, app, today), "gaps_open": len(open_gaps(db, app))}


def detail(db: Session, app: models.Application) -> dict:
    return {**serialize(db, app),
            "requirements": [serialize_requirement(r) for r in requirements_of(db, app)],
            "gaps": [serialize_gap(g) for g in open_gaps(db, app)]}


# ── Write primitives (the caller commits) ────────────────────

def create(db: Session, name: str, *, institution: str | None = None, programme: str | None = None,
           kind: str = "university", deadline: date | None = None, deadline_source: str | None = None,
           priority: int = 2, portal_url: str | None = None) -> dict:
    """A deadline given here was stated by Bandile, so its source is ``user``."""
    name = (name or "").strip()
    if not name:
        raise ValueError("an application needs a name")
    app = models.Application(
        name=name, institution=(institution or "").strip() or None, programme=(programme or "").strip() or None,
        kind=kind if kind in KINDS else "other", deadline=deadline,
        deadline_source=(deadline_source or "user") if deadline else "unknown",
        status="researching", priority=max(1, min(3, int(priority or 2))),
        portal_url=(portal_url or "").strip() or None, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(app)
    change = change_log.record_create(db, app, f"New application: {name[:60]}")
    return {**serialize(db, app), "change": change}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _dated(value, phrase, source_text: str) -> date | None:
    """A date from the model is kept only when it parses and comes with a phrase that is
    really in the pasted text. Anything else is treated as invented and dropped."""
    if not value or not (phrase or "").strip():
        return None
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return parsed if _norm(phrase) and _norm(phrase) in source_text else None


def _touch_requirements(app: models.Application) -> None:
    app.requirements_updated_at = datetime.utcnow()
    app.updated_at = datetime.utcnow()


def _index(db: Session, source_type: str, row_id: int, text: str) -> None:
    try:
        import retrieval
        retrieval.index_one(db, source_type, row_id, text)
    except Exception as exc:                # noqa: BLE001 — search is optional
        print(f"[applications] index failed: {exc}")


def import_requirements(db: Session, app: models.Application, text: str) -> dict:
    """Turn pasted requirements text into the checklist (one model call). Upserts by title.
    The caller commits. Raises ``llm.AIUnavailable`` with no key, ``ValueError`` on bad input."""
    text = (text or "").strip()[:IMPORT_MAX_CHARS]
    if len(text) < 20:
        raise ValueError("paste the requirements text")
    raw = llm.chat_text(
        [{"role": "system", "content": IMPORT_PROMPT},
         {"role": "user", "content": f"APPLICATION: {app.name}\n\nPASTED TEXT:\n{text}"}],
        db=db, purpose="application_parse", max_tokens=1200, response_format={"type": "json_object"})
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("the requirements could not be read — try pasting the text again")
    if not isinstance(parsed, dict):
        raise ValueError("the requirements could not be read — try pasting the text again")
    source = _norm(text)

    existing = {r.title.strip().lower(): r for r in requirements_of(db, app)}
    order = max([r.sort_order or 0 for r in existing.values()] + [0])
    created = updated = dates_dropped = 0
    changes = []
    for item in parsed.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        title = (str(item.get("title") or "")).strip()[:200]
        if not title:
            continue
        kind = item.get("kind") if item.get("kind") in REQ_KINDS else "other"
        details = (str(item.get("details") or "")).strip() or None
        due = _dated(item.get("due_date"), item.get("due_text"), source)
        if item.get("due_date") and due is None:
            dates_dropped += 1
        row = existing.get(title.lower())
        if row is None:
            order += 1
            row = models.ApplicationRequirement(
                application_id=app.id, title=title, kind=kind, status="missing", due_date=due,
                due_source="document" if due else None, details=details, sort_order=order,
                created_at=datetime.utcnow(), updated_at=datetime.utcnow())
            db.add(row)
            changes.append(change_log.record_create(db, row, f"{app.name[:30]}: {title[:50]}"))
            existing[title.lower()] = row
            created += 1
        else:
            before = change_log.snapshot(row)
            row.kind = kind
            if details:
                row.details = details
            if due is not None and row.due_source != "user":
                row.due_date, row.due_source = due, "document"
            if change_log.snapshot(row) != before:
                row.updated_at = datetime.utcnow()
            change = change_log.record_update(db, row, before, f"{app.name[:30]}: {title[:50]} updated")
            if change:
                changes.append(change)
            updated += 1
        if row.details:
            _index(db, "application", row.id, f"{app.name} — {row.title}\n{row.details}")

    before = change_log.snapshot(app)
    deadline_set = False
    deadline = _dated(parsed.get("deadline"), parsed.get("deadline_text"), source)
    if deadline is not None and (app.deadline is None or app.deadline_source != "user"):
        app.deadline, app.deadline_source = deadline, "document"
        deadline_set = True
    decision = _dated(parsed.get("decision_date"), parsed.get("decision_text"), source)
    if decision is not None and app.decision_date is None:
        app.decision_date = decision
    if app.status == "researching" and (created or updated):
        app.status = "preparing"
    _touch_requirements(app)
    change = change_log.record_update(db, app, before, f"Requirements imported for {app.name[:50]}")
    if change:
        changes.append(change)
    return {"application_id": app.id, "application": app.name, "created": created, "updated": updated,
            "deadline_set": deadline_set, "deadline": app.deadline,
            "deadline_source": app.deadline_source or "unknown", "dates_dropped": dates_dropped,
            "changes": [c for c in changes if c], "batch_id": change_log.current_batch_id(db)}


def add_requirement(db: Session, app: models.Application, title: str, kind: str = "other",
                    due_date: date | None = None, details: str | None = None) -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("a requirement needs a title")
    order = max([r.sort_order or 0 for r in requirements_of(db, app)] + [0]) + 1
    row = models.ApplicationRequirement(
        application_id=app.id, title=title[:200], kind=kind if kind in REQ_KINDS else "other",
        status="missing", due_date=due_date, due_source="user" if due_date else None,
        details=(details or "").strip() or None, sort_order=order,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(row)
    change = change_log.record_create(db, row, f"{app.name[:30]}: {title[:50]}")
    _touch_requirements(app)
    return {**serialize_requirement(row), "change": change}


def _app_of(db: Session, req: models.ApplicationRequirement) -> models.Application:
    return db.query(models.Application).filter(models.Application.id == req.application_id).first()


def set_status(db: Session, req: models.ApplicationRequirement, status: str) -> dict:
    if status not in REQ_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(REQ_STATUSES)}")
    app = _app_of(db, req)
    before = change_log.snapshot(req)
    req.status, req.updated_at = status, datetime.utcnow()
    change = change_log.record_update(db, req, before, f"{(app.name if app else 'Application')[:30]}: {req.title[:40]} → {status.replace('_', ' ')}")
    if change and app is not None:
        _touch_requirements(app)
    return {**serialize_requirement(req), "change": change}


def advance(db: Session, req: models.ApplicationRequirement) -> dict:
    """One tap: missing → in progress → ready → submitted, then round again."""
    current = REQ_CYCLE.index(req.status) if req.status in REQ_CYCLE else -1
    return set_status(db, req, REQ_CYCLE[(current + 1) % len(REQ_CYCLE)])


def update_requirement(db: Session, req: models.ApplicationRequirement, *, title: str | None = None,
                       kind: str | None = None, status: str | None = None, due_date: date | None = None,
                       details: str | None = None) -> dict:
    if status is not None and status not in REQ_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(REQ_STATUSES)}")
    app = _app_of(db, req)
    before = change_log.snapshot(req)
    if title is not None and title.strip():
        req.title = title.strip()[:200]
    if kind in REQ_KINDS:
        req.kind = kind
    if status is not None:
        req.status = status
    if due_date is not None:
        req.due_date, req.due_source = due_date, "user"
    if details is not None:
        req.details = details.strip() or None
    if change_log.snapshot(req) != before:
        req.updated_at = datetime.utcnow()
    change = change_log.record_update(db, req, before, f"{(app.name if app else 'Application')[:30]}: {req.title[:40]} updated")
    if change and app is not None:
        _touch_requirements(app)
    if req.details:
        _index(db, "application", req.id, f"{app.name if app else ''} — {req.title}\n{req.details}")
    return {**serialize_requirement(req), "change": change}


def make_todo(db: Session, req: models.ApplicationRequirement) -> dict:
    """A dated task for a requirement: due on the requirement's date, else a week before
    the application's deadline. Idempotent while that task is still open."""
    app = _app_of(db, req)
    if req.todo_id is not None:
        todo = db.query(models.Todo).filter(models.Todo.id == req.todo_id).first()
        if todo is not None and not todo.done:
            return {"todo": _todo_dict(todo), "change": None, "existing": True}
    due = req.due_date or ((app.deadline - timedelta(days=TODO_LEAD_DAYS)) if app and app.deadline else None)
    todo = models.Todo(text=f"{app.name if app else 'Application'}: {req.title}", priority=2, done=False,
                       category="applications", source="applications",
                       due=due.isoformat() if due else None, due_date=due)
    db.add(todo)
    change = change_log.record_create(db, todo, f"Added task: {todo.text[:80]}")
    before = change_log.snapshot(req)
    req.todo_id, req.updated_at = todo.id, datetime.utcnow()
    link = change_log.record_update(db, req, before, f"Task linked to {req.title[:50]}")
    return {"todo": _todo_dict(todo), "change": change, "changes": [c for c in (change, link) if c],
            "existing": False}


def _todo_dict(todo: models.Todo) -> dict:
    return {"id": todo.id, "text": todo.text, "due_date": todo.due_date, "done": bool(todo.done),
            "category": todo.category}


def set_gap_status(db: Session, gap: models.ApplicationGap, status: str) -> dict:
    if status not in ("open", "addressed", "accepted_risk"):
        raise ValueError("status must be open, addressed or accepted_risk")
    before = change_log.snapshot(gap)
    gap.status, gap.updated_at = status, datetime.utcnow()
    change = change_log.record_update(db, gap, before, f"Gap {status.replace('_', ' ')}: {gap.gap[:50]}")
    return {**serialize_gap(gap), "change": change}


GAPS_MAX = 8
_PROFILE_PATH = Path(__file__).resolve().parent.parent / "context" / "core" / "founder_profile.md"

# Stable prompt (cached prefix). The assembled context goes in the user message.
GAPS_PROMPT = (
    "You review how ready an applicant is for one application. You are given the "
    "application's requirements with their status, the applicant's profile, his current "
    "syllabus progress, and notes and memories that mention the application. Name the gaps: "
    "where the profile or the preparation is weak relative to what this application asks "
    "for. Use only the facts given; do not assume facts about the institution and do not "
    "mention dates that are not in the input. Respond as JSON: {\"gaps\": [{\"gap\": \"one "
    "line\", \"severity\": \"high|medium|low\", \"evidence\": \"what in the input shows it\", "
    "\"suggested_action\": \"one concrete next step\"}]}. At most 8 gaps, the most serious "
    "first. An empty list is a valid answer."
)


def _review_context(db: Session, app: models.Application) -> str:
    """Everything the review may use, assembled without a model."""
    import assistant_state
    import memory_service

    lines = [f"APPLICATION: {app.name}"
             + (f" — {app.institution}" if app.institution else "")
             + (f", {app.programme}" if app.programme else ""),
             f"KIND: {app.kind}. STATUS: {app.status}.",
             "DEADLINE: " + (f"{app.deadline.isoformat()} (source: {app.deadline_source})" if app.deadline else "unknown"),
             "", "REQUIREMENTS:"]
    reqs = requirements_of(db, app)
    for r in reqs:
        lines.append(f"- [{r.status.replace('_', ' ')}] {r.title} ({r.kind})"
                     + (f" — {r.details}" if r.details else "")
                     + (f" — due {r.due_date.isoformat()}" if r.due_date else ""))
    if not reqs:
        lines.append("- none listed yet")
    try:
        profile = _PROFILE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        profile = ""
    lines += ["", "PROFILE:", profile or "not written yet", "", "SYLLABUS PROGRESS:"]
    academics = assistant_state.academics_lines(db)
    lines += [f"- {line}" for line in academics] or ["- no syllabus imported"]
    # Notes and memories by keyword, never embeddings: the context costs no model call, so a
    # review is exactly one call.
    needle = app.name.lower()
    hits = [n for n in db.query(models.Note).order_by(models.Note.updated_at.desc()).all()
            if needle in (n.title or "").lower() or needle in (n.body or "").lower()][:3]
    lines += ["", "NOTES:"]
    lines += [f"- {n.title}: {(n.body or '')[-300:]}" for n in hits] or ["- none"]
    try:
        memories = [memory_service.format_memory(m) for m in memory_service.recall_keyword(db, app.name, limit=5)
                    if needle in (m.context_data or "").lower() or needle in (m.tags or "").lower()]
    except Exception:                       # noqa: BLE001 — context is best effort
        memories = []
    lines += ["", "MEMORIES:"]
    lines += [f"- {m}" for m in memories] or ["- none"]
    return "\n".join(lines)


def _gaps_result(db: Session, app: models.Application, *, cached: bool, stale: bool = False) -> dict:
    return {"application_id": app.id, "application": app.name, "gaps_reviewed_at": app.gaps_reviewed_at,
            "cached": cached, "stale": stale, "gaps": [serialize_gap(g) for g in open_gaps(db, app)]}


def review_is_fresh(app: models.Application) -> bool:
    """True when a review exists and nothing about the requirements has changed since."""
    if app.gaps_reviewed_at is None:
        return False
    return app.requirements_updated_at is None or app.gaps_reviewed_at >= app.requirements_updated_at


def review_gaps(db: Session, app: models.Application, *, refresh: bool = False) -> dict:
    """Where the profile is weak relative to this application.

    Cached: while the last review is at least as new as the last change to the
    requirements, the stored open gaps are returned and no model call is made, unless
    ``refresh`` is set. A new review replaces the previous one: its still-open gaps become
    ``superseded``. With no AI key the stored gaps are returned (marked ``stale`` when the
    requirements have changed); ``refresh=True`` then raises ``llm.AIUnavailable``.
    The caller commits."""
    if review_is_fresh(app) and not refresh:
        return _gaps_result(db, app, cached=True)
    if not llm.is_configured():
        if refresh:
            raise llm.AIUnavailable("OPENAI_API_KEY is not set — AI features are disabled.")
        return _gaps_result(db, app, cached=True, stale=True)

    raw = llm.chat_text(
        [{"role": "system", "content": GAPS_PROMPT},
         {"role": "user", "content": _review_context(db, app)}],
        db=db, purpose="application_gaps", max_tokens=700, response_format={"type": "json_object"})
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("gaps"), list):
        raise ValueError("the review could not be read — try again")

    now = datetime.utcnow()
    batch = f"review-{app.id}-{now.strftime('%Y%m%d%H%M%S')}"
    for old in open_gaps(db, app):
        before = change_log.snapshot(old)
        old.status, old.updated_at = "superseded", now
        change_log.record_update(db, old, before, f"Gap replaced by a newer review: {old.gap[:40]}")
    for item in parsed["gaps"][:GAPS_MAX]:
        if not isinstance(item, dict):
            continue
        text = (str(item.get("gap") or "")).strip()[:300]
        if not text:
            continue
        gap = models.ApplicationGap(
            application_id=app.id, gap=text,
            severity=item.get("severity") if item.get("severity") in SEVERITIES else "medium",
            evidence=(str(item.get("evidence") or "")).strip() or None,
            suggested_action=(str(item.get("suggested_action") or "")).strip() or None,
            status="open", review_batch=batch, created_at=now, updated_at=now)
        db.add(gap)
        change_log.record_create(db, gap, f"{app.name[:30]} gap: {text[:50]}")
        if llm.current_run_id() is None:        # inside an agent run an embedding would use up its call cap
            _index(db, "application_gap", gap.id, f"{app.name} gap: {text}\n{gap.evidence or ''}")
    before = change_log.snapshot(app)
    app.gaps_reviewed_at = now
    if app.requirements_updated_at is not None and app.requirements_updated_at > now:
        app.requirements_updated_at = now
    change_log.record_update(db, app, before, f"Gap review for {app.name[:50]}")
    db.flush()
    return _gaps_result(db, app, cached=False)


# ── Endpoints ────────────────────────────────────────────────

class ApplicationCreate(BaseModel):
    name: str
    institution: str | None = None
    programme: str | None = None
    kind: str | None = None
    deadline: date | None = None
    priority: int | None = None
    portal_url: str | None = None


class ApplicationPatch(BaseModel):
    name: str | None = None
    institution: str | None = None
    programme: str | None = None
    kind: str | None = None
    deadline: date | None = None
    decision_date: date | None = None
    status: str | None = None
    priority: int | None = None
    portal_url: str | None = None
    notes: str | None = None


class ImportBody(BaseModel):
    text: str


class RequirementCreate(BaseModel):
    title: str
    kind: str | None = None
    due_date: date | None = None
    details: str | None = None


class RequirementPatch(BaseModel):
    title: str | None = None
    kind: str | None = None
    status: str | None = None
    due_date: date | None = None
    details: str | None = None


class GapPatch(BaseModel):
    status: str


def _get(db: Session, app_id: int) -> models.Application:
    app = db.query(models.Application).filter(models.Application.id == app_id).first()
    if app is None:
        raise HTTPException(404, "Application not found")
    return app


def _get_req(db: Session, req_id: int) -> models.ApplicationRequirement:
    req = db.query(models.ApplicationRequirement).filter(models.ApplicationRequirement.id == req_id).first()
    if req is None:
        raise HTTPException(404, "Requirement not found")
    return req


@router.get("/applications")
def list_applications(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(models.Application)
    if status:
        q = q.filter(models.Application.status == status)
    rows = q.all()
    rows.sort(key=lambda a: (a.deadline is None, a.deadline or date.max, a.priority or 2, a.id))
    return [serialize(db, a) for a in rows]


@router.post("/applications", status_code=201)
def post_application(payload: ApplicationCreate, db: Session = Depends(get_db)):
    with change_log.batch_scope(db, "ui"):
        try:
            result = create(db, payload.name, institution=payload.institution, programme=payload.programme,
                            kind=payload.kind or "university", deadline=payload.deadline,
                            priority=payload.priority or 2, portal_url=payload.portal_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.get("/applications/{app_id}")
def get_application(app_id: int, db: Session = Depends(get_db)):
    return detail(db, _get(db, app_id))


@router.patch("/applications/{app_id}")
def patch_application(app_id: int, payload: ApplicationPatch, db: Session = Depends(get_db)):
    app = _get(db, app_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("status") is not None and data["status"] not in STATUSES:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(STATUSES)}")
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(app)
        for key, value in data.items():
            if key == "deadline":
                app.deadline = value
                app.deadline_source = "user" if value else "unknown"      # a date he set himself
            elif key == "kind":
                app.kind = value if value in KINDS else app.kind
            elif key == "priority":
                app.priority = max(1, min(3, int(value))) if value is not None else app.priority
            elif key == "name":
                app.name = (value or "").strip() or app.name
            elif isinstance(value, str):
                setattr(app, key, value.strip() or None)
            else:
                setattr(app, key, value)
        app.updated_at = datetime.utcnow()
        change = change_log.record_update(db, app, before, f"Updated application: {app.name[:60]}")
        db.commit()
    return {**serialize(db, app), "change": change}


@router.post("/applications/{app_id}/requirements/import")
def import_endpoint(app_id: int, payload: ImportBody, db: Session = Depends(get_db)):
    app = _get(db, app_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = import_requirements(db, app, payload.text)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(400, str(exc))
        except Exception as exc:                 # noqa: BLE001
            db.rollback()
            import main
            raise main._ai_http_error(exc)
        db.commit()
    return result


@router.post("/applications/{app_id}/requirements", status_code=201)
def post_requirement(app_id: int, payload: RequirementCreate, db: Session = Depends(get_db)):
    app = _get(db, app_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = add_requirement(db, app, payload.title, payload.kind or "other", payload.due_date, payload.details)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.get("/applications/{app_id}/gaps")
def get_gaps(app_id: int, refresh: int = 0, db: Session = Depends(get_db)):
    """The gap review. Cached: no model call while the requirements have not changed since
    the last review, unless ``refresh=1``. 503 on a refresh with no AI key."""
    app = _get(db, app_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = review_gaps(db, app, refresh=bool(refresh))
        except ValueError as exc:
            db.rollback()
            raise HTTPException(502, str(exc))
        except Exception as exc:                 # noqa: BLE001
            db.rollback()
            import main
            raise main._ai_http_error(exc)
        db.commit()
    return result


@router.patch("/requirements/{req_id}")
def patch_requirement(req_id: int, payload: RequirementPatch, db: Session = Depends(get_db)):
    req = _get_req(db, req_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = update_requirement(db, req, **payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.post("/requirements/{req_id}/advance")
def advance_requirement(req_id: int, db: Session = Depends(get_db)):
    req = _get_req(db, req_id)
    with change_log.batch_scope(db, "ui"):
        result = advance(db, req)
        db.commit()
    return result


@router.post("/requirements/{req_id}/todo")
def requirement_todo(req_id: int, db: Session = Depends(get_db)):
    req = _get_req(db, req_id)
    with change_log.batch_scope(db, "ui"):
        result = make_todo(db, req)
        db.commit()
    return result


@router.patch("/gaps/{gap_id}")
def patch_gap(gap_id: int, payload: GapPatch, db: Session = Depends(get_db)):
    gap = db.query(models.ApplicationGap).filter(models.ApplicationGap.id == gap_id).first()
    if gap is None:
        raise HTTPException(404, "Gap not found")
    with change_log.batch_scope(db, "ui"):
        try:
            result = set_gap_status(db, gap, payload.status)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result
