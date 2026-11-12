"""
Project portfolio — what deserves attention, what is stuck, what to pause (Phase 5E).

Primitives: attention, recommendation, health_of, review, propose_pauses, create_project,
            update_project, set_status, focus, log_update, project_detail
Endpoints:  GET /portfolio, GET/POST /projects, GET /projects/{id}, PATCH /projects/{id},
            POST /projects/{id}/status, POST /projects/{id}/focus,
            POST /projects/{id}/updates

Everything here is deterministic: the ranking is computed from data the other modules
already keep, so it is the same at 23:00 as at 06:00 and costs nothing. There are no model
calls. A pause is only ever *suggested*; nothing is paused without a tap or an explicit
instruction.
"""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import business_common
import change_log
import crm
import models
from database import get_db
import user_clock

router = APIRouter()

STATUSES = ("planned", "active", "paused", "delivered", "closed", "cancelled")
TAP_STATUSES = ("paused", "active", "delivered", "closed")
KINDS = ("client", "internal")
UPDATE_KINDS = ("note", "milestone", "blocker", "unblocked", "status")
RANKED_STATUSES = ("active", "planned", "delivered")

VALUE_CAP_USD = 5000          # a project worth this much or more scores the full 3 for value
NO_ACTIVITY_DAYS = 30         # a project with no recorded activity counts as this stale
WAITING_DAYS = 7
CLOSE_AFTER_DAYS = 30
FOCUS_RANKS = 3


# ── Scoring ──────────────────────────────────────────────────

def _usd(amount, currency) -> float:
    import main
    return main._to_usd(float(amount or 0), currency)


def _waiting_days(db: Session, project: models.Project, now: datetime) -> int | None:
    """Days since Bandile last wrote to the client with no answer: the client's last
    communication is outbound and more than seven days old. None otherwise."""
    if project.client_id is None:
        return None
    last = db.query(models.Communication).filter(
        models.Communication.client_id == project.client_id,
    ).order_by(models.Communication.occurred_at.desc(), models.Communication.id.desc()).first()
    if last is None or last.direction != "out":
        return None
    days = (now - last.occurred_at).days
    return days if days > WAITING_DAYS else None


def _overdue_payment(db: Session, project: models.Project, today: date):
    """The oldest open payment past its due date for this project, or for its client."""
    rows = db.query(models.Payment).filter(models.Payment.status.in_(crm.OPEN_PAYMENT)).all()
    mine = [p for p in rows if p.due_date is not None and p.due_date < today and (
        p.project_id == project.id or (project.client_id is not None and p.client_id == project.client_id
                                       and p.project_id is None))]
    mine.sort(key=lambda p: (p.due_date, p.id))
    return mine[0] if mine else None


def attention(db: Session, project: models.Project, today: date | None = None) -> dict:
    """How much attention a project deserves right now, with the parts that make up the
    score so the screen can show why, and the single bottleneck that matters most."""
    today = today or user_clock.today()
    now = datetime.utcnow()
    value_w = round(min(1.0, _usd(project.expected_value, project.currency) / VALUE_CAP_USD) * 3, 2)

    overdue_days = None
    urgency = 0
    if project.due_date is not None:
        left = (project.due_date - today).days
        if left < 0:
            urgency, overdue_days = 4, -left
        elif left <= 7:
            urgency = 3
        elif left <= 14:
            urgency = 2
        elif left <= 30:
            urgency = 1

    idle = business_common.days_since(project.last_activity_at)
    idle = NO_ACTIVITY_DAYS if idle is None else idle
    staleness = 2 if idle > 14 else 1 if idle > 7 else 0

    reason = (project.blocked_reason or "").strip()
    blocked = 2 if reason else 0
    priority = round((6 - max(1, min(5, int(project.priority or 3)))) / 5 * 2, 2)
    waiting_days = _waiting_days(db, project, now)
    waiting = 1 if waiting_days is not None else 0

    late_payment = _overdue_payment(db, project, today)
    bottleneck = None
    if reason:
        bottleneck = f"blocked: {reason}"
    elif overdue_days:
        bottleneck = f"overdue by {overdue_days} day{'s' if overdue_days != 1 else ''}"
    elif waiting:
        bottleneck = f"waiting on client for {waiting_days} days"
    elif staleness:
        bottleneck = f"no activity for {idle} days"
    elif late_payment is not None:
        bottleneck = f"payment overdue ({crm.money_label(late_payment.amount, late_payment.currency)})"

    return {
        "score": round(value_w + urgency + staleness + blocked + priority + waiting, 2),
        "components": {"value": value_w, "urgency": urgency, "staleness": staleness,
                       "blocked": blocked, "priority": priority, "waiting": waiting},
        "bottleneck": bottleneck,
        "overdue_days": overdue_days, "idle_days": idle, "waiting_days": waiting_days,
    }


