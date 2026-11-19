"""
Migration 026 — the model router (Phase 7, Stage 1).

New tables:
  model_prices  — every model Founder OS knows: provider, kind, price per million tokens,
                  its counterpart on the other provider, the parameters a route starts with
  model_routes  — one row per task: provider, model, parameters

Columns added:
  llm_usage.provider       — who served the call (rows from before this migration: openai)
  llm_usage.task           — the route the call went through
  llm_usage.fallback_from  — "provider:model" that failed first, when the call is a retry

This file is the only place a model id or a price is written down. The rows below are
SEEDS: a model or a route that is already in its table is never touched again, so after
the first run the tables are the source of truth. Routes are seeded from the environment
(``ASSISTANT_MODEL``, ``RESEARCH_MODEL``, ``EMBEDDING_MODEL``, ``REALTIME_MODEL``,
``REALTIME_TRANSCRIBE_MODEL``, ``REALTIME_VOICE``), falling back to the defaults here.

Prices were read from the providers' own pages on 2026-11-19:
  OpenAI     https://developers.openai.com/api/docs/pricing
  Anthropic  https://platform.claude.com/docs/en/about-claude/pricing
``confirmed`` is false where a model is not billed per token, so no cost is estimated.

SQLite + Postgres. Idempotent. No drops. RLS enabled on both tables (Postgres).
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from database import engine
from migrations._helpers import (dialect, add_column_if_missing, create_table_if_missing,
                                 create_index, enable_rls, claim_once)

TAG = "m026"

_TABLES = {
    "model_prices": """
CREATE TABLE IF NOT EXISTS model_prices (
    id                    INTEGER PRIMARY KEY {autoincrement},
    model                 VARCHAR NOT NULL UNIQUE,
    provider              VARCHAR NOT NULL,
    kind                  VARCHAR NOT NULL DEFAULT 'chat',
    input_per_mtok        FLOAT,
    cached_input_per_mtok FLOAT,
    cache_write_per_mtok  FLOAT,
    output_per_mtok       FLOAT,
    fallback_model        VARCHAR,
    default_params_json   TEXT,
    confirmed             BOOLEAN DEFAULT FALSE,
    note                  VARCHAR,
    updated_at            DATETIME
)""",
    "model_routes": """
