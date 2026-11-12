"""
Standing research objectives — research that recurs (Phase 5A).

Primitives: serialize, is_due, due_objectives, find_objective, schedule, run_objective
Endpoints:  GET/POST /research/objectives, PATCH /research/objectives/{id},
            POST /research/objectives/{id}/run

An objective is set in conversation ("keep researching Lusaka pharmacies every two weeks").
It runs when Bandile taps Run, and on schedule once the market-intelligence agent is
switched on. A run is an ordinary research run: it surfaces findings and promotes nothing.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import business_common
import change_log
import models
from database import get_db

router = APIRouter()

DEFAULT_CADENCE_DAYS = 14
DEFAULT_MAX_OPPORTUNITIES = 3


def is_due(o: models.ResearchObjective, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if not o.enabled:
        return False
    return o.last_run_at is None or o.last_run_at + timedelta(days=o.cadence_days or DEFAULT_CADENCE_DAYS) <= now


def serialize(o: models.ResearchObjective) -> dict:
    return {"id": o.id, "objective": o.objective, "cadence_days": o.cadence_days,
            "enabled": bool(o.enabled), "max_opportunities": o.max_opportunities,
            "last_run_at": o.last_run_at, "last_project_id": o.last_project_id, "due": is_due(o)}


def due_objectives(db: Session, now: datetime | None = None) -> list:
    """Enabled objectives whose cadence has elapsed, the longest-waiting first."""
    rows = [o for o in db.query(models.ResearchObjective).filter(
        models.ResearchObjective.enabled == True).all() if is_due(o, now)]   # noqa: E712
    rows.sort(key=lambda o: (o.last_run_at is not None, o.last_run_at or datetime.min, o.id))
    return rows


def find_objective(db: Session, text: str):
    """The existing objective this text restates (fuzzy, >= 0.75 and clear of the runner-up)."""
    picked, _scored = business_common.fuzzy_pick(
        db.query(models.ResearchObjective).all(), lambda o: o.objective, text or "")
    return picked


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def schedule(db: Session, objective: str, cadence_days: int | None = None,
             max_opportunities: int | None = None) -> dict:
    """Create a standing objective, or update the one the text already describes. The
    caller commits. Returns the objective plus ``created`` and ``change``."""
    text = (objective or "").strip()
    if len(text) < 10:
        raise ValueError("give the objective as a real research question")
    cadence = _clamp(cadence_days, 1, 365, DEFAULT_CADENCE_DAYS)
    limit = _clamp(max_opportunities, 1, 5, DEFAULT_MAX_OPPORTUNITIES)
    existing = find_objective(db, text)
    if existing is not None:
        before = change_log.snapshot(existing)
        existing.cadence_days, existing.max_opportunities, existing.enabled = cadence, limit, True
        if change_log.snapshot(existing) != before:
            existing.updated_at = datetime.utcnow()
        change = change_log.record_update(db, existing, before, f"Research every {cadence} days: {existing.objective[:50]}")
        return {**serialize(existing), "created": False, "change": change}
    row = models.ResearchObjective(objective=text, cadence_days=cadence, max_opportunities=limit,
                                   enabled=True, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(row)
    change = change_log.record_create(db, row, f"Research every {cadence} days: {text[:50]}")
    return {**serialize(row), "created": True, "change": change}


def run_objective(db: Session, objective: models.ResearchObjective) -> dict:
    """Run the research agent on a standing objective and stamp the objective and the
    project. Commits (a research run commits as it goes). Returns the run's result."""
    from market_intel.agent import run_market_research
    result = run_market_research(db, objective.objective, objective.max_opportunities or DEFAULT_MAX_OPPORTUNITIES)
    project_id = result.get("project_id")
    if project_id:
        project = db.query(models.ResearchProject).filter(models.ResearchProject.id == project_id).first()
        if project is not None:
            project.objective_id = objective.id
        objective.last_run_at = datetime.utcnow()
        objective.last_project_id = project_id
        objective.updated_at = datetime.utcnow()
        db.commit()
    return {**result, "objective_id": objective.id}


# ── Endpoints ────────────────────────────────────────────────

class ObjectiveCreate(BaseModel):
    objective: str
    cadence_days: int | None = None
    max_opportunities: int | None = None


class ObjectivePatch(BaseModel):
    objective: str | None = None
    cadence_days: int | None = None
    enabled: bool | None = None
    max_opportunities: int | None = None


def _get(db: Session, objective_id: int) -> models.ResearchObjective:
    row = db.query(models.ResearchObjective).filter(models.ResearchObjective.id == objective_id).first()
    if row is None:
        raise HTTPException(404, "Research objective not found")
    return row


@router.get("/research/objectives")
def list_objectives(db: Session = Depends(get_db)):
    rows = db.query(models.ResearchObjective).order_by(models.ResearchObjective.id.desc()).all()
    return [serialize(o) for o in rows]


@router.post("/research/objectives", status_code=201)
def post_objective(payload: ObjectiveCreate, db: Session = Depends(get_db)):
    with change_log.batch_scope(db, "ui"):
        try:
            result = schedule(db, payload.objective, payload.cadence_days, payload.max_opportunities)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return result


@router.patch("/research/objectives/{objective_id}")
def patch_objective(objective_id: int, payload: ObjectivePatch, db: Session = Depends(get_db)):
    row = _get(db, objective_id)
    data = payload.model_dump(exclude_unset=True)
    with change_log.batch_scope(db, "ui"):
        before = change_log.snapshot(row)
        if data.get("objective") is not None:
            text = data["objective"].strip()
            if len(text) < 10:
                raise HTTPException(400, "give the objective as a real research question")
            row.objective = text
        if data.get("cadence_days") is not None:
            row.cadence_days = _clamp(data["cadence_days"], 1, 365, row.cadence_days)
        if data.get("max_opportunities") is not None:
            row.max_opportunities = _clamp(data["max_opportunities"], 1, 5, row.max_opportunities)
        if data.get("enabled") is not None:
            row.enabled = bool(data["enabled"])
        row.updated_at = datetime.utcnow()
        change = change_log.record_update(db, row, before, f"Updated research objective: {row.objective[:50]}")
        db.commit()
    return {**serialize(row), "change": change}


@router.post("/research/objectives/{objective_id}/run")
def run_objective_endpoint(objective_id: int, db: Session = Depends(get_db)):
    """One tap Run. Slow (a full research run). 503 when no AI key is configured."""
    import llm
    import main
    row = _get(db, objective_id)
    if not llm.is_configured():
        raise main._ai_http_error(llm.AIUnavailable("OPENAI_API_KEY is not set — AI features are disabled."))
    with change_log.batch_scope(db, "ui"):
        result = run_objective(db, row)
        changes = change_log.current_batch_changes(db)
    return {**result, "objective": serialize(row), "changes": changes}