def recommendation(project: models.Project, att: dict, rank: int | None,
                   today: date | None = None) -> str:
    """close | pause | focus | maintain, checked in that order.

    close:    delivered more than 30 days ago
    pause:    active, stale for more than two weeks, nothing due within 30 days, low value
    focus:    one of the three active projects with the highest score
    maintain: everything else (a paused project stays "maintain" until it is resumed)"""
    c = att["components"]
    if project.status == "delivered":
        delivered = project.delivered_at
        if delivered is not None and (datetime.utcnow() - delivered).days > CLOSE_AFTER_DAYS:
            return "close"
        return "maintain"
    if project.status != "active":
        return "maintain"
    if c["staleness"] == 2 and c["urgency"] == 0 and c["value"] < 1.0:
        return "pause"
    if rank is not None and rank <= FOCUS_RANKS:
        return "focus"
    return "maintain"


def health_of(att: dict) -> str:
    c = att["components"]
    if c["blocked"] or att["overdue_days"]:
        return "red"
    if c["staleness"] >= 1 or c["waiting"]:
        return "amber"
    return "green"


def _entry(db: Session, project: models.Project, today: date) -> dict:
    att = attention(db, project, today)
    return {"project": crm.serialize_project(db, project), "attention": att,
            "bottleneck": att["bottleneck"], "recommendation": "maintain", "rank": None}


def review(db: Session, today: date | None = None, *, include_paused: bool = True) -> dict:
    """The ranked portfolio. Stores each project's health (derived, so not logged). The
    caller commits."""
    today = today or user_clock.today()
    projects = db.query(models.Project).filter(models.Project.status.notin_(("closed", "cancelled"))).all()
    by_id = {p.id: p for p in projects}
    entries = [_entry(db, p, today) for p in projects]
    entries.sort(key=lambda e: (-e["attention"]["score"], e["project"]["id"]))

    rank = 0
    for e in entries:
        project = by_id[e["project"]["id"]]
        if project.status == "active":
            # A project recommended for a pause does not take one of the three focus places.
            if recommendation(project, e["attention"], None, today) == "pause":
                e["recommendation"] = "pause"
            else:
                rank += 1
                e["rank"] = rank
                e["recommendation"] = recommendation(project, e["attention"], rank, today)
        else:
            e["recommendation"] = recommendation(project, e["attention"], None, today)
        health = health_of(e["attention"])
        if project.health != health:
            project.health = health
        e["project"]["health"] = health

    ranked = [e for e in entries if e["project"]["status"] in RANKED_STATUSES]
    paused = [e for e in entries if e["project"]["status"] == "paused"]
    active = [e for e in entries if e["project"]["status"] == "active"]
    return {
        "ranked": ranked,
        "paused": paused if include_paused else [],
        "pause_suggestions": [],
        "bottlenecks": [{"project_id": e["project"]["id"], "title": e["project"]["title"],
                         "bottleneck": e["bottleneck"]} for e in ranked if e["bottleneck"]],
        "totals": {
            "active": len(active), "paused": len(paused),
            "blocked": sum(1 for e in active if e["attention"]["components"]["blocked"]),
            "overdue": sum(1 for e in active if e["attention"]["overdue_days"]),
            "value_usd_active": round(sum(e["project"]["expected_value_usd"] or 0 for e in active), 2),
        },
    }


def open_pause_suggestions(db: Session) -> dict:
    """project_id → the open project_pause suggestion for it."""
    import json
    out = {}
    for s in db.query(models.Suggestion).filter(
            models.Suggestion.kind == "project_pause", models.Suggestion.status == "pending").all():
        try:
            out[int(json.loads(s.payload_json).get("project_id"))] = s
        except (ValueError, TypeError):
            continue
    return out


