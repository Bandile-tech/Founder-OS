"""GET /usage/summary — model spend from the llm_usage table: by day, month, model,
provider and task, plus the month so far against the cap."""

from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

import llm
import models
from database import get_db

router = APIRouter()


def _bucket(rows, key_fn, name: str = "period"):
    buckets: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "input_tokens": 0, "cached_input_tokens": 0,
        "output_tokens": 0, "estimated_cost_usd": 0.0,
    })
    for r in rows:
        b = buckets[key_fn(r)]
        b["calls"] += 1
        b["input_tokens"] += r.input_tokens or 0
        b["cached_input_tokens"] += r.cached_input_tokens or 0
        b["output_tokens"] += r.output_tokens or 0
        b["estimated_cost_usd"] += r.estimated_cost_usd or 0.0
    out = []
    for key in sorted(buckets, reverse=True):
        b = buckets[key]
        b["estimated_cost_usd"] = round(b["estimated_cost_usd"], 6)
        out.append({name: key, **b})
    return out


def _provider(row) -> str:
    return row.provider or llm._provider_of(row.model or "")


def _task(row) -> str:
    return row.task or llm.task_for(row.purpose)


@router.get("/usage/summary")
def usage_summary(days: int = Query(90, ge=1, le=730), db: Session = Depends(get_db)):
    from datetime import datetime, timedelta
    floor = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(models.LLMUsage)
        .filter(models.LLMUsage.created_at >= floor)
        .order_by(models.LLMUsage.created_at.asc())
        .all()
    )
    by_day = _bucket(rows, lambda r: r.created_at.date().isoformat() if r.created_at else "unknown")
    by_month = _bucket(rows, lambda r: r.created_at.strftime("%Y-%m") if r.created_at else "unknown")
    by_model = _bucket(rows, lambda r: r.model or "unknown", "model")
    by_provider = _bucket(rows, _provider, "provider")
    by_task = _bucket(rows, _task, "task")
    for t in by_task:
        t["label"] = llm.TASKS.get(t["task"], t["task"])
    total = {
        "calls": len(rows),
        "input_tokens": sum(r.input_tokens or 0 for r in rows),
        "cached_input_tokens": sum(r.cached_input_tokens or 0 for r in rows),
        "output_tokens": sum(r.output_tokens or 0 for r in rows),
        "estimated_cost_usd": round(sum(r.estimated_cost_usd or 0.0 for r in rows), 6),
        "fallbacks": sum(1 for r in rows if r.fallback_from),
    }
    return {"days": days, "total": total, "by_day": by_day, "by_month": by_month, "by_model": by_model,
            "by_provider": by_provider, "by_task": by_task, "cap": llm.cap_state(db, fresh=True)}
