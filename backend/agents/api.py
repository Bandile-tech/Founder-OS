"""
Agents API — the registry, toggles, Run now and the activity log.

Endpoints: GET /agents, GET /agents/runs, GET /agents/{key}, PATCH /agents/{key},
           POST /agents/{key}/run, GET /agents/{key}/runs, POST /agents/runs/{id}/undo

The registry in code says what an agent is; the ``agents`` table holds its toggle, schedule
overrides and last-run facts. A toggle is change-logged, so it can be undone.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import agents
import change_log
import models
from agents import runner, schedule
from database import get_db

router = APIRouter()


def _spec_or_404(key: str):
    spec = agents.REGISTRY.get(key)
    if spec is None:
        raise HTTPException(404, "Agent not found")
    return spec


def _month_cost(db: Session, key: str) -> float:
    start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.query(models.AgentRun).filter(
        models.AgentRun.agent_key == key, models.AgentRun.started_at >= start).all()
    return round(sum(r.cost_usd or 0.0 for r in rows), 6)


def serialize_agent(db: Session, spec, row=None) -> dict:
    row = row or runner.agent_row(db, spec)
    eff = schedule.effective(spec, row)
    last = db.query(models.AgentRun).filter(
        models.AgentRun.agent_key == spec.key, models.AgentRun.status != "running",
    ).order_by(models.AgentRun.id.desc()).first()
    p = spec.permissions
    return {
        "key": spec.key, "name": spec.name, "description": spec.description, "phase": spec.phase,
        "enabled": bool(row.enabled),
        "schedule": {"interval_minutes": eff.interval_minutes, "at_time": eff.at_time,
                     "weekdays": eff.weekdays, "effective": schedule.describe(eff),
                     "overridden": bool(row.interval_minutes or row.at_time or row.weekdays_json)},
        "events": list(spec.events or []),
        "permissions": {"tools": list(p.tools), "tables_write": list(p.tables_write),
                        "may_notify": p.may_notify, "may_emit": list(p.may_emit),
                        "max_llm_calls": p.max_llm_calls, "max_seconds": p.max_seconds},
        "last_run": ({"at": row.last_run_at, "status": row.last_status, "summary": row.last_summary,
                      "cost_usd": last.cost_usd if last else 0.0} if row.last_run_at else None),
        "runs_count": row.runs_count or 0, "cost_usd_total": row.cost_usd_total or 0.0,
        "cost_usd_month": _month_cost(db, spec.key),
        "next_due_estimate": (schedule.next_due(spec, row, schedule.to_local(datetime.utcnow()))
                              if row.enabled else None),
    }


def _runs(db: Session, key: str | None = None, status: str | None = None, limit: int = 20,
          with_changes: bool = False) -> list:
    q = db.query(models.AgentRun)
    if key:
        q = q.filter(models.AgentRun.agent_key == key)
    if status:
        q = q.filter(models.AgentRun.status == status)
    out = []
    for run in q.order_by(models.AgentRun.id.desc()).limit(max(1, min(int(limit), 200))).all():
        item = runner.serialize_run(run)
        spec = agents.REGISTRY.get(run.agent_key)
        item["agent_name"] = spec.name if spec else run.agent_key
        if with_changes:
            rows = (db.query(models.ChangeLog).filter(models.ChangeLog.batch_id == run.batch_id)
                    .order_by(models.ChangeLog.id).limit(10).all() if run.batch_id else [])
            item["changes"] = [change_log._entry(r) for r in rows]
        out.append(item)
    return out


def list_agents(db: Session) -> list:
    return [serialize_agent(db, spec) for spec in agents.ordered_specs()]


# ── Endpoints ────────────────────────────────────────────────

class AgentPatch(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    at_time: str | None = None
    weekdays: list[int] | None = None
    config: dict | None = None


@router.get("/agents")
def get_agents(db: Session = Depends(get_db)):
    return list_agents(db)


# Declared before /agents/{key} so "runs" is not read as an agent key.
@router.get("/agents/runs")
def get_all_runs(limit: int = 50, status: str | None = None, db: Session = Depends(get_db)):
    """The activity log across agents, newest first, each with its changes (at most ten)."""
    return _runs(db, status=status, limit=limit, with_changes=True)


@router.post("/agents/runs/{run_id}/undo")
def undo_run(run_id: int, db: Session = Depends(get_db)):
    run = db.query(models.AgentRun).filter(models.AgentRun.id == run_id).first()
    if run is None:
        raise HTTPException(404, "Run not found")
    if not run.batch_id or not run.changes_count:
        raise HTTPException(409, "This run changed nothing")
    return change_log.undo_batch(db, run.batch_id)


@router.get("/agents/{key}")
def get_agent(key: str, db: Session = Depends(get_db)):
    spec = _spec_or_404(key)
    row = runner.agent_row(db, spec)
    return {**serialize_agent(db, spec, row), "config": runner._config(spec, row),
            "runs": _runs(db, key=key, limit=20, with_changes=True)}


@router.patch("/agents/{key}")
def patch_agent(key: str, payload: AgentPatch, db: Session = Depends(get_db)):
    spec = _spec_or_404(key)
    with change_log.batch_scope(db, "ui"):
        try:
            out = runner.update_agent(db, spec, **payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        db.commit()
    return {**serialize_agent(db, spec, out["row"]), "change": out["change"]}


@router.post("/agents/{key}/run")
def run_agent_now(key: str):
    """Run now, whatever the schedule or the toggle say."""
    return runner.run_agent(_spec_or_404(key), "manual")


@router.get("/agents/{key}/runs")
def get_agent_runs(key: str, limit: int = 20, db: Session = Depends(get_db)):
    _spec_or_404(key)
    return _runs(db, key=key, limit=limit, with_changes=True)