def propose_pauses(db: Session, result: dict | None = None) -> list[dict]:
    """One open ``project_pause`` suggestion per project the review recommends pausing.
    Returns every open pause suggestion. Nothing is paused here. The caller commits."""
    import actions
    result = result or review(db)
    existing = open_pause_suggestions(db)
    for e in result["ranked"]:
        if e["recommendation"] != "pause" or e["project"]["id"] in existing:
            continue
        actions.add_suggestion(
            db, "project_pause", {"project_id": e["project"]["id"], "title": e["project"]["title"]},
            confidence=0.8,
            reason=f"Stale for {e['attention']['idle_days']} days, low value, nothing due",
            source="assistant")
    db.flush()
    return [actions._suggestion_dict(s) for s in open_pause_suggestions(db).values()]


def full_review(db: Session, *, include_paused: bool = True) -> dict:
    """review + propose_pauses + stored health, committed. What GET /portfolio returns."""
    result = review(db, include_paused=include_paused)
    result["pause_suggestions"] = propose_pauses(db, result)
    db.commit()
    return result


# ── Write primitives (the caller commits) ────────────────────

def _touch(project: models.Project, when: datetime | None = None) -> None:
    project.last_activity_at = when or datetime.utcnow()
    project.updated_at = datetime.utcnow()


def _add_update(db: Session, project: models.Project, kind: str, text: str,
                occurred_at: datetime | None = None, source: str = "conversation"):
    row = models.ProjectUpdate(project_id=project.id, kind=kind if kind in UPDATE_KINDS else "note",
                               text=text, occurred_at=occurred_at or datetime.utcnow(),
                               source=source, created_at=datetime.utcnow())
    db.add(row)
    return row, change_log.record_create(db, row, f"{project.title[:40]}: {text[:50]}")


