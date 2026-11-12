"""
Single gateway to OpenAI.

Every call the backend makes to OpenAI goes through here so that:
  * the client is created lazily — the app boots and every non-AI endpoint works
    with no OPENAI_API_KEY; AI endpoints raise ``AIUnavailable`` (→ HTTP 503)
  * the model is configured in one place (``ASSISTANT_MODEL``, default gpt-5.4-mini)
  * every call is logged to the ``llm_usage`` table with an estimated cost

Tests never reach OpenAI: they patch ``chat_completion`` / ``embed_texts`` or the
streaming wrapper in ``openai_client``.
"""

import json
import os
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

DEFAULT_ASSISTANT_MODEL = "gpt-5.4-mini"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class AIUnavailable(RuntimeError):
    """Raised when an AI feature is requested but no OPENAI_API_KEY is configured."""


class BudgetExceeded(RuntimeError):
    """Raised when an agent run tries to make more model calls than its cap allows."""


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


_client = None


def api_key() -> str | None:
    return os.getenv("OPENAI_API_KEY") or None


def is_configured() -> bool:
    return api_key() is not None


def get_client():
    """Return the shared OpenAI client, creating it on first use."""
    global _client
    if _client is None:
        key = api_key()
        if not key:
            raise AIUnavailable("OPENAI_API_KEY is not set — AI features are disabled.")
        from openai import OpenAI
        _client = OpenAI(api_key=key)
    return _client


def reset_client() -> None:
    """Drop the cached client (tests, key rotation)."""
    global _client
    _client = None


def assistant_model() -> str:
    """The chat model for the assistant, parser and briefing summary."""
    return (
        os.getenv("ASSISTANT_MODEL")
        or os.getenv("ORCHESTRATOR_MODEL")   # legacy name, honoured as a fallback
        or DEFAULT_ASSISTANT_MODEL
    )


def research_model() -> str:
    """The model for the market intelligence roles (``RESEARCH_MODEL``); the assistant
    model when unset."""
    return os.getenv("RESEARCH_MODEL") or assistant_model()


def embedding_model() -> str:
    return os.getenv("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL


# ── Pricing (USD per 1M tokens) ──────────────────────────────
# Estimates only. Override with LLM_PRICING_JSON, e.g.
#   {"gpt-5.4-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.0}}
_DEFAULT_PRICING = {
    "gpt-5.4-mini":            {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5.4":                 {"input": 2.50, "cached_input": 0.25,  "output": 10.00},
    "gpt-4o-mini":             {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "text-embedding-3-small":  {"input": 0.02, "cached_input": 0.02,  "output": 0.00},
    "whisper-1":               {"input": 0.0,  "cached_input": 0.0,   "output": 0.00},
    "tts-1":                   {"input": 0.0,  "cached_input": 0.0,   "output": 0.00},
}


def _pricing() -> dict:
    table = dict(_DEFAULT_PRICING)
    raw = os.getenv("LLM_PRICING_JSON")
    if raw:
        try:
            table.update(json.loads(raw))
        except (ValueError, TypeError):
            pass
    return table


def estimate_cost(model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    price = _pricing().get(model)
    if price is None:
        # Unknown model: fall back to the assistant default so cost is never silently zero.
        price = _pricing().get(DEFAULT_ASSISTANT_MODEL, {"input": 0, "cached_input": 0, "output": 0})
    uncached = max(0, int(input_tokens or 0) - int(cached_input_tokens or 0))
    cost = (
        uncached * price.get("input", 0)
        + int(cached_input_tokens or 0) * price.get("cached_input", 0)
        + int(output_tokens or 0) * price.get("output", 0)
    ) / 1_000_000
    return round(cost, 8)


# ── Usage logging ────────────────────────────────────────────

def log_usage(db, *, model: str, purpose: str, input_tokens: int = 0,
              cached_input_tokens: int = 0, output_tokens: int = 0,
              duration_ms: int | None = None, agent_run_id: int | None = None) -> None:
    """Record one OpenAI call. Never raises — a logging failure must not break a reply.

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
            input_tokens=int(input_tokens or 0),
            cached_input_tokens=int(cached_input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            estimated_cost_usd=estimate_cost(model, input_tokens, cached_input_tokens, output_tokens),
            duration_ms=duration_ms,
            agent_run_id=agent_run_id,
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


def usage_from_response(usage) -> tuple[int, int, int]:
    """Extract (input, cached_input, output) token counts from an SDK usage object."""
    if usage is None:
        return 0, 0, 0
    inp = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None) or 0
    out = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None) or 0
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None) or getattr(usage, "input_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None) or 0
    return int(inp), int(cached), int(out)


# ── Calls ────────────────────────────────────────────────────

def chat_completion(messages: list, *, db=None, purpose: str = "assistant",
                    model: str | None = None, **kwargs):
    """Non-streaming chat completion. Returns the SDK response. Logs usage."""
    _count_call()
    client = get_client()
    model = model or assistant_model()
    started = time.monotonic()
    response = client.chat.completions.create(model=model, messages=messages, **kwargs)
    duration = int((time.monotonic() - started) * 1000)
    inp, cached, out = usage_from_response(getattr(response, "usage", None))
    log_usage(db, model=model, purpose=purpose, input_tokens=inp,
              cached_input_tokens=cached, output_tokens=out, duration_ms=duration)
    return response


def chat_text(messages: list, *, db=None, purpose: str = "assistant",
              model: str | None = None, **kwargs) -> str:
    """Convenience: the text of the first choice, or '' if empty."""
    response = chat_completion(messages, db=db, purpose=purpose, model=model, **kwargs)
    return (response.choices[0].message.content or "") if response.choices else ""


def embed_texts(texts: list[str], *, db=None, purpose: str = "embedding") -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input, in order. Logs usage."""
    if not texts:
        return []
    _count_call()
    client = get_client()
    model = embedding_model()
    started = time.monotonic()
    response = client.embeddings.create(model=model, input=texts)
    duration = int((time.monotonic() - started) * 1000)
    inp, cached, out = usage_from_response(getattr(response, "usage", None))
    log_usage(db, model=model, purpose=purpose, input_tokens=inp,
              cached_input_tokens=cached, output_tokens=out, duration_ms=duration)
    ordered = sorted(response.data, key=lambda d: d.index)
    return [list(d.embedding) for d in ordered]
