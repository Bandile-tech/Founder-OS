"""GET /usage/summary — OpenAI spend by day and by month from the llm_usage table."""

from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

import models
from database import get_db

router = APIRouter()


def _bucket(rows, key_fn):
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
        out.append({"period": key, **b})
    return out


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
    by_model = _bucket(rows, lambda r: r.model or "unknown")
    for m in by_model:
        m["model"] = m.pop("period")
    total = {
        "calls": len(rows),
        "input_tokens": sum(r.input_tokens or 0 for r in rows),
        "cached_input_tokens": sum(r.cached_input_tokens or 0 for r in rows),
        "output_tokens": sum(r.output_tokens or 0 for r in rows),
        "estimated_cost_usd": round(sum(r.estimated_cost_usd or 0.0 for r in rows), 6),
    }
    return {"days": days, "total": total, "by_day": by_day, "by_month": by_month, "by_model": by_model}