CREATE TABLE IF NOT EXISTS model_routes (
    id          INTEGER PRIMARY KEY {autoincrement},
    task        VARCHAR NOT NULL UNIQUE,
    provider    VARCHAR NOT NULL,
    model       VARCHAR NOT NULL,
    params_json TEXT,
    updated_at  DATETIME
)""",
}

_OPENAI_PRICES = "developers.openai.com/api/docs/pricing, 2026-11-19"
_ANTHROPIC_PRICES = "platform.claude.com/docs/en/about-claude/pricing, 2026-11-19"

# (model, provider, kind, input, cached input, cache write, output, counterpart, default params, confirmed, note)
SEED_MODELS = [
    ("gpt-5.4-mini", "openai", "chat", 0.75, 0.075, None, 4.50, "claude-haiku-5-5", None, True,
     _OPENAI_PRICES),
    ("gpt-5.4", "openai", "chat", 2.50, 0.25, None, 15.00, "claude-sonnet-5-5", None, True,
     _OPENAI_PRICES + ". Prompts under 272K tokens; longer prompts cost more"),
    ("claude-haiku-5-5", "anthropic", "chat", 0.10, 0.01, 0.125, 0.50, "gpt-5.4-mini",
     {"thinking": {"type": "disabled"}}, True,
     _ANTHROPIC_PRICES + ". Prompts up to 100K tokens; longer prompts cost five times as much"),
    ("claude-sonnet-5-5", "anthropic", "chat", 2.00, 0.10, 2.50, 10.00, "gpt-5.4",
     {"thinking": {"type": "between_tools"}}, True,
     _ANTHROPIC_PRICES + ". This model has no 'disabled' setting: 'between_tools' is how thinking is switched off"),
    ("claude-opus-5-5", "anthropic", "chat", 4.00, 0.20, 5.00, 20.00, None, None, True,
     _ANTHROPIC_PRICES + ". Thinking cannot be switched off on this model"),
    ("text-embedding-3-small", "openai", "embedding", 0.02, 0.02, None, 0.0, None, None, True, _OPENAI_PRICES),
    ("whisper-1", "openai", "audio", None, None, None, None, None, None, False,
     "Billed per minute ($0.006), not per token: no cost is estimated"),
    ("tts-1", "openai", "audio", None, None, None, None, None, {"voice": "onyx"}, False,
     "Billed per character ($15 per 1M), not per token: no cost is estimated"),
    ("gpt-4o-mini-transcribe", "openai", "audio", 1.25, None, None, 5.00, None, None, True,
     _OPENAI_PRICES + ". Also $0.003 per minute"),
    ("gpt-realtime-2.1-mini", "openai", "realtime", 0.60, 0.06, None, 2.40, None, {"voice": "marin"}, True,
     _OPENAI_PRICES + ". Text rates; audio is $10 in and $20 out. Voice sessions are not metered here"),
]

_DEFAULT_ASSISTANT = "gpt-5.4-mini"

# task → (environment variables that name its model, default model)
SEED_ROUTES = {
    "assistant":            (("ASSISTANT_MODEL", "ORCHESTRATOR_MODEL"), _DEFAULT_ASSISTANT),
    "parser":               (("ASSISTANT_MODEL", "ORCHESTRATOR_MODEL"), _DEFAULT_ASSISTANT),
    "conversation_summary": (("ASSISTANT_MODEL", "ORCHESTRATOR_MODEL"), _DEFAULT_ASSISTANT),
    "briefing_summary":     (("ASSISTANT_MODEL", "ORCHESTRATOR_MODEL"), _DEFAULT_ASSISTANT),
    "agents":               (("ASSISTANT_MODEL", "ORCHESTRATOR_MODEL"), _DEFAULT_ASSISTANT),
    "analysis":             (("ASSISTANT_MODEL", "ORCHESTRATOR_MODEL"), _DEFAULT_ASSISTANT),
    "market_intel":         (("RESEARCH_MODEL", "ASSISTANT_MODEL", "ORCHESTRATOR_MODEL"), _DEFAULT_ASSISTANT),
    "embedding":            (("EMBEDDING_MODEL",), "text-embedding-3-small"),
    "transcribe":           ((), "whisper-1"),
    "speech":               ((), "tts-1"),
    "realtime":             (("REALTIME_MODEL",), "gpt-realtime-2.1-mini"),
    "realtime_transcribe":  (("REALTIME_TRANSCRIBE_MODEL",), "gpt-4o-mini-transcribe"),
}

# How a usage row written before this migration maps onto a task.
_PURPOSE_TASK_SQL = """
UPDATE llm_usage SET task = CASE
    WHEN purpose = 'assistant' THEN 'assistant'
    WHEN purpose = 'parse' THEN 'parser'
    WHEN purpose IN ('conversation_summary', 'briefing_summary') THEN purpose
    WHEN purpose LIKE 'market_intel%' THEN 'market_intel'
    WHEN purpose LIKE 'embed%' THEN 'embedding'
    WHEN purpose LIKE 'realtime%' THEN 'realtime'
    WHEN purpose IN ('chat', 'proactive_brief') THEN 'analysis'
    ELSE 'agents' END
