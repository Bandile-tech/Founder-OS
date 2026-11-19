"""
Model routes: which provider and model each task uses, and how that is changed.

  GET   /models/routes            every route, the providers, the known models, the cap
  PATCH /models/routes/{task}     change one route

Changing a route is one function, ``set_route``. The ``set_model_route`` tool ("use Claude
for chat"), the PATCH endpoint and Settings all call it. Every change is written through
the change log, so it shows with Undo like any other change, and the gateway's cached copy
is reloaded from the session that made the write.

Naming a provider without a model ("use Claude for chat") takes the counterpart of the
model the route has now (``llm.counterpart``: the seeded pairs, or ``FALLBACK_MAP``).
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import change_log
import llm
import models
from database import get_db

router = APIRouter()

_PROVIDER_WORDS = {
    "openai": "openai", "gpt": "openai", "chatgpt": "openai", "open ai": "openai",
    "anthropic": "anthropic", "claude": "anthropic",
}
_TASK_WORDS = {
    "chat": "assistant", "conversation": "assistant", "the assistant": "assistant", "assistant": "assistant",
    "typed chat": "assistant", "parse": "parser", "parsing": "parser", "brain dump": "parser",
    "briefing": "briefing_summary", "summary": "briefing_summary", "research": "market_intel",
    "market intelligence": "market_intel", "agent": "agents", "embeddings": "embedding",
    "voice": "realtime",
}
ALL_WORDS = ("all", "everything", "every task")


def provider_label(provider: str) -> str:
    adapter = llm.PROVIDERS.get(provider)
    return adapter.LABEL if adapter else provider


def normalise_provider(value) -> str | None:
    if not value:
        return None
    word = str(value).strip().lower()
    return _PROVIDER_WORDS.get(word) or (word if word in llm.PROVIDERS else None)


def normalise_task(value) -> str | None:
    if not value:
        return None
    word = str(value).strip().lower().replace("-", "_")
    if word in llm.TASKS:
        return word
    return _TASK_WORDS.get(word.replace("_", " "))


def _row(db: Session, task: str) -> models.ModelRoute:
    llm.routes(db)                                       # seeds an empty table
    row = db.query(models.ModelRoute).filter(models.ModelRoute.task == task).first()
    if row is None:
        raise ValueError(f"no route for '{task}'")
    return row


def _params(row: models.ModelRoute) -> dict:
    try:
        value = json.loads(row.params_json) if row.params_json else {}
    except (ValueError, TypeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _month_by_task(db: Session) -> dict:
    """task → (estimated USD, calls) since the first of the user's month."""
    import user_clock
    from agents import schedule
    today = user_clock.today()
    start = schedule.to_utc(datetime(today.year, today.month, 1, tzinfo=schedule.tz()))
    rows = db.query(models.LLMUsage.task, models.LLMUsage.purpose,
                    func.coalesce(func.sum(models.LLMUsage.estimated_cost_usd), 0.0), func.count(models.LLMUsage.id)
                    ).filter(models.LLMUsage.created_at >= start).group_by(
        models.LLMUsage.task, models.LLMUsage.purpose).all()
    out: dict = {}
    for task, purpose, cost, calls in rows:
        key = task or llm.task_for(purpose)
        spent, count = out.get(key, (0.0, 0))
        out[key] = (spent + float(cost or 0), count + int(calls or 0))
    return out


def serialize_route(row: models.ModelRoute, spend: dict | None = None, cap: dict | None = None) -> dict:
    spent, calls = (spend or {}).get(row.task, (0.0, 0))
    downgraded = bool(cap and row.task in (cap.get("downgraded_tasks") or []))
    other = None if row.task in llm.OPENAI_ONLY else llm.counterpart(row.model)
    return {
        "task": row.task, "label": llm.TASKS.get(row.task, row.task),
        "provider": row.provider, "provider_label": provider_label(row.provider),
        "model": row.model, "params": _params(row),
        "openai_only": row.task in llm.OPENAI_ONLY,
        "key_set": llm.provider_configured(row.provider),
        "fallback_model": other,
        "fallback_ready": bool(other and llm.provider_configured(llm._provider_of(other))),
        "serving_model": cap["cheapest_model"] if downgraded else row.model,
        "month_to_date_usd": round(spent, 4), "calls_this_month": calls,
        "updated_at": row.updated_at,
    }


def overview(db: Session) -> dict:
    llm.routes(db)
    rows = {r.task: r for r in db.query(models.ModelRoute).all()}
    spend = _month_by_task(db)
    cap = llm.cap_state(db, fresh=True)
    known = llm.known_models(db)
    return {
        "routes": [serialize_route(rows[t], spend, cap) for t in llm.TASKS if t in rows],
        "providers": [{"name": name, "label": adapter.LABEL, "key_env": adapter.KEY_ENV,
                       "configured": adapter.configured()} for name, adapter in llm.PROVIDERS.items()],
        "models": [{"model": m["model"], "provider": m["provider"], "provider_label": provider_label(m["provider"]),
                    "kind": m["kind"], "input_per_mtok": m["input"], "cached_input_per_mtok": m["cached_input"],
                    "cache_write_per_mtok": m["cache_write"], "output_per_mtok": m["output"],
                    "fallback_model": m["fallback_model"], "confirmed": m["confirmed"], "note": m["note"]}
                   for m in sorted(known.values(), key=lambda m: (m["provider"], m["model"]))],
        "cap": cap,
    }