def create_project(db: Session, *, title: str, kind: str = "internal", client: models.Client | None = None,
                   priority: int | None = None, expected_value: float | None = None,
                   currency: str | None = None, due_date: date | None = None,
                   effort_hours_week: float | None = None, next_action: str | None = None,
                   status: str = "active") -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("a project needs a title")
    if client is not None:
        kind = "client"
    project = models.Project(
        title=title, kind=kind if kind in KINDS else "internal",
        client_id=client.id if client else None, status=status if status in STATUSES else "active",
        priority=max(1, min(5, int(priority))) if priority is not None else 3,
        expected_value=float(expected_value) if expected_value is not None else None,
        currency=crm._currency(currency, "ZMW"), due_date=due_date,
        effort_hours_week=float(effort_hours_week) if effort_hours_week is not None else None,
        next_action=(next_action or "").strip() or None, start_date=user_clock.today(),
        last_activity_at=datetime.utcnow(), created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(project)
    change = change_log.record_create(db, project, f"New project: {title[:60]}")
    return {**crm.serialize_project(db, project), "change": change, "changes": [change]}


def set_status(db: Session, project: models.Project, status: str, source: str = "ui") -> dict:
    """Change a project's status. Logged, with a ``status`` update row."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    if status == project.status:
        return {**crm.serialize_project(db, project), "change": None, "changes": []}
    before = change_log.snapshot(project)
    old = project.status
    project.status = status
    if status == "delivered" and project.delivered_at is None:
        project.delivered_at = datetime.utcnow()
    _touch(project)
    change = change_log.record_update(db, project, before, f"Project '{project.title[:50]}' → {status}")
    _row, row_change = _add_update(db, project, "status", f"{old} → {status}", source=source)
    return {**crm.serialize_project(db, project), "change": change,
            "changes": [c for c in (change, row_change) if c]}


def update_project(db: Session, project: models.Project, *, title: str | None = None,
                   kind: str | None = None, client: models.Client | None = None,
                   status: str | None = None, priority: int | None = None,
                   expected_value: float | None = None, currency: str | None = None,
                   due_date: date | None = None, effort_hours_week: float | None = None,
                   next_action: str | None = None, blocked_reason: str | None = None,
                   notes: str | None = None, source: str = "conversation") -> dict:
    """Change project fields. ``blocked_reason=""`` clears the block and writes an
    ``unblocked`` update; a non-empty reason writes a ``blocker`` update."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    before = change_log.snapshot(project)
    changes = []
    if title is not None and title.strip():
        project.title = title.strip()
    if kind in KINDS:
        project.kind = kind
    if client is not None:
        project.client_id, project.kind = client.id, "client"
    if priority is not None:
        project.priority = max(1, min(5, int(priority)))
    if expected_value is not None:
        project.expected_value = float(expected_value)
    if currency:
        project.currency = crm._currency(currency)
    if due_date is not None:
        project.due_date = due_date
    if effort_hours_week is not None:
        project.effort_hours_week = float(effort_hours_week)
    if next_action is not None:
        project.next_action = next_action.strip() or None
    if notes is not None:
        project.notes = notes.strip() or None
    extra = None
    if blocked_reason is not None:
        reason = blocked_reason.strip()
        was = (project.blocked_reason or "").strip()
        project.blocked_reason = reason or None
        if reason and reason != was:
            extra = ("blocker", reason)
        elif not reason and was:
            extra = ("unblocked", f"Unblocked (was: {was})")
    if change_log.snapshot(project) != before:
        _touch(project)
    change = change_log.record_update(db, project, before, f"Updated project: {project.title[:60]}")
    if change:
        changes.append(change)
    if extra:
        changes.append(_add_update(db, project, extra[0], extra[1], source=source)[1])
    if status is not None and status != project.status:
        changes += set_status(db, project, status, source=source)["changes"]
    return {**crm.serialize_project(db, project), "change": change, "changes": [c for c in changes if c]}


def focus(db: Session, project: models.Project) -> dict:
    """Make this the priority-1 project; every other active priority-1 project drops to 2."""
    changes = []
    for other in db.query(models.Project).filter(
            models.Project.id != project.id, models.Project.status == "active",
            models.Project.priority == 1).all():
        before = change_log.snapshot(other)
        other.priority = 2
        other.updated_at = datetime.utcnow()
        changes.append(change_log.record_update(db, other, before, f"'{other.title[:50]}' priority 1 → 2"))
    before = change_log.snapshot(project)
    project.priority = 1
    if change_log.snapshot(project) != before:
        _touch(project)
    change = change_log.record_update(db, project, before, f"Focus: {project.title[:60]}")
    return {**crm.serialize_project(db, project), "change": change,
            "changes": [c for c in [change] + changes if c]}


def log_update(db: Session, project: models.Project, text: str, kind: str = "note",
               occurred_at: datetime | None = None, source: str = "conversation") -> dict:
    """Log progress on a project. ``blocker`` sets the blocked reason, ``unblocked`` clears it."""
    text = (text or "").strip()
    if not text:
        raise ValueError("an update needs some text")
    kind = kind if kind in UPDATE_KINDS else "note"
    before = change_log.snapshot(project)
    if kind == "blocker":
        project.blocked_reason = text
    elif kind == "unblocked":
        project.blocked_reason = None
    _touch(project, occurred_at)
    row, row_change = _add_update(db, project, kind, text, occurred_at, source)
    change = change_log.record_update(db, project, before, f"Update on {project.title[:60]}")
    db.flush()
    att = attention(db, project)
    project.health = health_of(att)
    return {"id": row.id, "project_id": project.id, "project": project.title, "kind": kind,
            "text": text, "occurred_at": row.occurred_at, "blocked_reason": project.blocked_reason,
            "health": project.health, "change": row_change,
            "changes": [c for c in (row_change, change) if c]}


def serialize_update(u: models.ProjectUpdate) -> dict:
    return {"id": u.id, "project_id": u.project_id, "kind": u.kind, "text": u.text,
            "occurred_at": u.occurred_at, "source": u.source, "created_at": u.created_at}


def project_detail(db: Session, project: models.Project) -> dict:
    out = {**crm.serialize_project(db, project), "attention": attention(db, project)}
    out["updates"] = [serialize_update(u) for u in db.query(models.ProjectUpdate).filter(
        models.ProjectUpdate.project_id == project.id,
    ).order_by(models.ProjectUpdate.occurred_at.desc(), models.ProjectUpdate.id.desc()).limit(50).all()]
    payments = db.query(models.Payment).filter(models.Payment.project_id == project.id).all()
    if project.client_id is not None:
        payments += db.query(models.Payment).filter(
            models.Payment.client_id == project.client_id, models.Payment.project_id.is_(None)).all()
    out["payments"] = [crm.serialize_payment(db, p) for p in sorted(payments, key=lambda p: -p.id)]
    out["communications"] = ([crm.serialize_communication(c) for c in db.query(models.Communication).filter(
        models.Communication.client_id == project.client_id,
    ).order_by(models.Communication.occurred_at.desc(), models.Communication.id.desc()).limit(20).all()]
        if project.client_id is not None else [])
    return out


# ── Endpoints ────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    title: str
    kind: str | None = None
    client_id: int | None = None
    priority: int | None = None
    expected_value: float | None = None
    currency: str | None = None
    due_date: date | None = None
    effort_hours_week: float | None = None
    next_action: str | None = None


class ProjectPatch(BaseModel):
    title: str | None = None
    kind: str | None = None
    client_id: int | None = None
    priority: int | None = None
    expected_value: float | None = None
    currency: str | None = None
    due_date: date | None = None
    effort_hours_week: float | None = None
    next_action: str | None = None
    status: str | None = None
    blocked_reason: str | None = None
    notes: str | None = None


class StatusBody(BaseModel):
    status: str


class UpdateBody(BaseModel):
    text: str
    kind: str | None = None
    occurred_at: datetime | None = None


def _get(db: Session, project_id: int) -> models.Project:
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


def _client(db: Session, client_id: int | None):
    if client_id is None:
        return None
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if client is None:
        raise HTTPException(404, "Client not found")
    return client


@router.get("/portfolio")
def get_portfolio(include_paused: int = 1, db: Session = Depends(get_db)):
    with change_log.batch_scope(db, "job:portfolio"):
        return full_review(db, include_paused=bool(include_paused))


@router.get("/projects")
def list_projects(status: str | None = None, kind: str | None = None, client_id: int | None = None,
                  db: Session = Depends(get_db)):
    q = db.query(models.Project)
    if status:
        q = q.filter(models.Project.status == status)
    if kind:
        q = q.filter(models.Project.kind == kind)
    if client_id is not None:
        q = q.filter(models.Project.client_id == client_id)
    rows = q.order_by(models.Project.id.desc()).all()
    return [{**crm.serialize_project(db, p), "attention": attention(db, p)} for p in rows]


@router.post("/projects", status_code=201)
def post_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    data = payload.model_dump()
    client = _client(db, data.pop("client_id"))
    with change_log.batch_scope(db, "ui"):
        try:
            result = create_project(db, client=client, kind=data.pop("kind") or "internal", **data)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    return project_detail(db, _get(db, project_id))


@router.patch("/projects/{project_id}")
def patch_project(project_id: int, payload: ProjectPatch, db: Session = Depends(get_db)):
    project = _get(db, project_id)
    data = payload.model_dump(exclude_unset=True)
    client = _client(db, data.pop("client_id", None))
    with change_log.batch_scope(db, "ui"):
        try:
            result = update_project(db, project, client=client, source="ui", **data)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.post("/projects/{project_id}/status")
def post_status(project_id: int, payload: StatusBody, db: Session = Depends(get_db)):
    """The one-tap statuses: paused, active (resume), delivered, closed."""
    project = _get(db, project_id)
    if payload.status not in TAP_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(TAP_STATUSES)}")
    with change_log.batch_scope(db, "ui"):
        result = set_status(db, project, payload.status, source="ui")
        db.commit()
    return result


@router.post("/projects/{project_id}/focus")
def post_focus(project_id: int, db: Session = Depends(get_db)):
    project = _get(db, project_id)
    with change_log.batch_scope(db, "ui"):
        result = focus(db, project)
        db.commit()
    return result


@router.post("/projects/{project_id}/updates", status_code=201)
def post_update(project_id: int, payload: UpdateBody, db: Session = Depends(get_db)):
    project = _get(db, project_id)
    with change_log.batch_scope(db, "ui"):
        try:
            result = log_update(db, project, payload.text, payload.kind or "note",
                                payload.occurred_at, source="ui")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result