WHERE task IS NULL"""


def guess_provider(model: str) -> str:
    return "anthropic" if str(model or "").lower().startswith("claude") else "openai"


def _env_model(names, default: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return default


def seed(conn) -> dict:
    """Insert the models and routes that are missing. ``conn`` is anything with
    ``execute`` (a Connection here, a Session when the gateway seeds an empty test
    database). Never changes a row that exists. The caller owns the transaction."""
    now = datetime.utcnow()
    added = {"models": 0, "routes": 0}

    def known_models() -> dict:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            text("SELECT model, provider, default_params_json FROM model_prices")).fetchall()}

    known = known_models()
    for (model, provider, kind, inp, cached, write, out, fallback, params, confirmed, note) in SEED_MODELS:
        if model in known:
            continue
        conn.execute(text(
            "INSERT INTO model_prices (model, provider, kind, input_per_mtok, cached_input_per_mtok, "
            "cache_write_per_mtok, output_per_mtok, fallback_model, default_params_json, confirmed, note, updated_at) "
            "VALUES (:model, :provider, :kind, :inp, :cached, :write, :out, :fallback, :params, :confirmed, :note, :now)"),
            {"model": model, "provider": provider, "kind": kind, "inp": inp, "cached": cached, "write": write,
             "out": out, "fallback": fallback, "params": json.dumps(params) if params else None,
             "confirmed": bool(confirmed), "note": note, "now": now})
        added["models"] += 1

    # Prices given in LLM_PRICING_JSON (the override that predates the table) are seeded too.
    try:
        extra = json.loads(os.getenv("LLM_PRICING_JSON") or "{}")
    except (ValueError, TypeError):
        extra = {}
    known = known_models()
    for model, price in (extra.items() if isinstance(extra, dict) else []):
        if model in known or not isinstance(price, dict):
            continue
        conn.execute(text(
            "INSERT INTO model_prices (model, provider, kind, input_per_mtok, cached_input_per_mtok, "
            "output_per_mtok, confirmed, note, updated_at) "
            "VALUES (:model, :provider, 'chat', :inp, :cached, :out, FALSE, 'From LLM_PRICING_JSON', :now)"),
            {"model": model, "provider": guess_provider(model), "inp": price.get("input"),
             "cached": price.get("cached_input"), "out": price.get("output"), "now": now})
        added["models"] += 1

    known = known_models()
    have = {r[0] for r in conn.execute(text("SELECT task FROM model_routes")).fetchall()}
    for task, (env_names, default) in SEED_ROUTES.items():
        if task in have:
            continue
        model = _env_model(env_names, default)
        if model not in known:
            # A model named in the environment that the seed list does not know: record it
            # with no price, so its cost shows as not estimated instead of as a guess.
            conn.execute(text(
                "INSERT INTO model_prices (model, provider, kind, confirmed, note, updated_at) "
                "VALUES (:model, :provider, 'chat', FALSE, 'Named in the environment; price not known', :now)"),
                {"model": model, "provider": guess_provider(model), "now": now})
            known[model] = (guess_provider(model), None)
            added["models"] += 1
        provider, params = known[model]
        if task == "realtime" and os.getenv("REALTIME_VOICE"):
            params = json.dumps({**(json.loads(params) if params else {}), "voice": os.getenv("REALTIME_VOICE")})
        conn.execute(text(
            "INSERT INTO model_routes (task, provider, model, params_json, updated_at) "
            "VALUES (:task, :provider, :model, :params, :now)"),
            {"task": task, "provider": provider, "model": model, "params": params, "now": now})
        added["routes"] += 1
    return added


def run(db=None):
    conn = engine.connect()
    try:
        d = dialect(conn, TAG)
        with conn.begin():
            for table, sql in _TABLES.items():
                create_table_if_missing(conn, d, table, sql, TAG)
            add_column_if_missing(conn, d, "llm_usage", "provider", "VARCHAR", TAG)
            add_column_if_missing(conn, d, "llm_usage", "task", "VARCHAR", TAG)
            add_column_if_missing(conn, d, "llm_usage", "fallback_from", "VARCHAR", TAG)
            create_index(conn, "ix_llm_usage_task", "llm_usage", "task")
            if claim_once(conn, d, "m026:llm_usage_backfill", TAG):
                # Every call before the router went to OpenAI. Only empty cells are filled.
                conn.execute(text("UPDATE llm_usage SET provider = 'openai' WHERE provider IS NULL"))
                conn.execute(text(_PURPOSE_TASK_SQL))
            added = seed(conn)
            enable_rls(conn, d, list(_TABLES), TAG)
        print(f"[{TAG}] Model router ready on {d}: {added['models']} model(s) and "
              f"{added['routes']} route(s) seeded.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