def _target(db: Session, row: models.ModelRoute, provider: str | None, model: str | None) -> tuple[str, str]:
    """The (provider, model) a request means, or ValueError saying why it cannot be done."""
    known = llm.known_models(db)
    chat_models = sorted(m for m, info in known.items() if info["kind"] == "chat")
    if model:
        wanted = str(model).strip()
        info = known.get(wanted) or known.get(wanted.lower())
        if info is None:
            raise ValueError(f"'{wanted}' is not a model on file. Known chat models: {', '.join(chat_models)}")
        if provider and info["provider"] != provider:
            raise ValueError(f"{info['model']} is served by {provider_label(info['provider'])}, "
                             f"not {provider_label(provider)}")
        if row.task not in llm.OPENAI_ONLY and info["kind"] != "chat":
            raise ValueError(f"{info['model']} is not a chat model")
        provider, model = info["provider"], info["model"]
    elif provider:
        if provider == row.provider:
            return row.provider, row.model
        model = llm.counterpart(row.model)
        if model is None or llm._provider_of(model) != provider:
            on_provider = [m for m in chat_models if known[m]["provider"] == provider]
            raise ValueError(f"{row.model} has no counterpart on {provider_label(provider)}. "
                             f"Name a model: {', '.join(on_provider) or 'none on file'}")
    else:
        return row.provider, row.model
    if row.task in llm.OPENAI_ONLY and provider != "openai":
        raise ValueError(f"{llm.TASKS[row.task]} runs on OpenAI only: "
                         f"{provider_label(provider)} has no API for it")
    return provider, model


def set_route(db: Session, task: str, *, provider: str | None = None, model: str | None = None,
              params: dict | None = None, source: str = "assistant") -> dict:
    """Change one route. Returns ``{route, change, summary, changed}``. Raises ValueError
    for a request that cannot be met. Commits."""
    if task not in llm.TASKS:
        raise ValueError(f"unknown task '{task}'. Tasks: {', '.join(llm.TASKS)}")
    if provider and normalise_provider(provider) is None:
        raise ValueError(f"unknown provider '{provider}'. Providers: OpenAI, Claude")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    row = _row(db, task)
    new_provider, new_model = _target(db, row, normalise_provider(provider), model)
    was = f"{row.model} on {provider_label(row.provider)}"
    label = llm.TASKS[task]
    with change_log.batch_scope(db, source):
        before = change_log.snapshot(row)
        if (new_provider, new_model) != (row.provider, row.model):
            row.provider, row.model = new_provider, new_model
            if params is None:
                # A new model starts from its own defaults (for Claude: thinking off).
                params = llm._default_params(new_model)
        if params is not None:
            row.params_json = json.dumps(params, sort_keys=True) if params else None
        if change_log.snapshot(row) != before:
            row.updated_at = datetime.utcnow()
        now = f"{row.model} on {provider_label(row.provider)}"
        summary = f"{label} now uses {now} (was {was})" if now != was else f"{label}: parameters updated"
        change = change_log.record_update(db, row, before, summary)
        db.commit()
    llm.load_routes(db)
    result = {"route": serialize_route(row), "change": change, "changed": change is not None,
              "summary": summary if change is not None else f"{label} already uses {was}"}
    if not llm.provider_configured(row.provider):
        adapter = llm.PROVIDERS.get(row.provider)
        result["warning"] = (f"{adapter.KEY_ENV if adapter else 'The provider key'} is not set on the server, so "
                             f"until it is, these calls go to the counterpart model or fail.")
    return result


def set_routes(db: Session, task, *, provider=None, model=None, source: str = "assistant") -> tuple[dict, str]:
    """The ``set_model_route`` tool: one task, or every chat task for "all"."""
    word = str(task or "assistant").strip().lower()
    tasks = list(llm.CHAT_TASKS) if word in ALL_WORDS else [normalise_task(word)]
    if tasks == [None]:
        msg = f"unknown task '{task}'. Tasks: {', '.join(llm.TASKS)}, or all"
        return {"error": msg}, msg
    if not provider and not model:
        msg = "say which provider (OpenAI or Claude) or which model"
        return {"error": msg}, msg
    done, summaries, warnings = [], [], []
    with change_log.batch_scope(db, source):
        for name in tasks:
            try:
                out = set_route(db, name, provider=provider, model=model, source=source)
            except ValueError as exc:
                if len(tasks) == 1:
                    return {"error": str(exc)}, str(exc)
                summaries.append(f"{llm.TASKS[name]}: {exc}")
                continue
            done.append(out["route"])
            summaries.append(out["summary"])
            if out.get("warning") and out["warning"] not in warnings:
                warnings.append(out["warning"])
    result = {"routes": [{"task": r["task"], "what": r["label"], "provider": r["provider_label"],
                          "model": r["model"]} for r in done], "summary": "; ".join(summaries)}
    if warnings:
        result["warning"] = " ".join(warnings)
    return result, result["summary"]


# ── Endpoints ────────────────────────────────────────────────

class RoutePatch(BaseModel):
    provider: str | None = None
    model: str | None = None
    params: dict | None = None


@router.get("/models/routes")
def get_routes(db: Session = Depends(get_db)):
    return overview(db)


@router.patch("/models/routes/{task}")
def patch_route(task: str, payload: RoutePatch, db: Session = Depends(get_db)):
    if task not in llm.TASKS:
        raise HTTPException(404, f"Unknown task '{task}'")
    if payload.provider is None and payload.model is None and payload.params is None:
        raise HTTPException(400, "Give a provider, a model or params")
    try:
        return set_route(db, task, provider=payload.provider, model=payload.model, params=payload.params,
                         source="ui")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
