"""
The model gateway. Every model call the backend makes goes through here.

  complete(task, messages, …)   one answer, as a ``Completion``
  stream(task, messages, tools) the same call streamed, as normalized deltas
  embed_texts / transcribe / speech   the OpenAI-only calls

A call names a TASK, not a model. The route for the task (``model_routes``: provider,
model, parameters) says who answers it. Routes and the models Founder OS knows
(``model_prices``: provider, price, counterpart on the other provider) are seeded by
migration m026 and changed through ``model_routes.py`` (the ``set_model_route`` tool, the
API, undo). No model id, price or key is written in this file.

What the gateway adds around a call:
  * keys: each provider works with only its own key. With no key for the route's provider
    the call goes to its counterpart; with no key at all it raises ``AIUnavailable``
    (HTTP 503) and everything that is not AI keeps working
  * fallback: a provider error, a timeout or a refusal is retried once on the other
    provider at the matching tier (``FALLBACK_MAP``), and the retry is logged
  * the monthly cap: above 80 percent of ``MONTHLY_LLM_CAP_USD`` a route that is not
    already the cheapest configured model is served by the cheapest one
  * usage: one ``llm_usage`` row per call, with provider, task and estimated cost

The SDKs live in ``llm_adapters``. Tests never reach a provider: they patch ``chat_text`` /
``embed_texts`` / ``openai_client._stream_completion``, or hand an adapter a fake client.
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime

from dotenv import load_dotenv

from llm_adapters import Completion, anthropic_adapter, openai_adapter

load_dotenv()

PROVIDERS = {openai_adapter.NAME: openai_adapter, anthropic_adapter.NAME: anthropic_adapter}

# Every task that has a route. The label is what Settings shows.
TASKS = {
    "assistant": "Assistant (chat)",
    "parser": "Brain-dump parser",
    "conversation_summary": "Conversation summary",
    "briefing_summary": "Daily briefing summary",
    "market_intel": "Market intelligence",
    "agents": "Agents and business tools",
    "analysis": "Analysis and proactive brief",
    "embedding": "Search embeddings",
    "transcribe": "Voice-note transcription",
    "speech": "Spoken replies",
    "realtime": "Real-time voice",
    "realtime_transcribe": "Real-time voice transcription",
}
# Tasks only OpenAI can serve (Claude has no embeddings, audio or real-time voice API).
OPENAI_ONLY = {"embedding", "transcribe", "speech", "realtime", "realtime_transcribe"}
CHAT_TASKS = [t for t in TASKS if t not in OPENAI_ONLY]
CAP_THRESHOLD = 0.8

_PURPOSE_TASK = {
    "assistant": "assistant", "parse": "parser", "parser": "parser",
    "conversation_summary": "conversation_summary", "briefing_summary": "briefing_summary",
    "chat": "analysis", "proactive_brief": "analysis",
    "lead_qualify": "agents", "outreach_draft": "agents", "application_parse": "agents",
    "application_gaps": "agents", "brand_insight": "agents",
    "realtime_session": "realtime", "transcribe": "transcribe", "speech": "speech",
}


def task_for(purpose: str | None) -> str:
    """The route a purpose (the label a call site logs under) belongs to."""
    purpose = purpose or "assistant"
    if purpose in TASKS:
        return purpose
    if purpose in _PURPOSE_TASK:
        return _PURPOSE_TASK[purpose]
    if purpose.startswith("market_intel"):
        return "market_intel"
    if purpose.startswith("embed"):
        return "embedding"
    return "assistant"


class AIUnavailable(RuntimeError):
    """Raised when an AI feature is requested but no provider that can serve it has a key."""


class BudgetExceeded(RuntimeError):
    """Raised when an agent run tries to make more model calls than its cap allows."""


class _Refused(RuntimeError):
    """A provider declined to answer; the call is retried on the other provider."""


# ── Agent run context ────────────────────────────────────────
# While an agent runs, every model call is counted against its cap and attributed to its
# run. The context is process-wide: runs are sequential, so an assistant turn served during
# an agent run would be attributed to it (a cost-only inaccuracy, accepted in ADR-0003).
_RUN_CONTEXT: dict | None = None


def set_run_context(run_id: int, max_calls: int | None = None) -> None:
    global _RUN_CONTEXT
    _RUN_CONTEXT = {"run_id": run_id, "max_calls": max_calls, "calls": 0}


def clear_run_context() -> None:
    global _RUN_CONTEXT
    _RUN_CONTEXT = None


def current_run_id() -> int | None:
    return _RUN_CONTEXT["run_id"] if _RUN_CONTEXT else None


def _count_call() -> None:
    """Count one model call against the active agent run; refuse it past the cap."""
    ctx = _RUN_CONTEXT
    if ctx is None:
        return
    cap = ctx.get("max_calls")
    if cap is not None and ctx["calls"] >= cap:
        raise BudgetExceeded(f"this run may make at most {cap} model call(s)")
    ctx["calls"] += 1


# ── Keys ─────────────────────────────────────────────────────

def provider_configured(provider: str) -> bool:
    adapter = PROVIDERS.get(provider)
    return bool(adapter and adapter.configured())


def api_key() -> str | None:
    """The OpenAI key (real-time voice mints its browser secret with it)."""
    return openai_adapter.key()


def reset_client() -> None:
    """Drop the cached provider clients (tests, key rotation)."""
    for adapter in PROVIDERS.values():
        adapter.reset()


# ── Routes and models (cached; written only through model_routes.py) ─────────

@dataclass
class Route:
    task: str
    provider: str
    model: str
    params: dict


_cache: dict = {"routes": None, "models": None}
_spend: dict = {"at": 0.0, "month": None, "value": 0.0}


def _json(text) -> dict:
    try:
        value = json.loads(text) if text else {}
    except (ValueError, TypeError):
        value = {}
    return value if isinstance(value, dict) else {}


def load_routes(db=None) -> None:
    """Read the routes and the model list into memory. Called on first use and by every
    write (``model_routes.set_route``, an undo). ``db`` is the session that made the write;
    with none a short-lived one is opened. Tables that are still empty are seeded."""
    import models
    own = db is None
    if own:
        from database import SessionLocal
        db = SessionLocal()
    try:
        routes = db.query(models.ModelRoute).all()
        known = db.query(models.ModelPrice).all()
        if len({r.task for r in routes}) < len(TASKS) or not known:
            from migrations import m026_model_router
            m026_model_router.seed(db)
            if own:
                db.commit()
            else:
                db.flush()
            routes = db.query(models.ModelRoute).all()
            known = db.query(models.ModelPrice).all()
        _cache["models"] = {m.model: {
            "model": m.model, "provider": m.provider, "kind": m.kind,
            "input": m.input_per_mtok, "cached_input": m.cached_input_per_mtok,
            "cache_write": m.cache_write_per_mtok, "output": m.output_per_mtok,
            "fallback_model": m.fallback_model, "default_params": _json(m.default_params_json),
            "confirmed": bool(m.confirmed), "note": m.note,
        } for m in known}
        _cache["routes"] = {r.task: Route(r.task, r.provider, r.model, _json(r.params_json)) for r in routes}
    finally:
        if own:
            db.close()


def reset_routes() -> None:
    """Forget the cached routes, models and month-to-date spend (tests)."""
    _cache["routes"] = _cache["models"] = None
    _spend.update(at=0.0, month=None, value=0.0)


def routes(db=None) -> dict:
    if _cache["routes"] is None:
        load_routes(db)
    return _cache["routes"]


def known_models(db=None) -> dict:
    if _cache["models"] is None:
        load_routes(db)
    return _cache["models"]


def route(task: str, db=None) -> Route:
    found = routes(db).get(task)
    if found is None:
        raise AIUnavailable(f"no model route is set for '{task}'")
    return found


def model_for(task: str, db=None) -> str:
    return route(task, db).model


def assistant_model() -> str:
    """The model the assistant route points at (shown by /health and stored with summaries)."""
    return model_for("assistant")


def embedding_model() -> str:
    return model_for("embedding")


def research_model() -> str:
    return model_for("market_intel")


def _provider_of(model: str) -> str:
    info = known_models().get(model)
    if info:
        return info["provider"]
    return "anthropic" if str(model).lower().startswith("claude") else "openai"


def fallback_map() -> dict:
    """model → its counterpart on the other provider. The seeded pairs, with ``FALLBACK_MAP``
    on top: a JSON object, or ``a=b,c=d``. Each pair works in both directions."""
    pairs = {m: info["fallback_model"] for m, info in known_models().items() if info.get("fallback_model")}
    raw = (os.getenv("FALLBACK_MAP") or "").strip()
    extra: dict = {}
    if raw.startswith("{"):
        extra = _json(raw)
    elif raw:
        for part in raw.split(","):
            left, _, right = part.partition("=")
            if left.strip() and right.strip():
                extra[left.strip()] = right.strip()
    for left, right in extra.items():
        pairs[str(left)] = str(right)
        pairs.setdefault(str(right), str(left))
    return pairs


def counterpart(model: str) -> str | None:
    """The matching model on the other provider, or None."""
    other = fallback_map().get(model)
    if other and _provider_of(other) != _provider_of(model):
        return other
    return None


def _default_params(model: str) -> dict:
    return dict((known_models().get(model) or {}).get("default_params") or {})


# ── The monthly cap ──────────────────────────────────────────

def monthly_cap() -> float | None:
    try:
        value = float(os.getenv("MONTHLY_LLM_CAP_USD") or 0)
    except ValueError:
        return None
    return value if value > 0 else None


def month_to_date(db=None, *, fresh: bool = False) -> float:
    """Estimated spend since the first of the month (the user's month). Cached for a minute."""
    import models
    import user_clock
    from agents import schedule
    today = user_clock.today()
    month = (today.year, today.month)
    if not fresh and _spend["month"] == month and time.monotonic() - _spend["at"] < 60:
        return _spend["value"]
    start = schedule.to_utc(datetime(today.year, today.month, 1, tzinfo=schedule.tz()))
    own = db is None
    if own:
        from database import SessionLocal
        db = SessionLocal()
    try:
        from sqlalchemy import func
        total = db.query(func.coalesce(func.sum(models.LLMUsage.estimated_cost_usd), 0.0)).filter(
            models.LLMUsage.created_at >= start).scalar() or 0.0
    except Exception as exc:  # noqa: BLE001 — the cap must never break a reply
        print(f"[llm] could not read month-to-date spend: {type(exc).__name__}")
        total = _spend["value"]
    finally:
        if own:
            db.close()
    _spend.update(at=time.monotonic(), month=month, value=float(total))
    return float(total)


def _price_rank(model: str) -> float | None:
    info = known_models().get(model) or {}
    if info.get("input") is None or info.get("output") is None:
        return None
    return float(info["input"]) + float(info["output"])


def cheapest_model() -> str | None:
    """The cheapest chat model that is configured: one a chat route uses, or the counterpart
    of one, whose provider has a key."""
    candidates = set()
    for task in CHAT_TASKS:
        found = routes().get(task)
        if found is None:
            continue
        candidates.add(found.model)
        other = counterpart(found.model)
        if other:
            candidates.add(other)
    priced = [(rank, model) for model in candidates
              if provider_configured(_provider_of(model)) and (rank := _price_rank(model)) is not None]
    return min(priced)[1] if priced else None


def cap_state(db=None, *, fresh: bool = False) -> dict:
    """Where spend stands against the cap. ``downgraded`` lists the tasks now served by the
    cheapest model instead of their own."""
    cap = monthly_cap()
    spent = month_to_date(db, fresh=fresh)
    over = bool(cap) and spent >= CAP_THRESHOLD * cap
    cheapest = cheapest_model() if over else None
    downgraded = []
    if cheapest:
        floor = _price_rank(cheapest)
        for task in CHAT_TASKS:
            found = routes().get(task)
            rank = _price_rank(found.model) if found else None
            if found and found.model != cheapest and rank is not None and rank > floor:
                downgraded.append(task)
    return {"monthly_cap_usd": cap, "month_to_date_usd": round(spent, 4),
            "pct": round(100 * spent / cap, 1) if cap else None, "over_threshold": over,
            "cheapest_model": cheapest, "downgraded_tasks": downgraded}


# ── Planning a call ──────────────────────────────────────────

@dataclass
class _Step:
    provider: str
    model: str
    params: dict
    fallback_from: str | None = None


def _plan(task: str, db=None) -> list[_Step]:
    """Who is asked, in order: the route (or the cheapest model past the cap), then its
    counterpart on the other provider when that provider has a key."""
    found = route(task, db)
    first = _Step(found.provider, found.model, dict(found.params))
    if task not in OPENAI_ONLY and monthly_cap():
        state = cap_state(db)
        if task in state["downgraded_tasks"]:
            cheap = state["cheapest_model"]
            first = _Step(_provider_of(cheap), cheap, _default_params(cheap))
    steps = [first] if provider_configured(first.provider) else []
    other = None if task in OPENAI_ONLY else counterpart(first.model)
    if other and provider_configured(_provider_of(other)):
        reason = f"{first.provider}:{first.model}" + ("" if steps else " (no key)")
        steps.append(_Step(_provider_of(other), other, _default_params(other), fallback_from=reason))
    if not steps:
        adapter = PROVIDERS.get(first.provider)
        key_name = adapter.KEY_ENV if adapter else "the provider key"
        raise AIUnavailable(f"{key_name} is not set — AI features are disabled.")
    return steps


def is_configured(task: str | None = None) -> bool:
    """Can a model call be served? With no task: is any chat provider keyed. With a task:
    can that task's route (or its counterpart) be served."""
    if task is None:
        return any(adapter.configured() for adapter in PROVIDERS.values())
    try:
        return bool(_plan(task))
    except AIUnavailable:
        return False
    except Exception as exc:  # noqa: BLE001 — a missing table must read as "not configured"
        print(f"[llm] could not read the route for {task}: {type(exc).__name__}")
        return False


# ── Cost and usage ───────────────────────────────────────────

def estimate_cost(model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int,
                  cache_write_tokens: int = 0) -> float:
    """Estimated USD for one call, from ``model_prices``. A model with no price on file
    (one billed by the minute, or one named in the environment) costs 0 here."""
    try:
        price = known_models().get(model) or {}
    except Exception:  # noqa: BLE001 — never lose a usage row over a price lookup
        price = {}
    inp = float(price.get("input") or 0)
    cached_price = price.get("cached_input")
    write_price = price.get("cache_write")
    total_in = int(input_tokens or 0)
    cached = min(int(cached_input_tokens or 0), total_in)
    written = min(int(cache_write_tokens or 0), total_in - cached)
    fresh = total_in - cached - written
    cost = (
        fresh * inp
        + cached * float(inp if cached_price is None else cached_price)
        + written * float(inp if write_price is None else write_price)
        + int(output_tokens or 0) * float(price.get("output") or 0)
    ) / 1_000_000
    return round(cost, 8)


def log_usage(db, *, model: str, purpose: str, input_tokens: int = 0,
              cached_input_tokens: int = 0, output_tokens: int = 0,
              duration_ms: int | None = None, agent_run_id: int | None = None,
              provider: str | None = None, task: str | None = None,
              cache_write_tokens: int = 0, fallback_from: str | None = None) -> None:
    """Record one model call. Never raises — a logging failure must not break a reply.

    ``db`` may be None, in which case a short-lived session is opened. The call is
    attributed to an agent run from ``agent_run_id``, else the session's
    ``db.info["agent_run_id"]``, else the active run context."""
    import models

    if agent_run_id is None and db is not None:
        agent_run_id = db.info.get("agent_run_id")
    if agent_run_id is None:
        agent_run_id = current_run_id()
    own = db is None
    if own:
        from database import SessionLocal
        db = SessionLocal()
    try:
        row = models.LLMUsage(
            created_at=datetime.utcnow(),
            model=model,
            purpose=purpose,
            provider=provider or _provider_of(model),
            task=task or task_for(purpose),
            input_tokens=int(input_tokens or 0),
            cached_input_tokens=int(cached_input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            estimated_cost_usd=estimate_cost(model, input_tokens, cached_input_tokens, output_tokens,
                                             cache_write_tokens),
            duration_ms=duration_ms,
            agent_run_id=agent_run_id,
            fallback_from=fallback_from,
        )
        db.add(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        print(f"[llm] usage logging failed: {exc}")
    finally:
        if own:
            db.close()


def _log_fallback(task: str, step: _Step, nxt: _Step, exc: Exception) -> None:
    reason = "refused" if isinstance(exc, _Refused) else type(exc).__name__
    print(f"[llm] fallback: {task} on {step.provider}:{step.model} failed ({reason}); "
          f"retrying once on {nxt.provider}:{nxt.model}")


# ── Calls ────────────────────────────────────────────────────

def complete(task: str, messages: list, *, db=None, purpose: str | None = None, tools=None,
             json_mode: bool = False, max_output: int | None = None) -> Completion:
    """One model call through the task's route. Returns a ``Completion``. Logs usage.

    ``purpose`` is the label the call is logged under (defaults to the task).
    ``json_mode`` asks for a single JSON object. ``max_output`` caps the output tokens.
    No other parameter reaches a provider unless the route's ``params`` lists it."""
    _count_call()
    steps = _plan(task, db)
    for index, step in enumerate(steps):
        last = index + 1 >= len(steps)
        started = time.monotonic()
        try:
            result = PROVIDERS[step.provider].complete(
                step.model, messages, tools=tools, params=step.params, json_mode=json_mode, max_output=max_output)
            if result.stop == "refusal" and not last:
                raise _Refused()
        except Exception as exc:  # noqa: BLE001 — provider error, timeout or refusal
            if last or isinstance(exc, (AIUnavailable, BudgetExceeded)):
                raise
            _log_fallback(task, step, steps[index + 1], exc)
            continue
        result.provider, result.model = step.provider, step.model
        log_usage(db, model=step.model, purpose=purpose or task, provider=step.provider, task=task,
                  input_tokens=result.input_tokens, cached_input_tokens=result.cached_input_tokens,
                  output_tokens=result.output_tokens, cache_write_tokens=result.cache_write_tokens,
                  duration_ms=int((time.monotonic() - started) * 1000), fallback_from=step.fallback_from)
        return result
    raise AIUnavailable("no provider could serve the call")      # unreachable: _plan never returns []


def stream(task: str, messages: list, tools=None, *, db=None, purpose: str | None = None,
           max_output: int | None = None):
    """The same call streamed. Yields ``{"content"}``, ``{"tool_calls"}``,
    ``{"finish_reason"}`` and, when the provider needs its own blocks back later in the
    turn, ``{"provider_raw"}``. Usage is logged when the stream ends.

    A failure before anything was sent on is retried once on the other provider. After the
    first delta it is not: the text already on screen would be repeated."""
    _count_call()
    steps = _plan(task, db)
    for index, step in enumerate(steps):
        last = index + 1 >= len(steps)
        started = time.monotonic()
        usage = {"input": 0, "cached": 0, "cache_write": 0, "output": 0}
        sent = False
        try:
            for delta in PROVIDERS[step.provider].stream(step.model, messages, tools, step.params, max_output):
                if "usage" in delta:
                    usage = delta["usage"]
                    continue
                if delta.get("finish_reason") == "refusal" and not sent and not last:
                    raise _Refused()
                if "content" in delta or "tool_calls" in delta:
                    sent = True
                yield delta
        except Exception as exc:  # noqa: BLE001
            if sent or last or isinstance(exc, (AIUnavailable, BudgetExceeded)):
                raise
            _log_fallback(task, step, steps[index + 1], exc)
            continue
        log_usage(None if db is None else db, model=step.model, purpose=purpose or task, provider=step.provider,
                  task=task, input_tokens=usage["input"], cached_input_tokens=usage["cached"],
                  output_tokens=usage["output"], cache_write_tokens=usage["cache_write"],
                  duration_ms=int((time.monotonic() - started) * 1000), fallback_from=step.fallback_from)
        return


def chat_text(messages: list, *, db=None, purpose: str = "assistant", task: str | None = None,
              max_completion_tokens: int | None = None, max_tokens: int | None = None,
              response_format: dict | None = None) -> str:
    """Convenience over ``complete``: the text of the answer, or '' if empty. The task is
    taken from ``purpose`` unless given. ``response_format={"type": "json_object"}`` asks
    for JSON. (``max_tokens`` is accepted as an old spelling of the output cap; what is
    sent to OpenAI is always ``max_completion_tokens``.)"""
    cap = max_completion_tokens if max_completion_tokens is not None else max_tokens
    wants_json = isinstance(response_format, dict) and response_format.get("type") == "json_object"
    return complete(task or task_for(purpose), messages, db=db, purpose=purpose,
                    json_mode=wants_json, max_output=cap).text or ""


def _openai_only(task: str, db=None) -> Route:
    found = route(task, db)
    if not openai_adapter.configured():
        raise AIUnavailable(f"{openai_adapter.KEY_ENV} is not set — AI features are disabled.")
    return found


def embed_texts(texts: list[str], *, db=None, purpose: str = "embedding") -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input, in order. Logs usage."""
    if not texts:
        return []
    _count_call()
    found = _openai_only("embedding", db)
    started = time.monotonic()
    vectors, usage = openai_adapter.embed(found.model, texts)
    log_usage(db, model=found.model, purpose=purpose, provider=openai_adapter.NAME, task="embedding",
              input_tokens=usage["input"], cached_input_tokens=usage["cached"], output_tokens=usage["output"],
              duration_ms=int((time.monotonic() - started) * 1000))
    return vectors


def transcribe(file_bytes: bytes, filename: str = "audio.webm", *, db=None) -> str:
    """Transcribe a recorded voice note. Returns plain text."""
    _count_call()
    found = _openai_only("transcribe", db)
    started = time.monotonic()
    text = openai_adapter.transcribe(found.model, file_bytes, filename)
    log_usage(db, model=found.model, purpose="transcribe", provider=openai_adapter.NAME, task="transcribe",
              duration_ms=int((time.monotonic() - started) * 1000))
    return text


def speech(text: str, *, db=None) -> bytes:
    """Reply text as spoken audio (MP3 bytes). The voice is the route's ``voice`` parameter."""
    _count_call()
    found = _openai_only("speech", db)
    started = time.monotonic()
    audio = openai_adapter.speech(found.model, found.params.get("voice") or "alloy", text[:4000])
    log_usage(db, model=found.model, purpose="speech", provider=openai_adapter.NAME, task="speech",
              duration_ms=int((time.monotonic() - started) * 1000))
    return audio
